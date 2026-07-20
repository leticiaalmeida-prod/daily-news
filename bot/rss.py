"""RSS-based crypto news source (CoinDesk, The Block, Blockworks).

Picked after every dedicated crypto news API checked turned out to be
gated behind a paid/sales tier: Messari's News API 403s with "Your Enterprise
team does not have access to this endpoint" even on a real paid-signup key
(see surfcall_tools.py's history / project memory); CryptoCompare/CCData and
CoinGecko's news endpoints are the same story. RSS feeds from reputable
outlets sidestep all of that — publicly syndicated, no API key, no sales
gate — and since we pick the specific outlets ourselves rather than filtering
an aggregator's mixed-quality firehose (the CryptoPanic alternative), the
reputation bar is enforced by WHICH feeds we choose, not by post-hoc
filtering of what an aggregator happened to include.

Verified live 2026-07-16: all three feeds return current-day articles.
CoinDesk and The Block are RSS 2.0; Blockworks is Atom — different XML
schemas, which is exactly why this uses ``feedparser`` (normalizes both)
rather than hand-rolled XML parsing.

Deliberately does NOT go through gecko-surf — RSS is XML, not a REST API
gecko-surf comprehends. This is the one source in the bot that doesn't
dogfood Gecko; NYT and the on-demand /news agent still do. RSS also isn't
wired into /news — it only feeds the scheduled digest's candidate list,
since RSS has no query/search capability an interactive agent could use
(see fetch_candidates in digest.py for the analogous NYT-only on-demand
scope).
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import Iterator

import feedparser

from .models import Candidate

# Which feeds to pull is now config, not code — see bot/sources.toml and
# bot/sources.py. This module stays the pure RSS fetch ENGINE: callers pass
# the (name, url) pairs; the digest builds them from the registry.

RSS_LIMIT_PER_FEED = 20
RSS_TIMEOUT_S = 10.0


@contextmanager
def _socket_timeout(seconds: float) -> Iterator[None]:
    """Bound feedparser's network fetch with a timeout. feedparser.parse(url)
    fetches via urllib with NO timeout by default, so a hung feed would stall
    the whole (unattended, time-limited) cron run. urllib honours the default
    socket timeout, so set it for the duration of one parse and restore it."""
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


def fetch_rss_candidates(
    feeds: tuple[tuple[str, str], ...],
    *,
    limit_per_feed: int = RSS_LIMIT_PER_FEED,
    timeout_s: float = RSS_TIMEOUT_S,
) -> list[Candidate]:
    """Pull the most recent entries from each feed. Never raises — a feed
    that's unreachable, times out, or fails to parse is silently skipped
    (feedparser itself doesn't raise on a bad URL/malformed XML; it returns an
    empty ``entries`` list with `bozo` set, which naturally yields zero
    candidates here, but network errors at the transport layer are still
    guarded defensively since this runs unattended on a schedule).
    ``timeout_s`` bounds each feed's network fetch (see ``_socket_timeout``)."""
    candidates: list[Candidate] = []
    for source_name, url in feeds:
        try:
            with _socket_timeout(timeout_s):
                parsed = feedparser.parse(url)
            entries = parsed.entries[:limit_per_feed]
        except Exception:  # noqa: BLE001 - one bad feed shouldn't skip the rest
            continue
        for entry in entries:
            title = entry.get("title") or ""
            if not title:
                continue
            candidates.append(
                Candidate(
                    title=title,
                    abstract=entry.get("summary") or "",
                    url=entry.get("link") or "",
                    section=source_name,
                )
            )
    return candidates
