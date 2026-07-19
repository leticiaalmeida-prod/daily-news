"""A minimal, synchronous Telegram Bot API client.

Replaces python-telegram-bot's Application/JobQueue machinery, which existed
for the long-polling deployment (Fly.io-style always-on process). On Vercel,
the bot is two stateless serverless functions (api/webhook.py, api/cron.py)
— no persistent event loop to run PTB's async Application in, and no
JobQueue to schedule against (Vercel Cron replaces that). All we actually
need from Telegram is "send a message" and "register the webhook URL", both
plain HTTPS calls — so this drops the dependency entirely rather than fight
an async framework inside a sync serverless handler.
"""

from __future__ import annotations

from typing import Any

import httpx

TELEGRAM_LIMIT = 4096
_API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramApiError(Exception):
    """A Telegram Bot API call failed — with the bot token REDACTED.

    httpx's own errors format the request URL into their message, and the
    Telegram API URL embeds the bot token (``/bot<token>/...``) — so letting
    an httpx error propagate out of a Vercel function would print the token
    straight into the deployment logs on any failed send. This error carries
    only the method name, the HTTP status, and Telegram's own (token-free)
    ``description`` field; it's raised ``from None`` so a logged traceback
    can't resurrect the original URL-bearing exception either.
    """


def _describe(resp: httpx.Response) -> str:
    """Telegram's own error `description` (e.g. "Bad Request: chat not
    found") — token-free by construction, capped defensively."""
    try:
        description = resp.json().get("description", "")
    except Exception:  # noqa: BLE001 - a non-JSON error body just has no detail
        return ""
    if not isinstance(description, str) or not description:
        return ""
    return f" — {description[:200]}"


def _post(method: str, token: str, payload: dict[str, Any], timeout: float) -> httpx.Response:
    """POST one Bot API call; any failure raises TelegramApiError (redacted,
    chain-suppressed) — never an httpx error whose message embeds the URL."""
    url = _API_BASE.format(token=token, method=method)
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise TelegramApiError(
            f"Telegram {method} failed: HTTP {exc.response.status_code}"
            f"{_describe(exc.response)}"
        ) from None
    except httpx.HTTPError as exc:
        # Transport-level failure (timeout, DNS, connect) — the exception TYPE
        # is the useful debugging signal; the message may embed the URL.
        raise TelegramApiError(
            f"Telegram {method} request failed: {type(exc).__name__}"
        ) from None
    return resp


def send_message(*, token: str, chat_id: str, text: str, timeout: float = 20.0) -> None:
    """Send one message. Raises TelegramApiError on failure — the caller (a
    Vercel function) surfaces that as a failed invocation, which is the right
    behavior here (better a visible error in Vercel's logs than a silently
    dropped message), and the error message never contains the bot token."""
    _post("sendMessage", token, {"chat_id": chat_id, "text": text}, timeout)


def send_chunked(*, token: str, chat_id: str, text: str) -> None:
    for chunk in chunk_message(text):
        send_message(token=token, chat_id=chat_id, text=chunk)


def set_webhook(
    *, token: str, url: str, secret_token: str, timeout: float = 20.0
) -> dict[str, Any]:
    """One-time (or per-redeploy-URL) setup call — see scripts/set_webhook.py.
    ``secret_token`` is echoed back by Telegram on every webhook POST as the
    ``X-Telegram-Bot-Api-Secret-Token`` header, which api/webhook.py verifies
    before trusting a request body — the one auth check long-polling never
    needed (no public endpoint to spoof in that model)."""
    resp = _post("setWebhook", token, {"url": url, "secret_token": secret_token}, timeout)
    return resp.json()


def chunk_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split a reply into Telegram-sized chunks (<= limit), preferring
    paragraph, then line, then word boundaries; hard-splits only as a last
    resort."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c]
