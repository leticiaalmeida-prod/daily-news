"""The deterministic market-numbers block — a compact factual header above the
digest (total DeFi TVL, market cap, BTC dominance, top movers, Fear & Greed).

Why it lives OUTSIDE the LLM pipeline: these are numbers. There is nothing to
summarise and nothing to de-bias, so they must NOT pass through
``filter_candidates``/``comprehend`` — running them through the model would
only add cost and a chance to hallucinate a figure. ``fetch_numbers_block``
produces the finished text; ``run_digest`` prepends it verbatim.

Every source is keyless and fail-SOFT: one dead or malformed endpoint drops
only its own line, never crashes the digest and never blanks the block. The
HTTP transport is injectable so the whole thing tests offline with canned
JSON — no network, no spend (the project's Pattern B: a free offline
falsifier first).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

# Keyless endpoints (see PRD.md §a, Lane A).
_CHAINS_URL = "https://api.llama.fi/v2/chains"
_MARKETS_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&order=market_cap_desc&per_page=250&page=1"
    "&price_change_percentage=24h"
)
_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
_FNG_URL = "https://api.alternative.me/fng/?limit=2"

# An external-API string (a coin symbol, a F&G label) is untrusted: collapse
# whitespace so it can't forge a new line, and cap it so it can't bloat the
# block — same discipline as the digest prompt fencing.
_STR_CAP = 24

Fetch = Callable[[str], Any]


def _clean(text: str, cap: int = _STR_CAP) -> str:
    return " ".join(str(text).split())[:cap]


def _default_fetch(timeout_s: float) -> Fetch:
    def fetch(url: str) -> Any:
        resp = httpx.get(url, timeout=timeout_s)
        resp.raise_for_status()
        return resp.json()

    return fetch


def _fmt_usd(value: float) -> str:
    """Compact money: $2.41T / $80.0B / $12.3M (trillions get 2 decimals so
    a day-to-day market-cap move is still visible)."""
    for scale, suffix, decimals in ((1e12, "T", 2), (1e9, "B", 1), (1e6, "M", 1)):
        if value >= scale:
            return f"${value / scale:.{decimals}f}{suffix}"
    return f"${value:,.0f}"


def _tvl_line(fetch: Fetch) -> str | None:
    chains = fetch(_CHAINS_URL)
    if not isinstance(chains, list):
        return None
    total = sum(float(c["tvl"]) for c in chains if isinstance(c, dict) and "tvl" in c)
    if total <= 0:
        return None
    return f"DeFi TVL {_fmt_usd(total)}"


def _market_cap_line(fetch: Fetch) -> str | None:
    data = fetch(_GLOBAL_URL)
    block = data.get("data", {}) if isinstance(data, dict) else {}
    cap = block.get("total_market_cap", {}).get("usd")
    dom = block.get("market_cap_percentage", {}).get("btc")
    if cap is None or dom is None:
        return None
    return f"Crypto mkt cap {_fmt_usd(float(cap))} · BTC dom {float(dom):.1f}%"


def _movers_line(fetch: Fetch) -> str | None:
    markets = fetch(_MARKETS_URL)
    if not isinstance(markets, list):
        return None
    rated = [
        m
        for m in markets
        if isinstance(m, dict)
        and isinstance(m.get("price_change_percentage_24h"), (int, float))
    ]
    if not rated:
        return None
    best = max(rated, key=lambda m: m["price_change_percentage_24h"])
    worst = min(rated, key=lambda m: m["price_change_percentage_24h"])
    return (
        f"Top 24h {_clean(best.get('symbol', '')).upper()} "
        f"{best['price_change_percentage_24h']:+.1f}% · "
        f"worst {_clean(worst.get('symbol', '')).upper()} "
        f"{worst['price_change_percentage_24h']:+.1f}%"
    )


def _fear_greed_line(fetch: Fetch) -> str | None:
    data = fetch(_FNG_URL)
    points = data.get("data", []) if isinstance(data, dict) else []
    if not points:
        return None
    today = points[0]
    value = int(today["value"])
    label = _clean(today.get("value_classification", ""))
    line = f"Fear & Greed {value} ({label})"
    if len(points) > 1:
        delta = value - int(points[1]["value"])
        line += f", {delta:+d} vs yesterday"
    return line


# Each line is fetched independently so one failure can't take down the others.
_LINE_BUILDERS: tuple[Callable[[Fetch], str | None], ...] = (
    _tvl_line,
    _market_cap_line,
    _movers_line,
    _fear_greed_line,
)


def fetch_numbers_block(*, fetch: Fetch | None = None, timeout_s: float = 10.0) -> str:
    """Build the numbers block. Returns ``""`` if every source fails (the
    caller then simply prepends nothing). ``fetch`` is injectable — pass a
    canned lookup in tests; the default hits the real keyless endpoints with a
    timeout."""
    get = fetch or _default_fetch(timeout_s)
    lines: list[str] = []
    for build in _LINE_BUILDERS:
        try:
            line = build(get)
        except Exception:  # noqa: BLE001 - one dead source drops its line, never the block
            line = None
        if line:
            lines.append(line)
    if not lines:
        return ""
    return "📊 Markets\n" + "\n".join(lines)
