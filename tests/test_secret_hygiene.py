"""Repo-wide secret hygiene guards.

Three invariants, each cheap to check and painful to discover broken in prod:

1. No module reads a secret at IMPORT time — a missing env var must fail at
   USE (with a clear message naming the variable), never as an import-time
   crash that takes down every route at once. `BotConfig.from_env()` is
   call-time today; this locks that in against a future module-level
   ``CFG = BotConfig.from_env()`` slipping in.
2. `.env` (real local secrets) is gitignored — it can never be committed by
   accident.
3. No secret-shaped literal is sitting in any tracked file.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Every module in the import graph, including the Vercel entrypoint (which
# pulls in api/webhook.py + api/cron.py, which pull in everything under bot/).
_IMPORT_PROBE = """
import os
for key in list(os.environ):
    if key.startswith(("TELEGRAM", "NYT_", "ANTHROPIC", "CRON_", "GECKO_CRED")):
        del os.environ[key]
# Stub .env loading BEFORE bot.config imports, so a populated local .env on a
# dev machine can't mask an import-time read that would crash a clean deploy.
import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import bot.agent
import bot.bot
import bot.config
import bot.digest
import bot.interests
import bot.models
import bot.providers
import bot.rss
import bot.surfcall_tools
import bot.telegram_api
import api.index
print("IMPORTS_OK")
"""


def test_no_module_reads_secrets_at_import_time() -> None:
    """Importing the whole app with every secret env var scrubbed (and .env
    loading stubbed out) must succeed — secrets are read at use, not import."""
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"import-time failure:\n{result.stderr}"
    assert "IMPORTS_OK" in result.stdout


def test_dotenv_is_gitignored() -> None:
    """`git check-ignore .env` exits 0 iff .env would be ignored — the file
    doesn't need to exist for the rule to be verified."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=REPO_ROOT, timeout=30
    )
    assert result.returncode == 0, ".env is NOT gitignored — a real key could be committed"


# Shapes that only ever appear as REAL credentials, never as placeholders:
# a Telegram bot token, an Anthropic key, an AWS access key id, or any
# key/secret/token-named assignment to a long opaque literal.
_SECRET_PATTERNS = (
    re.compile(r"\d{8,10}:AA[A-Za-z0-9_-]{33}"),  # Telegram bot token
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),  # Anthropic API key
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(
        r"""(?ix)(api[_-]?key|secret|token)\s*[:=]\s*['"][A-Za-z0-9+/_-]{24,}['"]"""
    ),
)

_SCANNED_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".example", ""}


def test_no_secret_looking_literals_in_tracked_files() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.splitlines()
    offenders: list[str] = []
    for rel in tracked:
        path = REPO_ROOT / rel
        if path.suffix not in _SCANNED_SUFFIXES or not path.is_file():
            continue
        if rel == "uv.lock":  # package hashes only — long literals, but not creds
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                # Report file + pattern only — NEVER echo the matched value.
                offenders.append(f"{rel} (pattern: {pattern.pattern[:40]}...)")
                break
    assert not offenders, f"secret-shaped literal(s) in tracked files: {offenders}"
