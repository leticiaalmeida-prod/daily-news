from __future__ import annotations

import json

from bot.archive import TokenUsage, append_run, build_record
from bot.digest import DigestItem
from bot.models import Candidate


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Resp:
    def __init__(self, usage: _Usage | None) -> None:
        self.usage = usage


def _item(title: str, source: str, relevance: str) -> DigestItem:
    return DigestItem(
        candidate=Candidate(title=title, abstract="a", url=f"https://x/{title}", section=source),
        why="w",
        summary="s",
        topic="AI",
        relevance=relevance,
        explanation_mode="background_context",
        neutral_explanation="n",
    )


def test_token_usage_sums_response_usage_and_ignores_missing() -> None:
    usage = TokenUsage()
    usage.add(_Resp(_Usage(100, 20)))
    usage.add(_Resp(_Usage(50, 10)))
    usage.add(_Resp(None))  # a fake/usage-less response must not crash
    usage.add(object())  # not even a response shape
    assert usage.prompt_tokens == 150
    assert usage.output_tokens == 30


def test_build_record_has_the_expected_keys_and_metadata_only() -> None:
    items = [_item("Story A", "CoinDesk", "must-read")]
    record = build_record(
        sources_fetched=["CoinDesk", "NYT Top Stories"],
        candidate_count=42,
        items=items,
        model="claude-haiku-4-5",
        usage=TokenUsage(prompt_tokens=1000, output_tokens=250),
    )
    assert set(record) == {
        "date",
        "sources_fetched",
        "candidate_count",
        "selected",
        "model",
        "prompt_tokens",
        "output_tokens",
    }
    assert record["candidate_count"] == 42
    assert record["model"] == "claude-haiku-4-5"
    assert record["prompt_tokens"] == 1000
    assert record["output_tokens"] == 250
    assert record["selected"] == [
        {
            "title": "Story A",
            "url": "https://x/Story A",
            "source": "CoinDesk",
            "category": "must-read",
        }
    ]
    # Control-plane discipline: NO article body/abstract, no summary, no
    # "why" — headline metadata only.
    blob = json.dumps(record)
    assert "abstract" not in blob and '"summary"' not in blob and '"why"' not in blob


def test_append_run_writes_exactly_one_parseable_line(tmp_path) -> None:
    path = tmp_path / "digests.jsonl"
    append_run({"date": "2026-07-19", "candidate_count": 3}, path=path)
    append_run({"date": "2026-07-19", "candidate_count": 5}, path=path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["candidate_count"] == 3
    assert json.loads(lines[1])["candidate_count"] == 5


def test_append_run_is_fail_soft_never_raises(tmp_path) -> None:
    """Archiving must never break a digest — an unwritable path is swallowed."""
    unwritable = tmp_path / "nope" / "deep" / "digests.jsonl"  # parent missing
    # Should not raise even though the directory doesn't exist and we don't
    # create it (fail-soft, not fail-loud, for a best-effort logbook).
    append_run({"date": "x"}, path=unwritable, create_parents=False)
