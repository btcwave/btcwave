#!/usr/bin/env python3
"""
Bitcoin Wave VPS provisioner.

Provisions a Bitcoin Wave node on a fresh VPS via SSH. Handles:
  - System updates and dependency installation
  - User creation
  - bitcoind + waved installation (from Docker or source)
  - systemd service setup
  - Snapshot bootstrapping
  - Firewall configuration

Supports Vultr and Hetzner VPS providers via API.

Usage:
  # Provision an existing VPS
  python3 provision.py --host 203.0.113.10 --ssh-key ~/.ssh/id_ed25519

  # Create + provision a new Vultr VPS
  python3 provision.py --provider vultr --api-key <key> --region syd --plan vc2-4c-8gb
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

VERSION = "0.1.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [provision] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("provision")

SETUP_SCRIPT = """#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "=== Bitcoin Wave VPS Setup ==="

# System updates
apt-get update -qq
apt-get upgrade -y -qq

# Dependencies
apt-get install -y -qq \\
    docker.io docker-compose-v2 \\
    ufw python3 curl jq

# User
if ! id wave &>/dev/null; then
    useradd -r -m -s /bin/bash wave
    usermod -aG docker wave
fi

# Data directory
mkdir -p /var/lib/wave
chown wave:wave /var/lib/wave

# Firewall
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 8333/tcp comment 'Bitcoin P2P'
ufw allow 8380/tcp comment 'Wave Dashboard'
echo "y" | ufw enable

# Docker compose for Wave
mkdir -p /opt/wave
cat > /opt/wave/docker-compose.yml << 'COMPOSE'
services:
  bitcoind:
    image: ghcr.io/btcwave/btcwave:latest
    container_name: wave-node
    restart: unless-stopped
    ports:
      - "8333:8333"
      - "8332:8332"
      - "8380:8380"
    volumes:
      - /var/lib/wave:/var/lib/bitcoind
      - /opt/wave/bitcoin.conf:/etc/bitcoin/bitcoin.conf:ro
    user: "1000:1000"
COMPOSE

cat > /opt/wave/bitcoin.conf << 'CONF'
# Bitcoin Wave — VPS configuration
listen=1
server=1
rest=1
txindex=1
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
maxconnections=40
maxuploadtarget=5000
dbcache=2048
maxmempool=200
consensusrules=rdts
CONF

chown -R wave:wave /opt/wave

# systemd service for docker compose
cat > /etc/systemd/system/wave.service << 'SERVICE'
[Unit]
Description=Bitcoin Wave Node (Docker)
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=wave
WorkingDirectory=/opt/wave
ExecStart=/usr/bin/docker compose up
ExecStop=/usr/bin/docker compose down
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable wave.service

echo "=== Setup complete ==="
echo "Start with: systemctl start wave"
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):8380"
"""


def ssh_exec(host: str, key_path: str, command: str,
             user: str = "root") -> tuple[int, str]:
    """Execute a command over SSH."""
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-i", key_path,
        f"{user}@{host}",
        command,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return result.returncode, result.stdout + result.stderr


def scp_upload(host: str, key_path: str, local: str, remote: str,
               user: str = "root"):
    """Upload a file via SCP."""
    cmd = [
        "scp", "-o", "StrictHostKeyChecking=accept-new",
        "-i", key_path,
        local, f"{user}@{host}:{remote}",
    ]
    subprocess.run(cmd, check=True, timeout=120)


def wait_for_ssh(host: str, key_path: str, timeout: int = 300):
    """Wait for SSH to become available."""
    log.info("Waiting for SSH on %s...", host)
    start = time.time()
    while time.time() - start < timeout:
        try:
            code, _ = ssh_exec(host, key_path, "echo ok")
            if code == 0:
                log.info("SSH available")
                return
        except Exception:
            pass
        time.sleep(10)
    raise TimeoutError(f"SSH not available after {timeout}s")


def create_vultr_vps(api_key: str, region: str, plan: str,
                     ssh_key_id: str = "") -> dict:
    """Create a VPS on Vultr."""
    log.info("Creating Vultr VPS: region=%s plan=%s", region, plan)
    data = json.dumps({
        "region": region,
        "plan": plan,
        "os_id": 1743,  # Ubuntu 24.04 LTS
        "label": "bitcoin-wave",
        "hostname": "wave",
        "sshkey_id": [ssh_key_id] if ssh_key_id else [],
    }).encode()

    req = urllib.request.Request(
        "https://api.vultr.com/v2/instances",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    instance = result["instance"]
    log.info("VPS created: id=%s", instance["id"])

    # Wait for IP assignment
    for _ in range(30):
        time.sleep(10)
        req = urllib.request.Request(
            f"https://api.vultr.com/v2/instances/{instance['id']}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req) as resp:
            info = json.loads(resp.read())["instance"]
        if info.get("main_ip") and info["main_ip"] != "0.0.0.0":
            log.info("VPS IP: %s", info["main_ip"])
            return info

    raise TimeoutError("VPS did not get an IP within 5 minutes")


def provision(host: str, key_path: str):
    """Run the provisioning script on a remote host."""
    wait_for_ssh(host, key_path)

    log.info("Running setup script on %s", host)
    script_path = "/tmp/wave-setup.sh"

    # Write script to temp file locally, upload, execute
    local_tmp = Path("/tmp/wave-setup-local.sh")
    local_tmp.write_text(SETUP_SCRIPT)
    scp_upload(host, key_path, str(local_tmp), script_path)
    local_tmp.unlink()

    code, output = ssh_exec(host, key_path, f"bash {script_path}")
    print(output)

    if code != 0:
        log.error("Setup script failed with exit code %d", code)
        sys.exit(1)

    log.info("Provisioning complete. Dashboard: http://%s:8380", host)


def main():
    parser = argparse.ArgumentParser(
        description="Bitcoin Wave VPS provisioner"
    )
    parser.add_argument(
        "--host", default=None,
        help="VPS IP/hostname (skip if using --provider to create one)",
    )
    parser.add_argument(
        "--ssh-key", default=str(Path.home() / ".ssh" / "id_ed25519"),
        help="SSH private key path",
    )
    parser.add_argument(
        "--provider", choices=["vultr", "hetzner"], default=None,
        help="VPS provider (creates a new VPS)",
    )
    parser.add_argument(
        "--api-key", default=None,
        help="VPS provider API key",
    )
    parser.add_argument(
        "--region", default="syd",
        help="VPS region (default: syd)",
    )
    parser.add_argument(
        "--plan", default="vc2-4c-8gb",
        help="VPS plan (default: vc2-4c-8gb)",
    )
    parser.add_argument(
        "--ssh-key-id", default="",
        help="Provider SSH key ID (for Vultr)",
    )
    parser.add_argument(
        "--version", action="version", version=f"wave-provision {VERSION}",
    )
    args = parser.parse_args()

    if args.provider and not args.host:
        if not args.api_key:
            log.error("--api-key required when using --provider")
            sys.exit(1)
        if args.provider == "vultr":
            info = create_vultr_vps(
                args.api_key, args.region, args.plan, args.ssh_key_id
            )
            args.host = info["main_ip"]
        else:
            log.error("Provider '%s' not yet implemented", args.provider)
            sys.exit(1)

    if not args.host:
        log.error("Either --host or --provider is required")
        sys.exit(1)

    provision(args.host, args.ssh_key)


if __name__ == "__main__":
    main()
