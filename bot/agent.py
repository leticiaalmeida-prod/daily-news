"""The Claude tool-use loop for the on-demand /news command — a Telegram
message in, a reply out. Mirrors GeckoVision/ayuda-venezuela-bot's agent.py: a
manual agentic loop (per the Anthropic Messages API, not an SDK agent
wrapper), bounded by ``max_iters`` so a misbehaving model can't loop forever.

``llm`` is injected (the real ``anthropic.Anthropic`` client OR a fake), so
the whole loop is testable offline — no network, no spend."""

from __future__ import annotations

from typing import Any

from .surfcall_tools import MultiSurfaceTools

FALLBACK = (
    "Sorry, I couldn't complete that lookup right now. Try again in a moment."
)


def _text_of(resp: Any) -> str:
    parts = [
        getattr(b, "text", "") or ""
        for b in resp.content
        if getattr(b, "type", None) == "text"
    ]
    return "".join(parts).strip()


def respond(
    user_text: str,
    *,
    llm: Any,
    tools: MultiSurfaceTools,
    model: str,
    system: str,
    history: list[dict[str, Any]] | None = None,
    max_tokens: int = 1024,
    max_iters: int = 4,
) -> str:
    """Run the tool-use loop for one user message; return the model's reply."""
    messages: list[dict[str, Any]] = list(history or []) + [
        {"role": "user", "content": user_text}
    ]
    tool_defs = tools.tools_for_llm()

    for _ in range(max_iters):
        resp = llm.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tool_defs,
            messages=messages,
        )
        if getattr(resp, "stop_reason", None) != "tool_use":
            return _text_of(resp) or FALLBACK

        messages.append({"role": "assistant", "content": resp.content})
        results: list[dict[str, Any]] = []
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tools.call(block.name, block.input),
                    }
                )
        messages.append({"role": "user", "content": results})

    return FALLBACK  # loop budget exhausted — degrade gracefully, never hang
