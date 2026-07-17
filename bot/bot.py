"""Telegram message/command logic — pure functions only.

Split so comprehension + agent logic (``digest``/``agent``/``surfcall_tools``)
stays decoupled from transport. Transport itself lives in ``api/webhook.py``
(incoming messages) and ``api/cron.py`` (the scheduled digest) — Vercel
serverless functions, not a long-running process, so there's no place here
for a persistent event loop or scheduler; see those modules and
``telegram_api.py`` for the actual wiring.

Every function here is pure (a responder + clock are injected) so it tests
offline with no network and no Telegram/Anthropic SDK involved.
"""

from __future__ import annotations

from collections.abc import Callable

from .agent import FALLBACK

RATE_LIMIT_MSG = (
    "You're sending requests too fast. Wait a few seconds and try again."
)
WELCOME_MSG = (
    "I'm your daily news bot. Every morning I'll send a digest of NYT + "
    "crypto RSS news (CoinDesk, The Block, Blockworks) filtered to your "
    "interests (see bot/interests.md). You can also ask me for news anytime "
    "with /news or plain language — that covers NYT live, though not the "
    "RSS crypto feeds (those only feed the scheduled digest).\n\n"
    "Your chat ID is below — set it as TELEGRAM_CHAT_ID so the daily digest "
    "knows where to send itself:"
)
HELP_MSG = (
    "Commands:\n"
    "/news — get an on-demand news check-in, filtered to your interests\n"
    "/help — this message\n\n"
    "Or just ask in plain language, e.g. \"anything new on AI regulation?\""
)
NEWS_QUERY = (
    "Give me the most relevant news right now, filtered to my stated interests."
)


class RateLimiter:
    """Per-user sliding-window limiter — protects LLM spend from a chatty user.

    api/webhook.py holds ONE instance at module level (not per-request) —
    same pattern as GeckoVision/ayuda-venezuela-bot's own Vercel webhook
    (app/api/telegram/route.ts's ``chatHits`` Map). Vercel reuses a "warm"
    function instance across nearby invocations, so this in-memory state
    persists there; it's best-effort, not guaranteed (a cold start or a
    scaled-out concurrent instance gets its own fresh state), but that's the
    same tradeoff the reference bot accepted — real protection most of the
    time beats none. ``limiter=None`` (accepted by ``handle_message``/
    ``handle_command``) skips the check entirely, for callers that don't want
    it (or a future test/local-dev path).
    """

    def __init__(self, max_per_min: int) -> None:
        self.max = max_per_min
        self._hits: dict[int, list[float]] = {}

    def allow(self, user_id: int, now: float) -> bool:
        window = [t for t in self._hits.get(user_id, []) if now - t < 60.0]
        if len(window) >= self.max:
            self._hits[user_id] = window
            return False
        window.append(now)
        self._hits[user_id] = window
        return True


def handle_message(
    text: str,
    user_id: int,
    *,
    responder: Callable[[str], str],
    limiter: RateLimiter | None,
    now: float,
) -> str:
    """Pure per-message handler: rate-check -> agent -> reply. Never raises; a
    failure degrades to a fallback that never leaks internals.
    ``limiter=None`` skips rate-checking entirely (api/webhook.py's case —
    see RateLimiter's docstring for why)."""
    if not text or not text.strip():
        return HELP_MSG
    if limiter is not None and not limiter.allow(user_id, now):
        return RATE_LIMIT_MSG
    try:
        return responder(text.strip())
    except Exception:  # noqa: BLE001 - the bot must never crash on one bad turn
        return FALLBACK


def resolve_command(command: str) -> tuple[str | None, str | None]:
    """Map a command to (static_reply, agent_query). Exactly one is non-None
    for a known command; (None, None) for an unknown one."""
    cmd = command.lstrip("/").split("@")[0].lower()
    if cmd == "start":
        return WELCOME_MSG, None
    if cmd == "help":
        return HELP_MSG, None
    if cmd == "news":
        return None, NEWS_QUERY
    return None, None


def handle_command(
    command: str,
    user_id: int,
    *,
    responder: Callable[[str], str],
    limiter: RateLimiter | None,
    now: float,
) -> str:
    static, query = resolve_command(command)
    if static is not None:
        return static
    if query is None:
        return HELP_MSG
    return handle_message(query, user_id, responder=responder, limiter=limiter, now=now)
