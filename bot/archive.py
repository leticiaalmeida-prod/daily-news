"""Append-only run archive — one JSON line per digest run, so each run leaves
a small logbook instead of vanishing without a trace.

What it buys: when a digest is thin, weird, or missing, there's now a record
of what happened — which sources answered, how many candidates there were,
what got selected, which model ran, and the token cost. It's also the eval
substrate: with a few weeks of runs logged, a prompt/interests change can be
measured against real history instead of guessed at.

**Control-plane discipline (same rule as gecko-surf):** the record stores
HEADLINE METADATA ONLY — title, url, source, category. Never an article body,
abstract, summary, or the model's "why". Small, safe to keep anywhere.

**Storage:** local dev writes a file (``var/digests.jsonl``, gitignored). On
Vercel the filesystem is EPHEMERAL — a file written here evaporates when the
function instance is recycled — so production needs a durable sink (Vercel
Blob: one object per run, or send the line to yourself on Telegram). That
sink is deliberately NOT built here; ``append_run`` is the seam to swap. This
module is intentionally a local-first, fail-soft logbook: archiving must
never break a digest.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid an import cycle (digest imports nothing from here)
    from .digest import DigestItem

# Local dev sink. Gitignored (see .gitignore) — it's run data, not source.
ARCHIVE_PATH = Path(__file__).parent.parent / "var" / "digests.jsonl"


@dataclass
class TokenUsage:
    """Accumulates ``response.usage`` across the run's LLM calls. Thread-safe
    because ``comprehend`` calls run in a thread pool. Reads defensively so a
    fake/usage-less response (tests) is a no-op, never a crash."""

    prompt_tokens: int = 0
    output_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        with self._lock:
            self.prompt_tokens += inp
            self.output_tokens += out


def build_record(
    *,
    sources_fetched: list[str],
    candidate_count: int,
    items: list[DigestItem],
    model: str,
    usage: TokenUsage,
) -> dict[str, Any]:
    """Build one archive record from a finished run — headline metadata only."""
    return {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "sources_fetched": sources_fetched,
        "candidate_count": candidate_count,
        "selected": [
            {
                "title": item.candidate.title,
                "url": item.candidate.url,
                "source": item.candidate.section,
                "category": item.relevance,
            }
            for item in items
        ],
        "model": model,
        "prompt_tokens": usage.prompt_tokens,
        "output_tokens": usage.output_tokens,
    }


def append_run(
    record: dict[str, Any],
    *,
    path: Path = ARCHIVE_PATH,
    create_parents: bool = True,
) -> None:
    """Append one JSON line. FAIL-SOFT: any I/O error is swallowed — a
    best-effort logbook must never take down the digest it's logging."""
    try:
        if create_parents:
            path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 - archiving is best-effort, never fatal
        return
