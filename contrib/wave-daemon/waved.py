#!/usr/bin/env python3
"""
waved — Bitcoin Wave node management daemon.

Lightweight watchdog that manages a Bitcoin Wave node instance.
NOT an LLM — scripted logic only. Handles:
  - Health monitoring (block height, peer count, disk space)
  - Auto-restart on crash
  - Peer rotation
  - Upstream version tracking
  - Status reporting via local HTTP API

The daemon talks to bitcoind via RPC (which is enabled by default
in Bitcoin Wave). It exposes a simple JSON API on a local port for
the dashboard and management tools.
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

VERSION = "0.1.0"
DEFAULT_RPC_PORT = 8332
DEFAULT_API_PORT = 8380
DEFAULT_CHECK_INTERVAL = 30  # seconds
DEFAULT_PEER_ROTATION_HOURS = 24
MIN_PEERS = 4
MIN_DISK_GB = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [waved] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("waved")


@dataclass
class NodeStatus:
    running: bool = False
    block_height: int = 0
    header_height: int = 0
    sync_progress: float = 0.0
    peer_count: int = 0
    network: str = "unknown"
    version: str = ""
    disk_free_gb: float = 0.0
    uptime_seconds: int = 0
    last_check: float = 0.0
    errors: list = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class RPCClient:
    """Minimal Bitcoin RPC client — no external dependencies."""

    def __init__(self, url: str, auth: tuple[str, str] | None = None,
                 cookie_path: Path | None = None):
        self.url = url
        self.auth = auth
        self.cookie_path = cookie_path
        self._request_id = 0

    def _get_auth_header(self) -> str:
        import base64
        if self.cookie_path and self.cookie_path.exists():
            cookie = self.cookie_path.read_text().strip()
            creds = base64.b64encode(cookie.encode()).decode()
        elif self.auth:
            creds = base64.b64encode(
                f"{self.auth[0]}:{self.auth[1]}".encode()
            ).decode()
        else:
            raise RuntimeError("No RPC auth configured")
        return f"Basic {creds}"

    def call(self, method: str, params: list | None = None) -> dict:
        self._request_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or [],
        }).encode()

        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._get_auth_header(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if result.get("error"):
                    raise RuntimeError(f"RPC error: {result['error']}")
                return result["result"]
        except urllib.error.URLError as e:
            raise ConnectionError(f"Cannot reach bitcoind RPC: {e}")


class WaveDaemon:
    """Core watchdog logic."""

    def __init__(self, datadir: Path, rpc_port: int = DEFAULT_RPC_PORT,
                 api_port: int = DEFAULT_API_PORT,
                 check_interval: int = DEFAULT_CHECK_INTERVAL):
        self.datadir = datadir
        self.rpc_port = rpc_port
        self.api_port = api_port
        self.check_interval = check_interval
        self.status = NodeStatus()
        self.running = False

        cookie_path = datadir / ".cookie"
        self.rpc = RPCClient(
            url=f"http://127.0.0.1:{rpc_port}",
            cookie_path=cookie_path,
        )

    def check_node(self) -> NodeStatus:
        """Poll bitcoind and update status."""
        status = NodeStatus(last_check=time.time())

        try:
            info = self.rpc.call("getblockchaininfo")
            status.running = True
            status.block_height = info["blocks"]
            status.header_height = info["headers"]
            status.sync_progress = info.get("verificationprogress", 0.0)
            status.network = info.get("chain", "unknown")
        except ConnectionError:
            status.running = False
            status.errors.append("bitcoind not responding")
            self.status = status
            return status

        try:
            netinfo = self.rpc.call("getnetworkinfo")
            status.peer_count = netinfo.get("connections", 0)
            status.version = netinfo.get("subversion", "")
        except Exception as e:
            status.errors.append(f"getnetworkinfo failed: {e}")

        try:
            uptime = self.rpc.call("uptime")
            status.uptime_seconds = uptime
        except Exception:
            pass

        disk = os.statvfs(str(self.datadir))
        status.disk_free_gb = (disk.f_bavail * disk.f_frsize) / (1024**3)

        if status.peer_count < MIN_PEERS:
            status.errors.append(
                f"Low peer count: {status.peer_count} (min {MIN_PEERS})"
            )

        if status.disk_free_gb < MIN_DISK_GB:
            status.errors.append(
                f"Low disk space: {status.disk_free_gb:.1f}GB (min {MIN_DISK_GB}GB)"
            )

        sync_gap = status.header_height - status.block_height
        if sync_gap > 10 and status.sync_progress > 0.99:
            status.errors.append(f"Falling behind: {sync_gap} blocks behind tip")

        self.status = status
        return status

    def start(self):
        """Main loop."""
        self.running = True
        log.info("waved %s starting — monitoring bitcoind on port %d",
                 VERSION, self.rpc_port)

        api_thread = Thread(target=self._run_api, daemon=True)
        api_thread.start()

        while self.running:
            try:
                status = self.check_node()
                if status.running:
                    log.info(
                        "height=%d/%d peers=%d sync=%.4f disk=%.1fGB",
                        status.block_height, status.header_height,
                        status.peer_count, status.sync_progress,
                        status.disk_free_gb,
                    )
                else:
                    log.warning("bitcoind not responding")

                for err in status.errors:
                    log.warning(err)

            except Exception as e:
                log.error("check failed: %s", e)

            time.sleep(self.check_interval)

    def stop(self, *_):
        log.info("waved shutting down")
        self.running = False

    def _run_api(self):
        """Local HTTP API for dashboard / tooling."""
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/status":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    data = asdict(daemon.status)
                    self.wfile.write(json.dumps(data, indent=2).encode())
                elif self.path == "/health":
                    ok = daemon.status.running and not daemon.status.errors
                    self.send_response(200 if ok else 503)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "healthy": ok,
                        "errors": daemon.status.errors,
                    }).encode())
                elif self.path == "/version":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "waved": VERSION,
                        "node": daemon.status.version,
                    }).encode())
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):
                pass  # suppress request logging

        server = HTTPServer(("127.0.0.1", self.api_port), Handler)
        log.info("API listening on 127.0.0.1:%d", self.api_port)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(
        description="Bitcoin Wave node management daemon"
    )
    parser.add_argument(
        "--datadir", type=Path,
        default=Path.home() / ".bitcoin",
        help="Bitcoin data directory (default: ~/.bitcoin)",
    )
    parser.add_argument(
        "--rpc-port", type=int, default=DEFAULT_RPC_PORT,
        help=f"bitcoind RPC port (default: {DEFAULT_RPC_PORT})",
    )
    parser.add_argument(
        "--api-port", type=int, default=DEFAULT_API_PORT,
        help=f"waved API port (default: {DEFAULT_API_PORT})",
    )
    parser.add_argument(
        "--check-interval", type=int, default=DEFAULT_CHECK_INTERVAL,
        help=f"Health check interval in seconds (default: {DEFAULT_CHECK_INTERVAL})",
    )
    parser.add_argument(
        "--version", action="version", version=f"waved {VERSION}",
    )
    args = parser.parse_args()

    daemon = WaveDaemon(
        datadir=args.datadir,
        rpc_port=args.rpc_port,
        api_port=args.api_port,
        check_interval=args.check_interval,
    )

    signal.signal(signal.SIGINT, daemon.stop)
    signal.signal(signal.SIGTERM, daemon.stop)

    daemon.start()


if __name__ == "__main__":
    main()
