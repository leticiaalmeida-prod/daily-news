"""Single Vercel Python entrypoint — a plain WSGI app dispatching by path.

Vercel's current Python builder wants exactly ONE entrypoint per project
(this file, defining ``app``), not one Vercel Function per file under
``api/`` — that per-file auto-discovery is documented but did not work
against this deployment in practice: both ``api/webhook.py`` and
``api/cron.py`` as independent ``handler`` classes produced "No python
entrypoint found ... found potential entrypoints" on every deploy attempt,
even with ``pyproject.toml``/``uv.lock`` excluded from the upload. See
README's "Known gaps" for the full story.

Both routes are served from here instead. ``api/webhook.py`` and
``api/cron.py`` keep the actual logic as plain, testable functions
(``handle_webhook``, ``handle_cron``) with no HTTP framework dependency of
their own — this file is only the thinnest possible routing shim translating
WSGI's ``environ`` into the arguments those functions need.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from cron import handle_cron  # noqa: E402
from webhook import handle_webhook  # noqa: E402

StartResponse = Callable[[str, list[tuple[str, str]]], None]

_STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
}


def app(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
    path = environ.get("PATH_INFO", "")
    method = environ.get("REQUEST_METHOD", "GET")

    if path == "/api/webhook" and method == "POST":
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length) if length else b""
        secret = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN")
        status = handle_webhook(secret, body)
    elif path == "/api/cron" and method == "GET":
        status = handle_cron(environ.get("HTTP_AUTHORIZATION"))
    else:
        status = 404

    text = _STATUS_TEXT.get(status, "Error")
    start_response(f"{status} {text}", [("Content-Type", "text/plain")])
    return [text.encode("utf-8")]
