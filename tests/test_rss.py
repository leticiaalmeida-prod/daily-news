from __future__ import annotations

from unittest.mock import patch

from bot.models import Candidate
from bot.rss import fetch_rss_candidates


class _FakeEntry(dict):
    """feedparser entries support both attribute-ish .get() and dict access;
    a plain dict with .get() is enough for how rss.py reads them."""


def _fake_parsed(entries: list[dict]):
    class _Parsed:
        pass

    p = _Parsed()
    p.entries = entries
    return p


def test_fetch_rss_candidates_parses_entries_across_feeds() -> None:
    def fake_parse(url: str):
        if "coindesk" in url:
            return _fake_parsed(
                [
                    _FakeEntry(
                        title="CoinDesk story",
                        summary="CoinDesk abstract",
                        link="https://coindesk.com/1",
                    )
                ]
            )
        return _fake_parsed(
            [_FakeEntry(title="Block story", summary="Block abstract", link="https://theblock.co/1")]
        )

    with patch("bot.rss.feedparser.parse", side_effect=fake_parse):
        candidates = fetch_rss_candidates(
            feeds=(("CoinDesk", "https://coindesk.example/rss"), ("The Block", "https://theblock.example/rss"))
        )

    assert candidates == [
        Candidate(
            title="CoinDesk story",
            abstract="CoinDesk abstract",
            url="https://coindesk.com/1",
            section="CoinDesk",
        ),
        Candidate(
            title="Block story",
            abstract="Block abstract",
            url="https://theblock.co/1",
            section="The Block",
        ),
    ]


def test_fetch_rss_candidates_respects_limit_per_feed() -> None:
    entries = [_FakeEntry(title=f"story {i}", summary="x", link=f"https://x.com/{i}") for i in range(10)]
    with patch("bot.rss.feedparser.parse", return_value=_fake_parsed(entries)):
        candidates = fetch_rss_candidates(feeds=(("X", "https://x.example/rss"),), limit_per_feed=3)
    assert len(candidates) == 3


def test_fetch_rss_candidates_skips_entries_without_title() -> None:
    entries = [
        _FakeEntry(title="", summary="no title here", link="https://x.com/1"),
        _FakeEntry(title="Real story", summary="s", link="https://x.com/2"),
    ]
    with patch("bot.rss.feedparser.parse", return_value=_fake_parsed(entries)):
        candidates = fetch_rss_candidates(feeds=(("X", "https://x.example/rss"),))
    assert len(candidates) == 1
    assert candidates[0].title == "Real story"


def test_fetch_rss_candidates_skips_a_feed_that_raises() -> None:
    def fake_parse(url: str):
        if "broken" in url:
            raise RuntimeError("network error")
        return _fake_parsed([_FakeEntry(title="OK story", summary="s", link="https://x.com/1")])

    with patch("bot.rss.feedparser.parse", side_effect=fake_parse):
        candidates = fetch_rss_candidates(
            feeds=(("Broken", "https://broken.example/rss"), ("OK", "https://ok.example/rss"))
        )
    assert len(candidates) == 1
    assert candidates[0].section == "OK"
