#!/usr/bin/env python3
"""
Bitcoin Wave chat agent proxy.

Sits between the dashboard and Anthropic's API. Users never see API keys.
Enforces subscription tiers, usage limits, and token metering.

The proxy:
  - Authenticates users via subscription tokens
  - Injects the Bitcoin-specialist system prompt
  - Forwards requests to Anthropic API
  - Tracks token usage per user per billing period
  - Enforces tier-based message limits and capabilities

Tiers:
  $5/month  — Node management only. ~20 included messages (node ops).
  $20/month — + long-term Bitcoin investment strategy.
  $50/month — + active trading support, market analysis.
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock

VERSION = "0.1.0"
DEFAULT_PORT = 8390
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-6"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [chat-proxy] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("chat-proxy")


@dataclass
class Tier:
    name: str
    monthly_messages: int
    capabilities: list[str]
    model: str
    max_tokens: int


TIERS = {
    "node": Tier(
        name="Node Management",
        monthly_messages=20,
        capabilities=["node_ops"],
        model=DEFAULT_MODEL,
        max_tokens=1024,
    ),
    "investor": Tier(
        name="Investment Strategy",
        monthly_messages=100,
        capabilities=["node_ops", "investment", "onchain_analytics"],
        model=DEFAULT_MODEL,
        max_tokens=2048,
    ),
    "trader": Tier(
        name="Trading Support",
        monthly_messages=500,
        capabilities=["node_ops", "investment", "onchain_analytics",
                       "trading", "market_analysis", "altcoin_research"],
        model=DEFAULT_MODEL,
        max_tokens=4096,
    ),
}

SYSTEM_PROMPTS = {
    "node_ops": """You are the Bitcoin Wave node assistant. You help users manage their Bitcoin Wave node.

You can help with:
- Node status and health monitoring
- Configuration changes (peer count, mempool size, bandwidth limits)
- Troubleshooting connectivity or sync issues
- Explaining Bitcoin network concepts
- Understanding block data and transaction status

You always recommend Bitcoin Wave defaults unless the user has a specific reason to change them.
You support BIP-110 and the data integrity position — Bitcoin is for money, not arbitrary data storage.

Keep responses concise and actionable. You're talking to node operators, not academics.""",

    "investment": """You are the Bitcoin Wave investment advisor. You help users with long-term Bitcoin investment strategy.

In addition to node management, you can help with:
- Dollar-cost averaging (DCA) strategies
- UTXO management and consolidation
- Fee estimation and transaction timing
- On-chain analytics (hash rate, difficulty, supply metrics)
- Cold storage best practices
- Tax-aware accumulation strategies

You are Bitcoin-only in your recommendations. You do not recommend altcoins at this tier.
You favour long-term holding and systematic accumulation over trading.
You never give specific price predictions — you discuss frameworks and metrics instead.""",

    "trading": """You are the Bitcoin Wave trading assistant. You help users with active market analysis and trading.

In addition to node management and investment strategy, you can help with:
- Market analysis (technical and fundamental)
- Altcoin research and evaluation (always framed relative to BTC)
- Portfolio tracking and rebalancing suggestions
- Exchange and liquidity analysis
- Risk management frameworks
- DeFi and yield strategies (with appropriate risk warnings)

You always frame altcoin discussion in terms of BTC-denominated returns.
You are direct about risks. You never hype. You flag when you're uncertain.
You remind users that most altcoins underperform BTC over full cycles.""",
}


class UsageDB:
    """SQLite-backed usage tracking."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    token TEXT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    active INTEGER DEFAULT 1
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_token TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    billing_period TEXT NOT NULL,
                    FOREIGN KEY (user_token) REFERENCES users(token)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_usage_period
                ON usage(user_token, billing_period)
            """)

    def add_user(self, token: str, tier: str):
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO users VALUES (?, ?, ?, 1)",
                    (token, tier, time.time())
                )

    def get_user(self, token: str) -> dict | None:
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                row = conn.execute(
                    "SELECT token, tier, active FROM users WHERE token = ?",
                    (token,)
                ).fetchone()
                if row:
                    return {"token": row[0], "tier": row[1], "active": bool(row[2])}
        return None

    def record_usage(self, token: str, input_tokens: int, output_tokens: int):
        period = time.strftime("%Y-%m")
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    "INSERT INTO usage (user_token, timestamp, input_tokens, output_tokens, billing_period) VALUES (?, ?, ?, ?, ?)",
                    (token, time.time(), input_tokens, output_tokens, period)
                )

    def get_period_message_count(self, token: str) -> int:
        period = time.strftime("%Y-%m")
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM usage WHERE user_token = ? AND billing_period = ?",
                    (token, period)
                ).fetchone()
                return row[0] if row else 0

    def get_period_tokens(self, token: str) -> dict:
        period = time.strftime("%Y-%m")
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM usage WHERE user_token = ? AND billing_period = ?",
                    (token, period)
                ).fetchone()
                return {"input_tokens": row[0], "output_tokens": row[1]}


class ChatProxy:
    """Proxy between dashboard and Anthropic API."""

    def __init__(self, anthropic_key: str, db_path: Path,
                 port: int = DEFAULT_PORT, waved_url: str = ""):
        self.anthropic_key = anthropic_key
        self.db = UsageDB(db_path)
        self.port = port
        self.waved_url = waved_url

    def _build_system_prompt(self, tier: Tier, node_status: dict | None = None) -> str:
        parts = []
        if "node_ops" in tier.capabilities:
            parts.append(SYSTEM_PROMPTS["node_ops"])
        if "investment" in tier.capabilities:
            parts.append(SYSTEM_PROMPTS["investment"])
        if "trading" in tier.capabilities:
            parts.append(SYSTEM_PROMPTS["trading"])

        prompt = "\n\n---\n\n".join(parts)

        if node_status:
            prompt += f"""

---

Current node status (live data from waved):
- Block height: {node_status.get('block_height', 'unknown')}
- Sync progress: {node_status.get('sync_progress', 0):.4%}
- Peers: {node_status.get('peer_count', 0)}
- Mempool: {node_status.get('mempool_size', 0)} transactions
- Network: {node_status.get('network', 'unknown')}
- Uptime: {node_status.get('uptime_seconds', 0)} seconds"""

        return prompt

    def _get_node_status(self) -> dict | None:
        if not self.waved_url:
            return None
        try:
            req = urllib.request.Request(f"{self.waved_url}/status")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    def _call_anthropic(self, messages: list, system: str,
                        tier: Tier) -> dict:
        payload = json.dumps({
            "model": tier.model,
            "max_tokens": tier.max_tokens,
            "system": system,
            "messages": messages,
        }).encode()

        req = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.anthropic_key,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    def start(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type, Authorization")

            def _json_response(self, code, data):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def _get_auth_token(self) -> str | None:
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    return auth[7:]
                return None

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):
                if self.path == "/usage":
                    token = self._get_auth_token()
                    if not token:
                        self._json_response(401, {"error": "missing auth token"})
                        return
                    user = proxy.db.get_user(token)
                    if not user:
                        self._json_response(401, {"error": "invalid token"})
                        return
                    tier = TIERS.get(user["tier"])
                    count = proxy.db.get_period_message_count(token)
                    tokens = proxy.db.get_period_tokens(token)
                    self._json_response(200, {
                        "tier": user["tier"],
                        "tier_name": tier.name if tier else "unknown",
                        "messages_used": count,
                        "messages_limit": tier.monthly_messages if tier else 0,
                        "tokens": tokens,
                        "billing_period": time.strftime("%Y-%m"),
                    })
                else:
                    self._json_response(404, {"error": "not found"})

            def do_POST(self):
                if self.path != "/chat":
                    self._json_response(404, {"error": "not found"})
                    return

                token = self._get_auth_token()
                if not token:
                    self._json_response(401, {"error": "missing auth token"})
                    return

                user = proxy.db.get_user(token)
                if not user or not user["active"]:
                    self._json_response(401, {"error": "invalid or inactive subscription"})
                    return

                tier = TIERS.get(user["tier"])
                if not tier:
                    self._json_response(500, {"error": "unknown tier"})
                    return

                msg_count = proxy.db.get_period_message_count(token)
                if msg_count >= tier.monthly_messages:
                    self._json_response(429, {
                        "error": "monthly message limit reached",
                        "limit": tier.monthly_messages,
                        "used": msg_count,
                        "upgrade_url": "https://btcwave.app/upgrade",
                    })
                    return

                content_length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(content_length))
                messages = body.get("messages", [])

                if not messages:
                    self._json_response(400, {"error": "no messages provided"})
                    return

                node_status = proxy._get_node_status()
                system = proxy._build_system_prompt(tier, node_status)

                try:
                    result = proxy._call_anthropic(messages, system, tier)
                    input_tokens = result.get("usage", {}).get("input_tokens", 0)
                    output_tokens = result.get("usage", {}).get("output_tokens", 0)
                    proxy.db.record_usage(token, input_tokens, output_tokens)

                    self._json_response(200, {
                        "response": result.get("content", []),
                        "usage": {
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "messages_remaining": tier.monthly_messages - msg_count - 1,
                        },
                    })
                except urllib.error.HTTPError as e:
                    error_body = e.read().decode()
                    log.error("Anthropic API error: %s %s", e.code, error_body)
                    self._json_response(502, {"error": "upstream API error"})
                except Exception as e:
                    log.error("Chat request failed: %s", e)
                    self._json_response(500, {"error": "internal error"})

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", self.port), Handler)
        log.info("Chat proxy listening on 127.0.0.1:%d", self.port)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(
        description="Bitcoin Wave chat agent proxy"
    )
    parser.add_argument(
        "--anthropic-key", required=True,
        help="Anthropic API key",
    )
    parser.add_argument(
        "--db", type=Path, default=Path("wave-usage.db"),
        help="Usage database path (default: wave-usage.db)",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Proxy port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--waved-url", default="http://127.0.0.1:8380",
        help="waved API URL for node status injection",
    )
    parser.add_argument(
        "--version", action="version", version=f"wave-chat-proxy {VERSION}",
    )
    args = parser.parse_args()

    proxy = ChatProxy(
        anthropic_key=args.anthropic_key,
        db_path=args.db,
        port=args.port,
        waved_url=args.waved_url,
    )
    proxy.start()


if __name__ == "__main__":
    main()
