"""Vercel serverless function — Telegram webhook endpoint.

Telegram POSTs one update here per incoming message (see
scripts/set_webhook.py for registering this URL). Each invocation handles
exactly one update — no persistent process, no event loop, and (per
GeckoVision/ayuda-venezuela-bot's own Vercel webhook, app/api/telegram/
route.ts) a rate limiter kept as a plain MODULE-LEVEL object rather than
dropped: Vercel reuses a "warm" function instance across nearby invocations,
so this in-memory state persists there and gives real (if best-effort, not
guaranteed across cold starts or scaled-out concurrent instances)
protection — better than nothing, same tradeoff the reference bot accepted.

Security note: long-polling (the previous deployment target) never exposed a
public endpoint at all — Telegram was only ever pulled from, never pushed to.
A webhook is a real, new attack surface (anyone can POST to this URL), so
every request is verified via the ``X-Telegram-Bot-Api-Secret-Token`` header
Telegram echoes back (set during ``setWebhook``, see telegram_api.py) before
the body is trusted at all.
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot import agent  # noqa: E402
from bot.bot import WELCOME_MSG, RateLimiter, handle_command, handle_message  # noqa: E402
from bot.config import SYSTEM_PROMPT, BotConfig  # noqa: E402
from bot.interests import load_interests  # noqa: E402
from bot.providers import make_llm  # noqa: E402
from bot.surfcall_tools import build_nyt_tools  # noqa: E402
from bot.telegram_api import send_chunked  # noqa: E402

_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

# Module-level (not per-request) so it persists across warm invocations — see
# module docstring. 8/min matches the reference bot's PER_CHAT_PER_MIN.
_LIMITER = RateLimiter(max_per_min=8)


def _system_prompt() -> str:
    return f"{SYSTEM_PROMPT}\n\nReader's interests profile:\n{load_interests()}"


def _reply_for(
    update: dict, cfg: BotConfig, *, limiter: RateLimiter | None = _LIMITER
) -> tuple[str, str] | None:
    """Returns (chat_id, reply_text), or None if there's nothing to reply to
    (not a text message — an edit, a channel post, a photo with no caption,
    etc.)."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    text = message.get("text")
    if chat_id is None or user_id is None or not text:
        return None

    # NYT only — RSS (crypto) has no query capability an interactive agent
    # could use, so it only feeds the scheduled digest (api/cron.py), not
    # this on-demand path. See bot/rss.py's docstring.
    llm = make_llm(cfg.anthropic_api_key)
    tools = build_nyt_tools(nyt_api_key=cfg.nyt_api_key, mode=cfg.mode)

    def responder(t: str) -> str:
        return agent.respond(
            t,
            llm=llm,
            tools=tools,
            model=cfg.model,
            system=_system_prompt(),
            max_tokens=cfg.max_tokens,
            max_iters=cfg.max_iters,
        )

    if text.startswith("/"):
        command = text.split(maxsplit=1)[0]
        if command.lstrip("/").split("@")[0].lower() == "start":
            # WELCOME_MSG ends with a prompt for the chat ID — append it here
            # rather than in bot.bot, since only the transport layer knows it.
            return str(chat_id), f"{WELCOME_MSG}\n{chat_id}"
        reply = handle_command(
            command, user_id, responder=responder, limiter=limiter, now=time.monotonic()
        )
    else:
        reply = handle_message(
            text, user_id, responder=responder, limiter=limiter, now=time.monotonic()
        )
    return str(chat_id), reply


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        cfg = BotConfig.from_env()

        if self.headers.get(_SECRET_HEADER) != cfg.telegram_webhook_secret:
            self.send_response(403)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            update = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        result = _reply_for(update, cfg)
        if result is not None:
            chat_id, reply = result
            send_chunked(token=cfg.telegram_token, chat_id=chat_id, text=reply)

        # Always 200 to Telegram once parsed — a slow/failed downstream call
        # (LLM, NYT) shouldn't make Telegram retry-storm this endpoint.
        self.send_response(200)
        self.end_headers()
