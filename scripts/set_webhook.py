"""One-time (or per-redeploy-URL) setup: register the Vercel deployment's
webhook URL with Telegram.

Usage:
    uv run python scripts/set_webhook.py https://<your-app>.vercel.app

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET from the environment
(via bot/config.py's .env loading) — the secret must be the SAME value set
as TELEGRAM_WEBHOOK_SECRET in Vercel's project Environment Variables, since
Telegram echoes it back on every webhook call and api/webhook.py verifies it
against that.
"""

from __future__ import annotations

import sys

from bot.config import BotConfig
from bot.telegram_api import set_webhook


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: uv run python scripts/set_webhook.py https://<your-app>.vercel.app")
        raise SystemExit(1)
    base_url = sys.argv[1].rstrip("/")
    webhook_url = f"{base_url}/api/webhook"

    cfg = BotConfig.from_env()
    result = set_webhook(
        token=cfg.telegram_token,
        url=webhook_url,
        secret_token=cfg.telegram_webhook_secret,
    )
    print(f"Webhook set to {webhook_url}")
    print(result)


if __name__ == "__main__":
    main()
