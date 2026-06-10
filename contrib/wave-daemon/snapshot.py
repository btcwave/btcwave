#!/usr/bin/env python3
"""
Bitcoin Wave snapshot bootstrapper.

Downloads a pre-validated blockchain snapshot for fast initial sync.
Snapshots are UTXO set snapshots compatible with Bitcoin Core/Knots
assumeutxo, or full block data archives.

Usage:
  python3 snapshot.py --datadir ~/.bitcoin --type utxo
  python3 snapshot.py --datadir ~/.bitcoin --type blocks --url https://snapshots.btcwave.app/latest.tar.zst
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import urllib.error
from pathlib import Path

VERSION = "0.1.0"
DEFAULT_SNAPSHOT_INDEX = "https://snapshots.btcwave.app/index.json"
CHUNK_SIZE = 8 * 1024 * 1024  # 8MB download chunks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [snapshot] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("snapshot")


def download_with_progress(url: str, dest: Path, expected_size: int = 0) -> Path:
    """Download a file with progress reporting."""
    log.info("Downloading %s", url)
    req = urllib.request.Request(url)
    downloaded = 0
    last_report = 0

    with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length", expected_size) or 0)
        while True:
            chunk = resp.read(CHUNK_SIZE)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)

            now = time.time()
            if now - last_report >= 5:
                if total:
                    pct = (downloaded / total) * 100
                    log.info("%.1f%% (%d / %d MB)",
                             pct, downloaded // (1024**2), total // (1024**2))
                else:
                    log.info("%d MB downloaded", downloaded // (1024**2))
                last_report = now

    log.info("Download complete: %d MB", downloaded // (1024**2))
    return dest


def verify_checksum(path: Path, expected_sha256: str) -> bool:
    """Verify SHA-256 checksum of a file."""
    log.info("Verifying checksum...")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected_sha256:
        log.error("Checksum mismatch: expected %s, got %s",
                  expected_sha256, actual)
        return False
    log.info("Checksum verified")
    return True


def extract_snapshot(archive: Path, datadir: Path):
    """Extract a snapshot archive into the data directory."""
    log.info("Extracting snapshot to %s", datadir)
    datadir.mkdir(parents=True, exist_ok=True)

    if archive.name.endswith(".tar.zst") or archive.name.endswith(".zst"):
        zstd = shutil.which("zstd")
        if not zstd:
            log.error("zstd not found — install zstd to extract .tar.zst snapshots")
            sys.exit(1)
        tar_path = archive.with_suffix("")
        subprocess.run(
            ["zstd", "-d", str(archive), "-o", str(tar_path)],
            check=True
        )
        with tarfile.open(tar_path) as tf:
            tf.extractall(path=str(datadir))
        tar_path.unlink()
    elif archive.name.endswith(".tar.gz") or archive.name.endswith(".tgz"):
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(path=str(datadir))
    elif archive.name.endswith(".tar"):
        with tarfile.open(archive) as tf:
            tf.extractall(path=str(datadir))
    else:
        log.error("Unknown archive format: %s", archive.name)
        sys.exit(1)

    log.info("Extraction complete")


def fetch_snapshot_index(index_url: str) -> dict:
    """Fetch the snapshot index from the server."""
    try:
        req = urllib.request.Request(index_url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.error("Cannot fetch snapshot index: %s", e)
        sys.exit(1)


def load_utxo_snapshot(datadir: Path, snapshot_path: Path, rpc_port: int = 8332):
    """Load a UTXO snapshot via the loadtxoutset RPC."""
    log.info("Loading UTXO snapshot via RPC...")
    from waved import RPCClient
    rpc = RPCClient(
        url=f"http://127.0.0.1:{rpc_port}",
        cookie_path=datadir / ".cookie",
    )
    try:
        result = rpc.call("loadtxoutset", [str(snapshot_path)])
        log.info("UTXO snapshot loaded: %s", result)
    except Exception as e:
        log.error("Failed to load UTXO snapshot: %s", e)
        log.info("You can load it manually: bitcoin-cli loadtxoutset %s",
                 snapshot_path)


def main():
    parser = argparse.ArgumentParser(
        description="Bitcoin Wave snapshot bootstrapper"
    )
    parser.add_argument(
        "--datadir", type=Path,
        default=Path.home() / ".bitcoin",
        help="Bitcoin data directory",
    )
    parser.add_argument(
        "--type", choices=["blocks", "utxo"], default="blocks",
        help="Snapshot type: 'blocks' (full block data) or 'utxo' (assumeutxo)",
    )
    parser.add_argument(
        "--url", default=None,
        help="Direct URL to snapshot archive (skips index lookup)",
    )
    parser.add_argument(
        "--index-url", default=DEFAULT_SNAPSHOT_INDEX,
        help=f"Snapshot index URL (default: {DEFAULT_SNAPSHOT_INDEX})",
    )
    parser.add_argument(
        "--checksum", default=None,
        help="Expected SHA-256 checksum (hex)",
    )
    parser.add_argument(
        "--keep-archive", action="store_true",
        help="Keep the downloaded archive after extraction",
    )
    parser.add_argument(
        "--rpc-port", type=int, default=8332,
        help="bitcoind RPC port (for UTXO snapshot loading)",
    )
    parser.add_argument(
        "--version", action="version", version=f"wave-snapshot {VERSION}",
    )
    args = parser.parse_args()

    if args.url:
        url = args.url
        checksum = args.checksum
    else:
        log.info("Fetching snapshot index from %s", args.index_url)
        index = fetch_snapshot_index(args.index_url)
        snapshots = index.get("snapshots", {}).get(args.type, [])
        if not snapshots:
            log.error("No %s snapshots available", args.type)
            sys.exit(1)
        latest = snapshots[0]
        url = latest["url"]
        checksum = latest.get("sha256", args.checksum)
        log.info("Latest %s snapshot: height %s, size %s MB",
                 args.type, latest.get("height", "?"),
                 latest.get("size_mb", "?"))

    filename = url.rsplit("/", 1)[-1]
    archive_path = args.datadir / filename

    args.datadir.mkdir(parents=True, exist_ok=True)

    download_with_progress(url, archive_path)

    if checksum:
        if not verify_checksum(archive_path, checksum):
            log.error("Snapshot verification failed — aborting")
            archive_path.unlink()
            sys.exit(1)

    if args.type == "utxo":
        load_utxo_snapshot(args.datadir, archive_path, args.rpc_port)
    else:
        extract_snapshot(archive_path, args.datadir)

    if not args.keep_archive:
        archive_path.unlink(missing_ok=True)
        log.info("Archive removed")

    log.info("Bootstrap complete")


if __name__ == "__main__":
    main()
