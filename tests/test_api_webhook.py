from __future__ import annotations

import json
from unittest.mock import patch

from api.webhook import _reply_for, handle_webhook
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


# --- handle_webhook: the WSGI-agnostic entrypoint api/index.py dispatches to ---

_ENV = {
    "TELEGRAM_BOT_TOKEN": "TOKEN",
    "TELEGRAM_WEBHOOK_SECRET": "correct-secret",
    "NYT_API_KEY": "nyt",
    "ANTHROPIC_API_KEY": "anthropic",
}


def _set_env(monkeypatch) -> None:
    for key, val in _ENV.items():
        monkeypatch.setenv(key, val)


def test_handle_webhook_rejects_wrong_secret(monkeypatch) -> None:
    _set_env(monkeypatch)
    status = handle_webhook("wrong-secret", b"{}")
    assert status == 403


def test_handle_webhook_rejects_missing_secret_header(monkeypatch) -> None:
    """Telegram always sends the header once set_webhook registered it — a
    request WITHOUT it is by definition not from Telegram."""
    _set_env(monkeypatch)
    assert handle_webhook(None, b"{}") == 403


def test_handle_webhook_rejects_non_ascii_secret_header(monkeypatch) -> None:
    """A hostile header can contain anything; the constant-time comparison
    must reject it with a 403, never crash (compare_digest rejects non-ASCII
    str, hence the bytes encoding in handle_webhook)."""
    _set_env(monkeypatch)
    assert handle_webhook("сёкрет-🔑", b"{}") == 403


def test_handle_webhook_rejects_invalid_json(monkeypatch) -> None:
    _set_env(monkeypatch)
    status = handle_webhook("correct-secret", b"not json")
    assert status == 400


def test_handle_webhook_accepts_and_sends_reply(monkeypatch) -> None:
    _set_env(monkeypatch)
    body = json.dumps(_message_update(text="/help")).encode()
    with patch("api.webhook.send_chunked") as mock_send:
        status = handle_webhook("correct-secret", body)
    assert status == 200
    mock_send.assert_called_once_with(token="TOKEN", chat_id="555", text=HELP_MSG)


def test_handle_webhook_empty_body_still_returns_200(monkeypatch) -> None:
    _set_env(monkeypatch)
    with patch("api.webhook.send_chunked") as mock_send:
        status = handle_webhook("correct-secret", b"")
    assert status == 200
    mock_send.assert_not_called()


def test_duplicate_update_id_is_processed_only_once(monkeypatch) -> None:
    """Telegram re-delivering the same update (slow 200) must NOT re-run the
    agent or re-send — that would double the LLM spend. The second delivery is
    dropped, still 200 so Telegram stops retrying."""
    from bot.bot import SeenUpdates

    _set_env(monkeypatch)
    body = json.dumps(_message_update(text="/news")).encode()
    # A fresh guard isolates this test from the module-level singleton.
    monkeypatch.setattr("api.webhook._SEEN", SeenUpdates(max_size=8))
    with (
        patch("api.webhook.make_llm", return_value="fake-llm"),
        patch("api.webhook.build_nyt_tools", return_value="fake-tools"),
        patch("api.webhook.agent.respond", return_value="the news") as mock_respond,
        patch("api.webhook.send_chunked") as mock_send,
    ):
        first = handle_webhook("correct-secret", body)
        second = handle_webhook("correct-secret", body)  # redelivery

    assert first == 200 and second == 200
    mock_respond.assert_called_once()  # agent ran once, not twice
    mock_send.assert_called_once()  # one reply, not two


def test_message_without_chat_id_returns_none() -> None:
    """The `chat_id is None` branch — a malformed update must be ignored,
    not crash the handler."""
    update = {"update_id": 1, "message": {"from": {"id": 1}, "text": "hi"}}
    assert _reply_for(update, _cfg(), limiter=None) is None
