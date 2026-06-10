Pi Image Builder
================

Builds a flashable Raspberry Pi image with Bitcoin Wave pre-installed.

Target hardware: Raspberry Pi 5 (4GB+ RAM recommended, 1TB+ SSD via USB).

What's included
---------------

- Raspberry Pi OS Lite (64-bit, headless)
- Bitcoin Wave (bitcoind + bitcoin-cli)
- Wave daemon (waved) — auto-starts on boot via systemd
- Pre-configured for first-boot blockchain sync from snapshot
- SSH enabled, default user `wave`

Building
--------

```
./build-image.sh
```

Requires: Docker (for cross-compilation), qemu-user-static.

Output: `bitcoin-wave-pi5.img.gz` — flash to SD card or SSD with
Raspberry Pi Imager or `dd`.

First boot
----------

1. Flash the image to an SSD (SD card works but SSD strongly recommended)
2. Connect SSD to Pi 5 via USB 3
3. Power on — the node will start syncing automatically
4. Access the dashboard at `http://<pi-ip>:8380/status`

The initial blockchain sync takes 12-24 hours from snapshot,
or several days from genesis. Snapshot URL is configured in
`/etc/wave/wave.conf`.
