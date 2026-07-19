"""The scheduled daily digest — routed to by api/index.py (see that module's
docstring for why this isn't its own Vercel Function).

Invoked by Vercel Cron per the schedule in vercel.json (UTC-only; the actual
local-time reasoning for the schedule lives there, not in this module — see
its comment). Authenticated via Vercel's own CRON_SECRET convention: set
CRON_SECRET as a project Environment Variable and Vercel automatically sends
it as ``Authorization: Bearer <CRON_SECRET>`` on every cron invocation —
this rejects anyone else who finds the URL and GETs it directly.
"""

from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.config import BotConfig  # noqa: E402
from bot.digest import run_digest  # noqa: E402
from bot.interests import load_interests  # noqa: E402
from bot.providers import make_llm  # noqa: E402
from bot.surfcall_tools import build_nyt_tools  # noqa: E402
from bot.telegram_api import send_chunked  # noqa: E402


def _run_and_send(cfg: BotConfig) -> None:
    """Generate the digest and push it, or do nothing if there's no chat ID
    yet (before the first /start) — never an error, just nothing to send to."""
    if not cfg.telegram_chat_id:
        return
    llm = make_llm(cfg.anthropic_api_key)
    tools = build_nyt_tools(nyt_api_key=cfg.nyt_api_key, mode=cfg.mode)
    digest_text = run_digest(
        tools=tools, llm=llm, model=cfg.model, interests=load_interests()
    )
    send_chunked(token=cfg.telegram_token, chat_id=cfg.telegram_chat_id, text=digest_text)


def handle_cron(auth_header: str | None) -> int:
    """Process one Vercel Cron GET; returns the HTTP status to reply with."""
    cron_secret = os.environ.get("CRON_SECRET", "")
    # Constant-time comparison (see api/webhook.py's secret check for why).
    if (
        not cron_secret
        or auth_header is None
        or not hmac.compare_digest(auth_header.encode(), f"Bearer {cron_secret}".encode())
    ):
        return 401
    _run_and_send(BotConfig.from_env())
    return 200
