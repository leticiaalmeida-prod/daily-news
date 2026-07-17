from __future__ import annotations

from unittest.mock import patch

from api.index import app


def _call(environ: dict) -> tuple[int, list[tuple[str, str]], bytes]:
    captured = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = int(status.split(" ", 1)[0])
        captured["headers"] = headers

    body = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], body


def test_unknown_path_returns_404() -> None:
    status, _, _ = _call({"PATH_INFO": "/nope", "REQUEST_METHOD": "GET"})
    assert status == 404


def test_webhook_wrong_method_returns_404() -> None:
    status, _, _ = _call({"PATH_INFO": "/api/webhook", "REQUEST_METHOD": "GET"})
    assert status == 404


def test_cron_wrong_method_returns_404() -> None:
    status, _, _ = _call({"PATH_INFO": "/api/cron", "REQUEST_METHOD": "POST"})
    assert status == 404


def test_webhook_route_reads_body_and_secret_header() -> None:
    from io import BytesIO

    body = b'{"update_id": 1}'
    environ = {
        "PATH_INFO": "/api/webhook",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "the-secret",
        "wsgi.input": BytesIO(body),
    }
    with patch("api.index.handle_webhook", return_value=200) as mock_handle:
        status, _, _ = _call(environ)
    assert status == 200
    mock_handle.assert_called_once_with("the-secret", body)


def test_cron_route_reads_authorization_header() -> None:
    environ = {
        "PATH_INFO": "/api/cron",
        "REQUEST_METHOD": "GET",
        "HTTP_AUTHORIZATION": "Bearer x",
    }
    with patch("api.index.handle_cron", return_value=200) as mock_handle:
        status, _, _ = _call(environ)
    assert status == 200
    mock_handle.assert_called_once_with("Bearer x")
