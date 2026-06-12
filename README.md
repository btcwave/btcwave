# btcwave-node

Bitcoin Wave node configuration profile for stock [Bitcoin Knots](https://bitcoinknots.org/).

Bitcoin Wave does **not** fork Bitcoin Knots. It ships a curated configuration profile that the `btcwave-cli` applies during guided setup. Your node runs unmodified Knots binaries.

## What's in the profile

| Category | Settings |
|---|---|
| **Core** | `server=1`, `txindex=1` (full index for Lightning + block explorer) |
| **Privacy** | Tor by default (`proxy`, `listenonion`, `torcontrol`) |
| **RPC** | Cookie auth, ZMQ for block/tx streaming, localhost-only binding |
| **Spam filtering** | Knots policy defaults — `datacarriersize=42`, no bare multisig, dust relay fee |
| **Resources** | Tuned for Raspberry Pi 4/5 (`maxconnections=40`, upload target 5GB/day) |
| **Consensus** | BIP-110/RDTS opt-in (commented out, enabled during guided setup) |

## Usage

The configuration template is at [`config/bitcoin.conf`](config/bitcoin.conf). The `btcwave-cli` reads this template and generates a machine-specific version with:

- Per-node `rpcauth` credentials
- LAN IP binding (if requested)
- Hardware-appropriate resource limits

You can also use the template directly — copy it to `~/.bitcoin/bitcoin.conf` and adjust the commented sections.

## Related repos

- [btcwave-cli](https://github.com/btcwave/btcwave-cli) — guided installer and node management
- [btcwave-dashboard](https://github.com/btcwave/btcwave-dashboard) — local web dashboard
- [btcwave-skill](https://github.com/btcwave/btcwave-skill) — Claude Code skill for node interaction

## License

MIT
