from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from bot.telegram_api import chunk_message, send_chunked, send_message, set_webhook


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
        with pytest.raises(httpx.HTTPStatusError):
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
