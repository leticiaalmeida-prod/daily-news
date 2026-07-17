from __future__ import annotations

from unittest.mock import patch

from api.cron import _run_and_send
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


def test_skips_send_when_no_chat_id() -> None:
    with (
        patch("api.cron.make_llm") as mock_make_llm,
        patch("api.cron.run_digest") as mock_run_digest,
        patch("api.cron.send_chunked") as mock_send,
    ):
        _run_and_send(_cfg(telegram_chat_id=""))

    mock_make_llm.assert_not_called()
    mock_run_digest.assert_not_called()
    mock_send.assert_not_called()


def test_generates_and_sends_digest_when_chat_id_set() -> None:
    with (
        patch("api.cron.make_llm", return_value="fake-llm") as mock_make_llm,
        patch("api.cron.build_nyt_tools", return_value="fake-tools"),
        patch("api.cron.run_digest", return_value="the digest text") as mock_run_digest,
        patch("api.cron.send_chunked") as mock_send,
    ):
        _run_and_send(_cfg(telegram_chat_id="999"))

    mock_make_llm.assert_called_once_with("anthropic")
    assert mock_run_digest.call_args.kwargs["tools"] == "fake-tools"
    assert mock_run_digest.call_args.kwargs["llm"] == "fake-llm"
    mock_send.assert_called_once_with(token="TOKEN", chat_id="999", text="the digest text")
