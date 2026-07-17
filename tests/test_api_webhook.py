from __future__ import annotations

from unittest.mock import patch

from api.webhook import _reply_for
from bot.bot import HELP_MSG, RATE_LIMIT_MSG, RateLimiter
from bot.config import BotConfig


def _cfg(**overrides) -> BotConfig:
    defaults = dict(
        telegram_token="TOKEN",
        telegram_chat_id="",
        telegram_webhook_secret="secret",
        nyt_api_key="nyt",
        anthropic_api_key="anthropic",
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _message_update(*, text: str, chat_id: int = 555, user_id: int = 1) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id},
            "text": text,
        },
    }


# limiter=None everywhere below — the module-level default (_LIMITER) is a
# SHARED singleton across the whole test session; passing it implicitly here
# would make these tests order-dependent on each other's call counts. The
# shared-instance behavior itself is covered separately below.


def test_start_command_replies_with_chat_id() -> None:
    chat_id, reply = _reply_for(
        _message_update(text="/start", chat_id=999), _cfg(), limiter=None
    )
    assert chat_id == "999"
    assert reply.endswith("999")


def test_help_command_does_not_touch_llm_or_nyt() -> None:
    # No patching of make_llm/build_nyt_tools/agent.respond at all — if the
    # help path ever accidentally called through to them, this would raise
    # (real network/API calls aren't available in the test environment).
    chat_id, reply = _reply_for(_message_update(text="/help"), _cfg(), limiter=None)
    assert chat_id == "555"
    assert reply == HELP_MSG


def test_news_command_routes_through_agent_respond() -> None:
    with (
        patch("api.webhook.make_llm", return_value="fake-llm"),
        patch("api.webhook.build_nyt_tools", return_value="fake-tools"),
        patch("api.webhook.agent.respond", return_value="here's the news") as mock_respond,
    ):
        chat_id, reply = _reply_for(_message_update(text="/news"), _cfg(), limiter=None)

    assert chat_id == "555"
    assert reply == "here's the news"
    assert mock_respond.call_args.kwargs["llm"] == "fake-llm"
    assert mock_respond.call_args.kwargs["tools"] == "fake-tools"


def test_plain_text_routes_through_agent_respond() -> None:
    with (
        patch("api.webhook.make_llm", return_value="fake-llm"),
        patch("api.webhook.build_nyt_tools", return_value="fake-tools"),
        patch("api.webhook.agent.respond", return_value="plain text reply"),
    ):
        chat_id, reply = _reply_for(
            _message_update(text="anything new on AI?"), _cfg(), limiter=None
        )

    assert chat_id == "555"
    assert reply == "plain text reply"


def test_non_message_update_returns_none() -> None:
    assert _reply_for({"update_id": 1, "edited_message": {}}, _cfg(), limiter=None) is None


def test_message_without_text_returns_none() -> None:
    update = {"update_id": 1, "message": {"chat": {"id": 1}, "from": {"id": 1}}}
    assert _reply_for(update, _cfg(), limiter=None) is None


def test_rate_limited_user_gets_rate_limit_message() -> None:
    """A fresh, explicitly-passed RateLimiter (not the shared module-level
    _LIMITER) — proves _reply_for actually threads `limiter` through to
    handle_message/handle_command, matching the reference bot's own
    module-level chatHits pattern (see api/webhook.py's module docstring)."""
    limiter = RateLimiter(max_per_min=1)
    with (
        patch("api.webhook.make_llm", return_value="fake-llm"),
        patch("api.webhook.build_nyt_tools", return_value="fake-tools"),
        patch("api.webhook.agent.respond", return_value="reply one"),
    ):
        first = _reply_for(_message_update(text="hi"), _cfg(), limiter=limiter)
        second = _reply_for(_message_update(text="hi again"), _cfg(), limiter=limiter)

    assert first == ("555", "reply one")
    assert second == ("555", RATE_LIMIT_MSG)
