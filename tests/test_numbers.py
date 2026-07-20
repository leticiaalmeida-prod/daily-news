from __future__ import annotations

from bot.numbers import fetch_numbers_block

# Canned API responses — the block is tested fully offline by injecting a
# fetch that returns these instead of hitting the network.

_CHAINS = [  # api.llama.fi/v2/chains
    {"name": "Ethereum", "tvl": 60_000_000_000.0},
    {"name": "Solana", "tvl": 12_500_000_000.0},
    {"name": "Tron", "tvl": 7_500_000_000.0},
]

_MARKETS = [  # api.coingecko.com/api/v3/coins/markets
    {"symbol": "btc", "price_change_percentage_24h": 1.2},
    {"symbol": "sol", "price_change_percentage_24h": 8.4},
    {"symbol": "bonk", "price_change_percentage_24h": -12.1},
]

_GLOBAL = {  # api.coingecko.com/api/v3/global
    "data": {
        "total_market_cap": {"usd": 2_410_000_000_000.0},
        "market_cap_percentage": {"btc": 54.2},
    }
}

_FNG = {  # api.alternative.me/fng/?limit=2
    "data": [
        {"value": "72", "value_classification": "Greed"},
        {"value": "67", "value_classification": "Greed"},
    ]
}


def _fake_fetch(responses: dict[str, object]):
    def fetch(url: str) -> object:
        for key, payload in responses.items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected url: {url}")

    return fetch


def _all_good_fetch():
    return _fake_fetch(
        {
            "llama.fi": _CHAINS,
            "coins/markets": _MARKETS,
            "global": _GLOBAL,
            "alternative.me": _FNG,
        }
    )


def test_block_renders_every_line_from_canned_data() -> None:
    block = fetch_numbers_block(fetch=_all_good_fetch())
    # Total TVL = 80.0B (sum of the three chains).
    assert "$80.0B" in block
    # Total market cap $2.41T and BTC dominance 54.2%.
    assert "$2.41T" in block
    assert "54.2%" in block
    # Top gainer SOL +8.4%, worst BONK -12.1% (symbols upper-cased).
    assert "SOL" in block and "8.4%" in block
    assert "BONK" in block and "12.1%" in block
    # Fear & Greed 72 (Greed), +5 vs the previous day (72 - 67).
    assert "72" in block and "Greed" in block
    assert "+5" in block


def test_block_omits_a_failed_source_line_but_keeps_the_rest() -> None:
    """Fail-soft: one dead endpoint drops only its own line — never crashes
    the digest, never blanks the whole block."""
    fetch = _fake_fetch(
        {
            "llama.fi": _CHAINS,
            "coins/markets": _MARKETS,
            "global": _GLOBAL,
            # Fear & Greed omitted -> that fetch raises (KeyError -> AssertionError).
        }
    )
    block = fetch_numbers_block(fetch=fetch)
    assert "$80.0B" in block  # TVL still there
    assert "Greed" not in block  # the F&G line was dropped


def test_block_is_empty_when_every_source_fails() -> None:
    def boom(url: str) -> object:
        raise RuntimeError("network down")

    assert fetch_numbers_block(fetch=boom) == ""


def test_block_never_raises_on_malformed_payloads() -> None:
    """A source that returns an unexpected SHAPE (not just a network error)
    must also be dropped, not crash."""
    fetch = _fake_fetch(
        {
            "llama.fi": {"unexpected": "shape"},
            "coins/markets": "not a list",
            "global": {"data": {}},
            "alternative.me": {"data": []},
        }
    )
    assert fetch_numbers_block(fetch=fetch) == ""


def test_block_caps_and_cleans_untrusted_strings() -> None:
    """A classification/symbol string from an external API is untrusted — a
    newline-injecting or over-long value can't forge extra lines or bloat the
    block (same discipline as the digest prompt fencing)."""
    fetch = _fake_fetch(
        {
            "llama.fi": _CHAINS,
            "coins/markets": [
                {"symbol": "evil\n99. injected", "price_change_percentage_24h": 9.9},
                {"symbol": "x", "price_change_percentage_24h": -1.0},
            ],
            "global": _GLOBAL,
            "alternative.me": {
                "data": [
                    {"value": "50", "value_classification": "Neutral" + "z" * 500},
                    {"value": "50", "value_classification": "Neutral"},
                ]
            },
        }
    )
    block = fetch_numbers_block(fetch=fetch)
    assert "\n99. injected" not in block
    assert "z" * 500 not in block
