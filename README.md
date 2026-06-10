Bitcoin Wave
============

https://btcwave.app

Agent-native Bitcoin node implementation, forked from [Bitcoin Knots](https://github.com/bitcoinknots/bitcoin).

What is Bitcoin Wave?
---------------------

Bitcoin Wave is a Bitcoin full node implementation built for the agentic economy. It connects to the Bitcoin peer-to-peer network to download and fully validate blocks and transactions, with defaults optimised for programmatic management by software agents.

Bitcoin Wave inherits Bitcoin Knots' data integrity policies, including BIP-110 (RDTS) consensus enforcement — OP_RETURN outputs are hard-capped at 83 bytes at the consensus level.

### What's different from Knots?

- **RPC server enabled by default** — agents need API access out of the box
- **REST API enabled by default** — standard HTTP interface for tooling
- **Transaction index enabled by default** — full tx lookups without extra config
- **Wave daemon (waved)** — lightweight watchdog that monitors node health, reports status via local HTTP API, and provides the foundation for automated node management
- **Container-first** — Docker image bundles bitcoind + waved, ready to deploy

### What's the same as Knots?

Everything else. Consensus rules, networking, wallet, P2P protocol — all inherited from upstream Knots. Wave tracks Knots releases and merges upstream changes.

Wave Daemon
-----------

`waved` is a Python daemon that runs alongside bitcoind. It monitors the node via RPC and exposes a local HTTP API:

- `GET /status` — full node status (height, peers, sync progress, disk space)
- `GET /health` — simple health check (200 OK or 503)
- `GET /version` — waved and node versions

Default port: 8380 (localhost only).

```
waved --datadir ~/.bitcoin --api-port 8380
```

Building
--------

Same as Bitcoin Knots / Bitcoin Core:

```
cmake -B build -DRDTS_CONSENT=IMPLICIT
cmake --build build -j$(nproc)
```

Or use Docker:

```
docker build --build-arg RDTS_CONSENT=IMPLICIT -t btcwave -f contrib/docker/Dockerfile .
```

Further build documentation is in the [doc folder](/doc).

License
-------

Bitcoin Wave is released under the terms of the MIT license. See [COPYING](COPYING) for more information or see https://opensource.org/licenses/MIT.

Upstream
--------

Bitcoin Wave is a fork of [Bitcoin Knots](https://github.com/bitcoinknots/bitcoin), which is itself built on [Bitcoin Core](https://github.com/bitcoin/bitcoin). We track upstream Knots releases and merge changes for each release.
