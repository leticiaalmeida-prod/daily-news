from __future__ import annotations

from unittest.mock import patch

from api.webhook import _reply_for
from bot.bot import HELP_MSG
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


def test_start_command_replies_with_chat_id() -> None:
    chat_id, reply = _reply_for(_message_update(text="/start", chat_id=999), _cfg())
    assert chat_id == "999"
    assert reply.endswith("999")


def test_help_command_does_not_touch_llm_or_nyt() -> None:
    # No patching of make_llm/build_nyt_tools/agent.respond at all — if the
    # help path ever accidentally called through to them, this would raise
    # (real network/API calls aren't available in the test environment).
    chat_id, reply = _reply_for(_message_update(text="/help"), _cfg())
    assert chat_id == "555"
    assert reply == HELP_MSG


def test_news_command_routes_through_agent_respond() -> None:
    with (
        patch("api.webhook.make_llm", return_value="fake-llm"),
        patch("api.webhook.build_nyt_tools", return_value="fake-tools"),
        patch("api.webhook.agent.respond", return_value="here's the news") as mock_respond,
    ):
        chat_id, reply = _reply_for(_message_update(text="/news"), _cfg())

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
        chat_id, reply = _reply_for(_message_update(text="anything new on AI?"), _cfg())

    assert chat_id == "555"
    assert reply == "plain text reply"


def test_non_message_update_returns_none() -> None:
    assert _reply_for({"update_id": 1, "edited_message": {}}, _cfg()) is None


def test_message_without_text_returns_none() -> None:
    update = {"update_id": 1, "message": {"chat": {"id": 1}, "from": {"id": 1}}}
    assert _reply_for(update, _cfg()) is None
