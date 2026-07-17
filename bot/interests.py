"""Loads the interests profile doc (bot/interests.md) as plain text — read
verbatim and handed to the LLM as context, never parsed into structured
fields. See interests.md's header comment for how to fill it in."""

from __future__ import annotations

from pathlib import Path

from .config import INTERESTS_PATH


def load_interests(path: Path = INTERESTS_PATH) -> str:
    return path.read_text(encoding="utf-8")
