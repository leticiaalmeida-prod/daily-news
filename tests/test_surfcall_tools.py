from __future__ import annotations

import json
from unittest.mock import patch

from bot.surfcall_tools import (
    ARTICLE_SEARCH_TOOL,
    TOP_STORIES_TOOL,
    MultiSurfaceTools,
    SurfcallTools,
    _cap,
    _find_truncatable_list,
    _UnlockSession,
    build_nyt_tools,
)

ARTICLE_SEARCH_SPEC = "bot/spec/article_search_v2.json"
TOP_STORIES_SPEC = "bot/spec/top_stories_v2.json"

_NYT_QUERY_AUTH = ("api-key", "k")


def test_recorded_mode_exposes_only_allowlisted_tool() -> None:
    tools = SurfcallTools(
        ARTICLE_SEARCH_SPEC,
        session=_UnlockSession(),
        mode="recorded",
        allowlist={ARTICLE_SEARCH_TOOL},
        query_auth=_NYT_QUERY_AUTH,
    )
    names = {t["name"] for t in tools.tools_for_llm()}
    assert names == {ARTICLE_SEARCH_TOOL}


def test_call_rejects_non_allowlisted_tool_without_raising() -> None:
    tools = SurfcallTools(
        ARTICLE_SEARCH_SPEC,
        session=_UnlockSession(),
        mode="recorded",
        allowlist={ARTICLE_SEARCH_TOOL},
        query_auth=_NYT_QUERY_AUTH,
    )
    result = json.loads(tools.call("some_other_tool", {}))
    assert "error" in result


def test_call_never_raises_on_backend_failure() -> None:
    tools = SurfcallTools(
        ARTICLE_SEARCH_SPEC,
        session=_UnlockSession(),
        mode="live",
        allowlist={ARTICLE_SEARCH_TOOL},
        query_auth=_NYT_QUERY_AUTH,
    )
    with patch.object(tools, "_call_live_query_auth", side_effect=RuntimeError("boom")):
        result = json.loads(tools.call(ARTICLE_SEARCH_TOOL, {"q": "x"}))
    assert "error" in result


def test_live_call_injects_api_key_into_query_not_headers() -> None:
    tools = SurfcallTools(
        ARTICLE_SEARCH_SPEC,
        session=_UnlockSession(),
        mode="live",
        allowlist={ARTICLE_SEARCH_TOOL},
        query_auth=("api-key", "SECRET"),
    )
    captured = {}

    def fake_execute(req, transport=None):
        captured["url"] = req.url
        captured["headers"] = dict(req.headers)
        return 200, {"response": {"docs": []}}

    with patch("bot.surfcall_tools.caller.execute", fake_execute):
        tools.call(ARTICLE_SEARCH_TOOL, {"q": "AI regulation"})

    assert captured["url"].startswith("https://api.nytimes.com/svc/search/v2/articlesearch.json?")
    assert "api-key=SECRET" in captured["url"]
    assert "SECRET" not in captured["headers"].values()


def test_top_stories_base_url_resolved_from_swagger2_host() -> None:
    tools = SurfcallTools(
        TOP_STORIES_SPEC,
        session=_UnlockSession(),
        mode="live",
        allowlist={TOP_STORIES_TOOL},
        query_auth=_NYT_QUERY_AUTH,
    )
    assert tools.client.base_url == "https://api.nytimes.com/svc/topstories/v2"


def test_multi_surface_tools_routes_by_name() -> None:
    combined = MultiSurfaceTools(
        [
            SurfcallTools(
                ARTICLE_SEARCH_SPEC,
                session=_UnlockSession(),
                mode="recorded",
                allowlist={ARTICLE_SEARCH_TOOL},
                query_auth=_NYT_QUERY_AUTH,
            ),
            SurfcallTools(
                TOP_STORIES_SPEC,
                session=_UnlockSession(),
                mode="recorded",
                allowlist={TOP_STORIES_TOOL},
                query_auth=_NYT_QUERY_AUTH,
            ),
        ]
    )
    assert combined.tool_names == {ARTICLE_SEARCH_TOOL, TOP_STORIES_TOOL}
    args_by_tool = {
        ARTICLE_SEARCH_TOOL: {},
        TOP_STORIES_TOOL: {"section": "technology", "format": "json"},
    }
    for name, args in args_by_tool.items():
        result = json.loads(combined.call(name, args))
        assert "error" not in result


def test_build_nyt_tools_factory() -> None:
    combined = build_nyt_tools(nyt_api_key="k", mode="recorded")
    assert combined.tool_names == {ARTICLE_SEARCH_TOOL, TOP_STORIES_TOOL}


# --- _cap: regression coverage for the truncation-shape bug found via a real
# live NYT call (a ~65KB Top Stories response got byte-truncated mid-string,
# producing invalid JSON, because the real article list lives one level
# inside `data` — e.g. `data.results` — never at the top level directly) ---


def test_find_truncatable_list_top_level() -> None:
    assert _find_truncatable_list([1, 2, 3]) == (None, [1, 2, 3])


def test_find_truncatable_list_nested_like_nyt_top_stories() -> None:
    assert _find_truncatable_list({"section": "tech", "results": [1, 2, 3]}) == (
        "results",
        [1, 2, 3],
    )


def test_find_truncatable_list_none_found() -> None:
    assert _find_truncatable_list({"error": "no list here"}) is None


def test_cap_truncates_nested_list_and_stays_valid_json() -> None:
    payload = {
        "status": 200,
        "data": {"results": [{"title": f"story {i}", "body": "x" * 200} for i in range(50)]},
    }
    capped = _cap(payload, max_chars=2000)
    parsed = json.loads(capped)  # would raise if the old byte-truncation bug regressed
    assert len(capped) <= 2000
    assert parsed["truncated"] is True
    kept = parsed["data"]["results"]
    assert 0 < len(kept) < 50
    # Kept items are a PREFIX (oldest/least-relevant dropped from the tail),
    # not corrupted mid-item.
    assert kept == payload["data"]["results"][: len(kept)]


def test_cap_no_truncation_needed_returns_payload_unchanged() -> None:
    payload = {"status": 200, "data": {"results": [{"title": "one story"}]}}
    capped = _cap(payload, max_chars=10_000)
    assert json.loads(capped) == payload
    assert "truncated" not in json.loads(capped)


def test_cap_falls_back_to_byte_truncation_when_no_list_found() -> None:
    payload = {"status": 200, "data": {"error": "x" * 5000}}
    capped = _cap(payload, max_chars=100)
    assert len(capped) == 100  # last-resort path only, no valid-JSON guarantee here
