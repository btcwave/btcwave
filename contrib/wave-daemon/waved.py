#!/usr/bin/env python3
"""
waved — Bitcoin Wave node management daemon.

Lightweight watchdog that manages a Bitcoin Wave node instance.
NOT an LLM — scripted logic only. Handles:
  - Health monitoring (block height, peer count, disk space)
  - Auto-restart on crash (via systemd or direct process management)
  - Peer rotation
  - Upstream version tracking
  - Status reporting via local HTTP API
  - Status history for dashboard charts

The daemon talks to bitcoind via RPC (which is enabled by default
in Bitcoin Wave). It exposes a JSON API on a local port for
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
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Lock

VERSION = "0.2.0"
DEFAULT_RPC_PORT = 8332
DEFAULT_API_PORT = 8380
DEFAULT_CHECK_INTERVAL = 30  # seconds
DEFAULT_PEER_ROTATION_HOURS = 24
DEFAULT_HISTORY_SIZE = 2880  # 24 hours at 30s intervals
MIN_PEERS = 4
MIN_DISK_GB = 10
RESTART_COOLDOWN = 60  # seconds between restart attempts
MAX_RESTART_ATTEMPTS = 5

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
    connections_in: int = 0
    connections_out: int = 0
    network: str = "unknown"
    version: str = ""
    disk_free_gb: float = 0.0
    disk_used_gb: float = 0.0
    uptime_seconds: int = 0
    last_check: float = 0.0
    mempool_size: int = 0
    mempool_bytes: int = 0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class StatusSnapshot:
    timestamp: float
    block_height: int
    peer_count: int
    sync_progress: float
    mempool_size: int
    disk_free_gb: float


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
                 check_interval: int = DEFAULT_CHECK_INTERVAL,
                 manage_process: bool = False,
                 bitcoind_path: str = "bitcoind",
                 conf_path: Path | None = None,
                 dashboard_dir: Path | None = None,
                 bind_address: str = "127.0.0.1"):
        self.datadir = datadir
        self.rpc_port = rpc_port
        self.api_port = api_port
        self.check_interval = check_interval
        self.manage_process = manage_process
        self.bitcoind_path = bitcoind_path
        self.conf_path = conf_path
        self.dashboard_dir = dashboard_dir
        self.bind_address = bind_address
        self.status = NodeStatus()
        self.running = False
        self._history: deque[StatusSnapshot] = deque(maxlen=DEFAULT_HISTORY_SIZE)
        self._history_lock = Lock()
        self._restart_count = 0
        self._last_restart = 0.0
        self._started_at = 0.0
        self._bitcoind_proc = None
        self._last_peer_rotation = 0.0

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
            status.connections_in = netinfo.get("connections_in", 0)
            status.connections_out = netinfo.get("connections_out", 0)
            status.version = netinfo.get("subversion", "")
            for warning in netinfo.get("warnings", []):
                status.warnings.append(warning)
        except Exception as e:
            status.errors.append(f"getnetworkinfo failed: {e}")

        try:
            meminfo = self.rpc.call("getmempoolinfo")
            status.mempool_size = meminfo.get("size", 0)
            status.mempool_bytes = meminfo.get("bytes", 0)
        except Exception:
            pass

        try:
            uptime = self.rpc.call("uptime")
            status.uptime_seconds = uptime
        except Exception:
            pass

        try:
            disk = os.statvfs(str(self.datadir))
            status.disk_free_gb = (disk.f_bavail * disk.f_frsize) / (1024**3)
            total = (disk.f_blocks * disk.f_frsize) / (1024**3)
            status.disk_used_gb = total - status.disk_free_gb
        except OSError:
            pass

        if status.peer_count < MIN_PEERS:
            status.warnings.append(
                f"Low peer count: {status.peer_count} (min {MIN_PEERS})"
            )

        if status.disk_free_gb < MIN_DISK_GB:
            status.errors.append(
                f"Low disk space: {status.disk_free_gb:.1f}GB (min {MIN_DISK_GB}GB)"
            )

        sync_gap = status.header_height - status.block_height
        if sync_gap > 10 and status.sync_progress > 0.99:
            status.warnings.append(f"Falling behind: {sync_gap} blocks behind tip")

        with self._history_lock:
            self._history.append(StatusSnapshot(
                timestamp=status.last_check,
                block_height=status.block_height,
                peer_count=status.peer_count,
                sync_progress=status.sync_progress,
                mempool_size=status.mempool_size,
                disk_free_gb=status.disk_free_gb,
            ))

        self.status = status
        return status

    def _maybe_restart_bitcoind(self):
        """Attempt to restart bitcoind if it's down and we manage the process."""
        if not self.manage_process:
            return
        now = time.time()
        if now - self._last_restart < RESTART_COOLDOWN:
            return
        if self._restart_count >= MAX_RESTART_ATTEMPTS:
            log.error("Max restart attempts (%d) reached — giving up",
                      MAX_RESTART_ATTEMPTS)
            return

        log.warning("Attempting to restart bitcoind (attempt %d/%d)",
                     self._restart_count + 1, MAX_RESTART_ATTEMPTS)
        self._last_restart = now
        self._restart_count += 1

        cmd = [self.bitcoind_path, f"-datadir={self.datadir}"]
        if self.conf_path:
            cmd.append(f"-conf={self.conf_path}")
        cmd.append("-daemon")

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
            log.info("bitcoind restart initiated")
        except Exception as e:
            log.error("Failed to restart bitcoind: %s", e)

    def _maybe_rotate_peers(self):
        """Disconnect a random peer periodically to improve network diversity."""
        now = time.time()
        rotation_interval = DEFAULT_PEER_ROTATION_HOURS * 3600
        if now - self._last_peer_rotation < rotation_interval:
            return
        if not self.status.running or self.status.peer_count <= MIN_PEERS:
            return

        self._last_peer_rotation = now
        try:
            peers = self.rpc.call("getpeerinfo")
            if not peers:
                return
            inbound = [p for p in peers if p.get("connection_type") == "inbound"]
            if len(inbound) > 2:
                target = min(inbound, key=lambda p: p.get("last_recv", 0))
                self.rpc.call("disconnectnode", [target["addr"]])
                log.info("Rotated peer: disconnected %s", target["addr"])
        except Exception as e:
            log.debug("Peer rotation skipped: %s", e)

    def get_history(self, hours: float = 1.0) -> list[dict]:
        """Return status history for the requested time window."""
        cutoff = time.time() - (hours * 3600)
        with self._history_lock:
            return [asdict(s) for s in self._history if s.timestamp >= cutoff]

    def start(self):
        """Main loop."""
        self.running = True
        self._started_at = time.time()
        log.info("waved %s starting — monitoring bitcoind on port %d",
                 VERSION, self.rpc_port)

        api_thread = Thread(target=self._run_api, daemon=True)
        api_thread.start()

        while self.running:
            try:
                status = self.check_node()
                if status.running:
                    self._restart_count = 0
                    log.info(
                        "height=%d/%d peers=%d sync=%.4f mempool=%d disk=%.1fGB",
                        status.block_height, status.header_height,
                        status.peer_count, status.sync_progress,
                        status.mempool_size, status.disk_free_gb,
                    )
                    self._maybe_rotate_peers()
                else:
                    log.warning("bitcoind not responding")
                    self._maybe_restart_bitcoind()

                for err in status.errors:
                    log.error(err)
                for warn in status.warnings:
                    log.warning(warn)

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
            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

            def _json_response(self, code, data):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps(data, indent=2).encode())

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    if daemon.dashboard_dir:
                        index = daemon.dashboard_dir / "index.html"
                        if index.exists():
                            self.send_response(200)
                            self.send_header("Content-Type", "text/html")
                            self._cors()
                            self.end_headers()
                            self.wfile.write(index.read_bytes())
                            return
                    self._json_response(200, {
                        "service": "waved",
                        "version": VERSION,
                        "endpoints": ["/status", "/health", "/version",
                                      "/history", "/peers", "/network"],
                    })

                elif self.path == "/status":
                    data = asdict(daemon.status)
                    data["waved_uptime"] = int(time.time() - daemon._started_at)
                    data["restart_count"] = daemon._restart_count
                    self._json_response(200, data)

                elif self.path == "/health":
                    ok = daemon.status.running and not daemon.status.errors
                    self._json_response(200 if ok else 503, {
                        "healthy": ok,
                        "errors": daemon.status.errors,
                        "warnings": daemon.status.warnings,
                    })

                elif self.path == "/version":
                    self._json_response(200, {
                        "waved": VERSION,
                        "node": daemon.status.version,
                    })

                elif self.path.startswith("/history"):
                    hours = 1.0
                    if "?" in self.path:
                        params = dict(
                            p.split("=", 1) for p in self.path.split("?", 1)[1].split("&")
                            if "=" in p
                        )
                        try:
                            hours = min(float(params.get("hours", 1)), 24.0)
                        except ValueError:
                            pass
                    self._json_response(200, daemon.get_history(hours))

                elif self.path == "/peers":
                    try:
                        peers = daemon.rpc.call("getpeerinfo")
                        summary = [{
                            "addr": p.get("addr"),
                            "subver": p.get("subver"),
                            "connection_type": p.get("connection_type"),
                            "synced_headers": p.get("synced_headers"),
                            "synced_blocks": p.get("synced_blocks"),
                        } for p in peers]
                        self._json_response(200, summary)
                    except Exception as e:
                        self._json_response(503, {"error": str(e)})

                elif self.path == "/network":
                    try:
                        netinfo = daemon.rpc.call("getnetworkinfo")
                        nettotals = daemon.rpc.call("getnettotals")
                        self._json_response(200, {
                            "connections": netinfo.get("connections", 0),
                            "connections_in": netinfo.get("connections_in", 0),
                            "connections_out": netinfo.get("connections_out", 0),
                            "total_received_mb": nettotals.get("totalbytesrecv", 0) / (1024**2),
                            "total_sent_mb": nettotals.get("totalbytessent", 0) / (1024**2),
                            "networks": netinfo.get("networks", []),
                        })
                    except Exception as e:
                        self._json_response(503, {"error": str(e)})

                else:
                    self._json_response(404, {"error": "not found"})

            def log_message(self, format, *args):
                pass

        server = HTTPServer((daemon.bind_address, self.api_port), Handler)
        log.info("API listening on %s:%d", daemon.bind_address, self.api_port)
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
        "--manage-process", action="store_true",
        help="Manage bitcoind process (start/restart). Otherwise assume systemd.",
    )
    parser.add_argument(
        "--bitcoind", default="bitcoind",
        help="Path to bitcoind binary (default: bitcoind)",
    )
    parser.add_argument(
        "--conf", type=Path, default=None,
        help="Path to bitcoin.conf",
    )
    parser.add_argument(
        "--dashboard-dir", type=Path, default=None,
        help="Directory containing dashboard HTML (serves at /)",
    )
    parser.add_argument(
        "--bind", default="127.0.0.1",
        help="Bind address for API server (default: 127.0.0.1, use 0.0.0.0 for LAN access)",
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
        manage_process=args.manage_process,
        bitcoind_path=args.bitcoind,
        conf_path=args.conf,
        dashboard_dir=args.dashboard_dir,
        bind_address=args.bind,
    )

    signal.signal(signal.SIGINT, daemon.stop)
    signal.signal(signal.SIGTERM, daemon.stop)

    daemon.start()


if __name__ == "__main__":
    main()
