"""LLM provider — a thin, injectable wrapper so the digest pipeline and the
on-demand agent loop never import ``anthropic`` directly (keeps both testable
offline with a fake client, same seam as the reference bot)."""

from __future__ import annotations

from typing import Any, Protocol

DEFAULT_MODEL = "claude-haiku-4-5"


class LlmClient(Protocol):
    """The one method both the agent loop and the digest pipeline need —
    matches ``anthropic.Anthropic().messages`` closely enough to duck-type it."""

    class _Messages(Protocol):
        def create(self, **kwargs: Any) -> Any: ...

    messages: _Messages


def make_llm(api_key: str) -> Any:
    import anthropic

    return anthropic.Anthropic(api_key=api_key)
