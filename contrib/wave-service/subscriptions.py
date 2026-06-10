#!/usr/bin/env python3
"""
Bitcoin Wave subscription management.

Handles user registration, subscription tokens, and Stripe webhook
processing. Manages the lifecycle of user subscriptions.

Usage:
  # Create a subscription token (admin)
  python3 subscriptions.py create --tier node --email user@example.com

  # Run the webhook server (receives Stripe events)
  python3 subscriptions.py serve --stripe-secret whsec_...

  # List active subscriptions
  python3 subscriptions.py list
"""

import argparse
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

VERSION = "0.1.0"
DEFAULT_DB = Path("wave-usage.db")
DEFAULT_WEBHOOK_PORT = 8391

TIER_PRICES = {
    "node": 500,       # $5/month in cents
    "investor": 2000,  # $20/month
    "trader": 5000,    # $50/month
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [subscriptions] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("subscriptions")


class SubscriptionManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path
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
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_token TEXT NOT NULL,
                    email TEXT,
                    stripe_customer_id TEXT,
                    stripe_subscription_id TEXT,
                    tier TEXT NOT NULL,
                    status TEXT DEFAULT 'active',
                    created_at REAL NOT NULL,
                    cancelled_at REAL,
                    FOREIGN KEY (user_token) REFERENCES users(token)
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

    def create_subscription(self, tier: str, email: str = "",
                            stripe_customer_id: str = "",
                            stripe_subscription_id: str = "") -> str:
        if tier not in TIER_PRICES:
            raise ValueError(f"Unknown tier: {tier}")

        token = f"wave_{secrets.token_hex(24)}"
        now = time.time()

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO users VALUES (?, ?, ?, 1)",
                (token, tier, now)
            )
            conn.execute(
                "INSERT INTO subscriptions (user_token, email, stripe_customer_id, stripe_subscription_id, tier, status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (token, email, stripe_customer_id, stripe_subscription_id, tier, now)
            )

        log.info("Created %s subscription for %s: token=%s...",
                 tier, email or "(no email)", token[:16])
        return token

    def cancel_subscription(self, stripe_subscription_id: str):
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT user_token FROM subscriptions WHERE stripe_subscription_id = ?",
                (stripe_subscription_id,)
            ).fetchone()
            if not row:
                log.warning("Unknown subscription: %s", stripe_subscription_id)
                return

            conn.execute(
                "UPDATE subscriptions SET status = 'cancelled', cancelled_at = ? WHERE stripe_subscription_id = ?",
                (time.time(), stripe_subscription_id)
            )
            conn.execute(
                "UPDATE users SET active = 0 WHERE token = ?",
                (row[0],)
            )
            log.info("Cancelled subscription: %s", stripe_subscription_id)

    def update_tier(self, stripe_subscription_id: str, new_tier: str):
        if new_tier not in TIER_PRICES:
            raise ValueError(f"Unknown tier: {new_tier}")
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT user_token FROM subscriptions WHERE stripe_subscription_id = ?",
                (stripe_subscription_id,)
            ).fetchone()
            if not row:
                log.warning("Unknown subscription: %s", stripe_subscription_id)
                return
            conn.execute(
                "UPDATE subscriptions SET tier = ? WHERE stripe_subscription_id = ?",
                (new_tier, stripe_subscription_id)
            )
            conn.execute(
                "UPDATE users SET tier = ? WHERE token = ?",
                (new_tier, row[0])
            )
            log.info("Updated tier to %s: %s", new_tier, stripe_subscription_id)

    def list_active(self) -> list[dict]:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT s.email, s.tier, s.status, u.token, s.created_at
                FROM subscriptions s
                JOIN users u ON s.user_token = u.token
                WHERE u.active = 1
                ORDER BY s.created_at DESC
            """).fetchall()
            return [dict(r) for r in rows]


def verify_stripe_signature(payload: bytes, sig_header: str,
                            secret: str) -> bool:
    """Verify Stripe webhook signature."""
    parts = dict(
        item.split("=", 1) for item in sig_header.split(",") if "=" in item
    )
    timestamp = parts.get("t", "")
    v1_sig = parts.get("v1", "")

    signed_payload = f"{timestamp}.{payload.decode()}".encode()
    expected = hmac.new(
        secret.encode(), signed_payload, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, v1_sig)


def run_webhook_server(manager: SubscriptionManager, stripe_secret: str,
                       port: int):
    """Run Stripe webhook handler."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/webhook/stripe":
                self.send_response(404)
                self.end_headers()
                return

            content_length = int(self.headers.get("Content-Length", 0))
            payload = self.rfile.read(content_length)

            sig = self.headers.get("Stripe-Signature", "")
            if not verify_stripe_signature(payload, sig, stripe_secret):
                log.warning("Invalid Stripe signature")
                self.send_response(400)
                self.end_headers()
                return

            event = json.loads(payload)
            event_type = event.get("type", "")

            if event_type == "checkout.session.completed":
                session = event["data"]["object"]
                tier = session.get("metadata", {}).get("tier", "node")
                email = session.get("customer_email", "")
                customer_id = session.get("customer", "")
                sub_id = session.get("subscription", "")
                token = manager.create_subscription(
                    tier, email, customer_id, sub_id
                )
                log.info("New subscription via Stripe: %s (%s)", email, tier)

            elif event_type == "customer.subscription.deleted":
                sub = event["data"]["object"]
                manager.cancel_subscription(sub["id"])

            elif event_type == "customer.subscription.updated":
                sub = event["data"]["object"]
                tier_map = {v: k for k, v in TIER_PRICES.items()}
                amount = sub.get("items", {}).get("data", [{}])[0].get(
                    "price", {}
                ).get("unit_amount", 0)
                new_tier = tier_map.get(amount)
                if new_tier:
                    manager.update_tier(sub["id"], new_tier)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"received": true}')

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    log.info("Stripe webhook server on port %d", port)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Bitcoin Wave subscriptions")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command")

    create = sub.add_parser("create", help="Create a subscription")
    create.add_argument("--tier", required=True, choices=TIER_PRICES.keys())
    create.add_argument("--email", default="")

    sub.add_parser("list", help="List active subscriptions")

    serve = sub.add_parser("serve", help="Run Stripe webhook server")
    serve.add_argument("--stripe-secret", required=True)
    serve.add_argument("--port", type=int, default=DEFAULT_WEBHOOK_PORT)

    args = parser.parse_args()
    manager = SubscriptionManager(args.db)

    if args.command == "create":
        token = manager.create_subscription(args.tier, args.email)
        print(f"Token: {token}")
        print(f"Tier: {args.tier} (${TIER_PRICES[args.tier] / 100}/month)")

    elif args.command == "list":
        subs = manager.list_active()
        if not subs:
            print("No active subscriptions")
        for s in subs:
            print(f"  {s['email'] or '(no email)'} — {s['tier']} — token: {s['token'][:16]}...")

    elif args.command == "serve":
        run_webhook_server(manager, args.stripe_secret, args.port)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
