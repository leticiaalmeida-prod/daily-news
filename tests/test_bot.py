from __future__ import annotations

from bot.bot import (
    HELP_MSG,
    RATE_LIMIT_MSG,
    RateLimiter,
    handle_command,
    handle_message,
    resolve_command,
)


def test_rate_limiter_blocks_after_max_per_min() -> None:
    limiter = RateLimiter(max_per_min=2)
    assert limiter.allow(1, now=0.0) is True
    assert limiter.allow(1, now=1.0) is True
    assert limiter.allow(1, now=2.0) is False


def test_rate_limiter_window_slides() -> None:
    limiter = RateLimiter(max_per_min=1)
    assert limiter.allow(1, now=0.0) is True
    assert limiter.allow(1, now=30.0) is False
    assert limiter.allow(1, now=61.0) is True  # outside the 60s window


def test_rate_limiter_is_per_user() -> None:
    limiter = RateLimiter(max_per_min=1)
    assert limiter.allow(1, now=0.0) is True
    assert limiter.allow(2, now=0.0) is True


def test_handle_message_routes_to_responder() -> None:
    limiter = RateLimiter(max_per_min=5)
    reply = handle_message(
        "hi", 1, responder=lambda t: f"got: {t}", limiter=limiter, now=0.0
    )
    assert reply == "got: hi"


def test_handle_message_empty_text_shows_help() -> None:
    limiter = RateLimiter(max_per_min=5)
    reply = handle_message("   ", 1, responder=lambda t: "x", limiter=limiter, now=0.0)
    assert reply == HELP_MSG


def test_handle_message_rate_limited() -> None:
    limiter = RateLimiter(max_per_min=1)
    limiter.allow(1, now=0.0)
    reply = handle_message("hi", 1, responder=lambda t: "x", limiter=limiter, now=0.0)
    assert reply == RATE_LIMIT_MSG


def test_handle_message_never_raises_on_responder_error() -> None:
    limiter = RateLimiter(max_per_min=5)

    def boom(_: str) -> str:
        raise RuntimeError("boom")

    from bot.agent import FALLBACK

    reply = handle_message("hi", 1, responder=boom, limiter=limiter, now=0.0)
    assert reply == FALLBACK


def test_resolve_command_known_and_unknown() -> None:
    static, query = resolve_command("/start")
    assert static is not None and query is None
    static, query = resolve_command("/news")
    assert static is None and query is not None
    static, query = resolve_command("/nonexistent")
    assert static is None and query is None


def test_handle_command_unknown_shows_help() -> None:
    limiter = RateLimiter(max_per_min=5)
    reply = handle_command(
        "/nonexistent", 1, responder=lambda t: "x", limiter=limiter, now=0.0
    )
    assert reply == HELP_MSG


def test_handle_message_with_no_limiter_skips_rate_check() -> None:
    """api/webhook.py's case — stateless serverless, no persistent limiter."""
    reply = handle_message("hi", 1, responder=lambda t: f"got: {t}", limiter=None, now=0.0)
    assert reply == "got: hi"
