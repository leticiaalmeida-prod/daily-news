from __future__ import annotations

from unittest.mock import patch

from api.cron import _run_and_send, handle_cron
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


# --- handle_cron: the WSGI-agnostic entrypoint api/index.py dispatches to ---


def test_handle_cron_rejects_missing_secret(monkeypatch) -> None:
    monkeypatch.delenv("CRON_SECRET", raising=False)
    assert handle_cron("Bearer whatever") == 401


def test_handle_cron_rejects_wrong_auth_header(monkeypatch) -> None:
    monkeypatch.setenv("CRON_SECRET", "right-secret")
    assert handle_cron("Bearer wrong-secret") == 401
    assert handle_cron(None) == 401


def test_handle_cron_rejects_non_ascii_auth_header(monkeypatch) -> None:
    """The constant-time comparison must reject a hostile non-ASCII header
    with a 401, never crash — see api/webhook.py's secret check."""
    monkeypatch.setenv("CRON_SECRET", "right-secret")
    assert handle_cron("Bearer сёкрет-🔑") == 401


def test_handle_cron_accepts_correct_bearer_token(monkeypatch) -> None:
    monkeypatch.setenv("CRON_SECRET", "right-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "w")
    monkeypatch.setenv("NYT_API_KEY", "n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)  # -> _run_and_send no-ops
    assert handle_cron("Bearer right-secret") == 200
