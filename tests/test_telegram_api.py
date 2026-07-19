from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from bot.telegram_api import (
    TelegramApiError,
    chunk_message,
    send_chunked,
    send_message,
    set_webhook,
)


def test_chunk_message_short_text_single_chunk() -> None:
    assert chunk_message("hello") == ["hello"]


def test_chunk_message_splits_long_text_within_limit() -> None:
    text = "word " * 2000
    chunks = chunk_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks).replace(" ", "") in text.replace(" ", "")


def _fake_response(status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json={"ok": True}, request=httpx.Request("POST", "https://x"))


def test_send_message_posts_to_correct_url_and_body() -> None:
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _fake_response()

    with patch("bot.telegram_api.httpx.post", fake_post):
        send_message(token="TOKEN", chat_id="123", text="hi")

    assert captured["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert captured["json"] == {"chat_id": "123", "text": "hi"}


def test_send_message_raises_on_error_status() -> None:
    with patch("bot.telegram_api.httpx.post", return_value=_fake_response(400)):
        with pytest.raises(TelegramApiError):
            send_message(token="TOKEN", chat_id="123", text="hi")


def test_send_chunked_sends_one_call_per_chunk() -> None:
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json["text"])
        return _fake_response()

    long_text = "word " * 2000
    expected_chunks = chunk_message(long_text)
    with patch("bot.telegram_api.httpx.post", fake_post):
        send_chunked(token="TOKEN", chat_id="123", text=long_text)

    assert calls == expected_chunks


def test_set_webhook_posts_url_and_secret() -> None:
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _fake_response()

    with patch("bot.telegram_api.httpx.post", fake_post):
        set_webhook(token="TOKEN", url="https://example.com/api/webhook", secret_token="s3cr3t")

    assert captured["url"] == "https://api.telegram.org/botTOKEN/setWebhook"
    assert captured["json"] == {"url": "https://example.com/api/webhook", "secret_token": "s3cr3t"}


# --- Token redaction: a failed Telegram call must NEVER put the bot token in
# an exception message. httpx's own HTTPStatusError formats the request URL
# into str(exc) — and the Telegram API URL embeds the bot token — so an
# unhandled failure inside a Vercel function would print the token straight
# into the deployment logs. TelegramApiError replaces it. ---


def _real_shaped_response(status_code: int, token: str) -> httpx.Response:
    """A response whose request URL embeds the token, exactly as httpx.post
    builds it in send_message/set_webhook."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    return httpx.Response(
        status_code,
        json={"ok": False, "description": "Bad Request: chat not found"},
        request=httpx.Request("POST", url),
    )


def test_send_message_error_never_contains_bot_token() -> None:
    token = "123456789:AAfake-short-token"
    with patch(
        "bot.telegram_api.httpx.post",
        return_value=_real_shaped_response(400, token),
    ):
        with pytest.raises(TelegramApiError) as excinfo:
            send_message(token=token, chat_id="123", text="hi")
    assert token not in str(excinfo.value)
    # The chain must be suppressed too — a logged traceback would otherwise
    # print the original httpx error, whose message carries the URL (with the
    # token) in it. `raise ... from None` sets exactly these two flags.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_send_message_error_keeps_debuggable_details() -> None:
    """Redaction must not destroy debuggability: status code and Telegram's
    own (token-free) error description survive."""
    token = "123456789:AAfake-short-token"
    with patch(
        "bot.telegram_api.httpx.post",
        return_value=_real_shaped_response(400, token),
    ):
        with pytest.raises(TelegramApiError, match="400.*chat not found"):
            send_message(token=token, chat_id="123", text="hi")


def test_send_message_transport_error_never_contains_bot_token() -> None:
    token = "123456789:AAfake-short-token"

    def fake_post(url, json=None, timeout=None):
        raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

    with patch("bot.telegram_api.httpx.post", fake_post):
        with pytest.raises(TelegramApiError) as excinfo:
            send_message(token=token, chat_id="123", text="hi")
    assert token not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_set_webhook_error_never_contains_bot_token() -> None:
    token = "123456789:AAfake-short-token"
    with patch(
        "bot.telegram_api.httpx.post",
        return_value=_real_shaped_response(500, token),
    ):
        with pytest.raises(TelegramApiError) as excinfo:
            set_webhook(token=token, url="https://x.vercel.app/api/webhook", secret_token="s")
    assert token not in str(excinfo.value)


def test_chunk_message_hard_splits_text_with_no_boundaries() -> None:
    """The `cut <= 0` last-resort branch: a single unbroken run longer than
    the limit (no paragraph/line/word boundary to prefer) must still come
    back in <= limit chunks with no character lost."""
    text = "x" * 10_000
    chunks = chunk_message(text)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == text
