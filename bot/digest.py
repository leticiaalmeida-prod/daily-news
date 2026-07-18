"""The scheduled daily digest: fetch -> filter -> comprehend -> format.

Two stages, run separately so each prompt stays narrow and the expensive one
only runs on survivors (see memory: this shape was chosen over a single
combined prompt specifically to keep cost down and each prompt tunable):

1. ``filter_candidates`` — ONE batched call over every candidate's
   title+abstract only. Cheap. Decides which articles are worth a reader's
   time at all, and a first-pass relevance category.
2. ``comprehend`` — one call PER surviving article. Produces the actual
   digest content: a summary, a topic tag, a (possibly revised) relevance
   category, and a neutral explanation — de-biasing the article's own framing
   for contentious topics, or background context for unfamiliar/technical
   ones (the model picks the mode; see SYSTEM_PROMPT-adjacent instructions
   below).

Both stages force a structured reply via Anthropic tool-use with
``tool_choice`` pinned to a single synthetic tool, rather than asking for
free-text JSON — more reliable to parse, same trick ``bot/agent.py`` doesn't
need because it's conversational, not extractive.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .models import Candidate
from .rss import fetch_rss_candidates
from .surfcall_tools import TOP_STORIES_TOOL, ToolProvider

DEFAULT_SECTIONS: tuple[str, ...] = (
    "technology",
    "business",
    "science",
    "world",
    "politics",
    "health",
)

RELEVANCE_ORDER = ("must-read", "relevant", "tangential")


@dataclass
class DigestItem:
    candidate: Candidate
    why: str
    summary: str
    topic: str
    relevance: str
    explanation_mode: str
    neutral_explanation: str


_FILTER_TOOL = {
    "name": "submit_filtered",
    "description": (
        "Return which candidate articles (by index) are relevant enough to a "
        "reader's stated interests to be worth showing them, each with a "
        "first-pass relevance category."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "0-based index into the candidate list.",
                        },
                        "relevance": {
                            "type": "string",
                            "enum": list(RELEVANCE_ORDER),
                        },
                    },
                    "required": ["index", "relevance"],
                },
            }
        },
        "required": ["matches"],
    },
}

_COMPREHEND_TOOL = {
    "name": "submit_comprehension",
    "description": "Return the comprehension-layer output for one article.",
    "input_schema": {
        "type": "object",
        "properties": {
            "why": {
                "type": "string",
                "description": (
                    "ONE sharp sentence on why THIS reader specifically should care "
                    "— not a summary of the article. Ground it in their stated "
                    "interests/position (see the interests profile): their strategic "
                    "position, their work, their stage, their ecosystem. This is the "
                    "'here's why you should care' line, not the 'here's what "
                    "happened' line."
                ),
            },
            "summary": {
                "type": "string",
                "description": "2-3 sentence resume of the article.",
            },
            "topic": {
                "type": "string",
                "description": "A short topic tag/label, e.g. 'AI regulation'.",
            },
            "relevance": {"type": "string", "enum": list(RELEVANCE_ORDER)},
            "explanation_mode": {
                "type": "string",
                "enum": ["debias_framing", "background_context"],
                "description": (
                    "'debias_framing' for contentious/political topics (strip the "
                    "article's own loaded language, restate claims neutrally); "
                    "'background_context' for technical/unfamiliar topics (explain "
                    "what a reader needs to know, independent of this article)."
                ),
            },
            "neutral_explanation": {
                "type": "string",
                "description": (
                    "The de-biased framing or background context, per "
                    "explanation_mode — NOT a summary of the article. It's the "
                    "context layer AROUND the article: define any jargon or "
                    "acronyms, explain the background situation, connect it to the "
                    "broader landscape. No opinion, no spin. Write for a sharp "
                    "reader who hasn't been following this specific story — the "
                    "footnote a well-informed friend would whisper to them while "
                    "reading the news together. 2-3 sentences max."
                ),
            },
        },
        "required": [
            "why",
            "summary",
            "topic",
            "relevance",
            "explanation_mode",
            "neutral_explanation",
        ],
    },
}


def fetch_candidates(
    tools: ToolProvider, sections: tuple[str, ...] = DEFAULT_SECTIONS
) -> list[Candidate]:
    """Pull Top Stories for each section. Never raises — a section that fails
    to fetch (or parse) is silently skipped rather than failing the whole run."""
    candidates: list[Candidate] = []
    for section in sections:
        raw = tools.call(TOP_STORIES_TOOL, {"section": section, "format": "json"})
        try:
            payload = json.loads(raw)
            results = (payload.get("data") or {}).get("results") or []
        except (json.JSONDecodeError, AttributeError):
            continue
        for item in results:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or ""
            if not title:
                continue
            candidates.append(
                Candidate(
                    title=title,
                    abstract=item.get("abstract") or "",
                    url=item.get("url") or "",
                    section=item.get("section") or section,
                )
            )
    return candidates


def _extract_tool_input(response: Any, tool_name: str) -> dict[str, Any] | None:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return block.input
    return None


def filter_candidates(
    *,
    llm: Any,
    model: str,
    interests: str,
    candidates: list[Candidate],
    max_tokens: int = 2048,
) -> list[tuple[Candidate, str]]:
    """One batched call: which candidates are worth showing, and at what
    first-pass relevance. Returns (candidate, relevance) pairs in the same
    order the model returned them; unmatched candidates are dropped."""
    if not candidates:
        return []
    listing = "\n".join(
        f"{i}. [{c.section}] {c.title} — {c.abstract}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        "Reader's interests profile:\n"
        f"{interests}\n\n"
        "Candidate articles (index. [section] title — abstract):\n"
        f"{listing}\n\n"
        "Call submit_filtered with only the articles worth showing this reader."
    )
    response = llm.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[_FILTER_TOOL],
        tool_choice={"type": "tool", "name": "submit_filtered"},
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = _extract_tool_input(response, "submit_filtered")
    if not parsed:
        return []
    out: list[tuple[Candidate, str]] = []
    for match in parsed.get("matches", []):
        idx = match.get("index")
        relevance = match.get("relevance")
        if not isinstance(idx, int) or not (0 <= idx < len(candidates)):
            continue
        if relevance not in RELEVANCE_ORDER:
            continue
        out.append((candidates[idx], relevance))
    return out


def comprehend(
    *,
    llm: Any,
    model: str,
    interests: str,
    candidate: Candidate,
    first_pass_relevance: str,
    max_tokens: int = 1024,
) -> DigestItem | None:
    """One call per surviving article: summary, topic, (possibly revised)
    relevance, and the neutral explanation. Returns None on a malformed reply
    rather than raising — a dropped story degrades the digest, not the run."""
    prompt = (
        "Reader's interests profile:\n"
        f"{interests}\n\n"
        f"Article: {candidate.title}\n"
        f"Section: {candidate.section}\n"
        f"Abstract: {candidate.abstract}\n"
        f"First-pass relevance: {first_pass_relevance}\n\n"
        "Call submit_comprehension with the full comprehension-layer output for "
        "this article."
    )
    response = llm.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[_COMPREHEND_TOOL],
        tool_choice={"type": "tool", "name": "submit_comprehension"},
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = _extract_tool_input(response, "submit_comprehension")
    if not parsed or parsed.get("relevance") not in RELEVANCE_ORDER:
        return None
    return DigestItem(
        candidate=candidate,
        why=parsed.get("why", ""),
        summary=parsed.get("summary", ""),
        topic=parsed.get("topic", ""),
        relevance=parsed["relevance"],
        explanation_mode=parsed.get("explanation_mode", "background_context"),
        neutral_explanation=parsed.get("neutral_explanation", ""),
    )


_RELEVANCE_HEADERS = {
    "must-read": "🚨 MUST-READ",
    "relevant": "📌 RELEVANT",
    "tangential": "🔍 TANGENTIAL",
}


def _format_story(item: DigestItem) -> str:
    return "\n\n".join(
        [
            f"• {item.topic} — {item.candidate.title}",
            item.why,
            item.summary,
            f"({item.explanation_mode.replace('_', ' ')}) {item.neutral_explanation}",
            item.candidate.url,
        ]
    )


def format_digest(items: list[DigestItem]) -> str:
    """Plain-text digest, grouped by relevance (must-read first) under an
    emoji-labeled header, with a full blank line between every paragraph.
    Telegram doesn't render Markdown reliably here, so spacing + emoji carry
    the visual structure instead of ``**``/``#``."""
    if not items:
        return "No stories cleared your interests filter today."
    by_relevance = {r: [i for i in items if i.relevance == r] for r in RELEVANCE_ORDER}
    sections: list[str] = []
    for relevance in RELEVANCE_ORDER:
        group = by_relevance[relevance]
        if not group:
            continue
        body = "\n\n".join(_format_story(item) for item in group)
        sections.append(f"{_RELEVANCE_HEADERS[relevance]}\n\n{body}")
    return "\n\n".join(sections)


COMPREHEND_MAX_WORKERS = 8


def run_digest(
    *,
    tools: ToolProvider,
    llm: Any,
    model: str,
    interests: str,
    sections: tuple[str, ...] = DEFAULT_SECTIONS,
    max_workers: int = COMPREHEND_MAX_WORKERS,
) -> str:
    """The full pipeline: fetch -> filter -> comprehend -> format. Never raises
    on a per-article basis — a story that fails comprehension is skipped, not
    fatal to the whole digest. Candidates come from NYT (via ``tools``, gecko-
    surf) and RSS (CoinDesk/The Block/Blockworks, plain XML — see rss.py for
    why crypto coverage isn't a gecko-surf tool).

    ``comprehend`` calls run CONCURRENTLY (thread pool, not sequential) — each
    is an independent network call, so this is a real wall-clock win, not
    just a nicety: on the Vercel deployment, the scheduled digest runs inside
    a serverless function with a hard execution time limit, and a dozen-plus
    sequential LLM calls (one per surviving article) can plausibly exceed it.
    Order of ``items`` in the output isn't guaranteed to match filter order —
    ``format_digest`` groups by relevance anyway, so this doesn't matter."""
    candidates = fetch_candidates(tools, sections) + fetch_rss_candidates()
    filtered = filter_candidates(
        llm=llm, model=model, interests=interests, candidates=candidates
    )
    if not filtered:
        return format_digest([])

    def _comprehend_one(pair: tuple[Candidate, str]) -> DigestItem | None:
        candidate, relevance = pair
        return comprehend(
            llm=llm,
            model=model,
            interests=interests,
            candidate=candidate,
            first_pass_relevance=relevance,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = pool.map(_comprehend_one, filtered)
    items = [item for item in results if item is not None]
    return format_digest(items)
