from __future__ import annotations

import pytest

from bot.sources import (
    SourceConfigError,
    SourceSpec,
    digest_openapi_sources,
    digest_rss_sources,
    interactive_openapi_sources,
    load_sources,
)

# --- The shipped registry: the 4 original sources migrated 1:1 (proof the
# migration is behaviour-identical) plus the first new wave. ---


def test_registry_loads_and_every_entry_is_a_frozen_sourcespec() -> None:
    sources = load_sources()
    assert sources  # non-empty
    assert all(isinstance(s, SourceSpec) for s in sources)
    with pytest.raises(Exception):
        sources[0].name = "mutated"  # type: ignore[misc]  # frozen


def test_original_four_sources_present_unchanged() -> None:
    by_name = {s.name: s for s in load_sources()}

    # NYT — two OpenAPI reads, query-param auth, both interactive + digest.
    top = by_name["NYT Top Stories"]
    assert top.kind == "openapi"
    assert top.auth == "NYT_API_KEY"
    assert top.query_auth_param == "api-key"
    assert top.spec is not None and top.spec.endswith("top_stories_v2.json")
    assert top.allowlist == ("get___section___format_",)

    search = by_name["NYT Article Search"]
    assert search.kind == "openapi"
    assert search.allowlist == ("get__articlesearch_json",)

    # The three original RSS crypto feeds — keyless, digest-only.
    for name, url in (
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("The Block", "https://www.theblock.co/rss.xml"),
        ("Blockworks", "https://blockworks.co/feed"),
    ):
        s = by_name[name]
        assert s.kind == "rss"
        assert s.url == url
        assert s.digest is True
        assert s.interactive is False
        assert s.auth is None


def test_new_rss_feeds_are_wired_as_config() -> None:
    by_name = {s.name: s for s in load_sources()}
    for name, url in (
        ("The Defiant", "https://thedefiant.io/api/feed"),
        ("Solana Foundation News", "https://solana.com/news/rss.xml"),
        ("Agave releases", "https://github.com/anza-xyz/agave/releases.atom"),
    ):
        s = by_name[name]
        assert s.kind == "rss"
        assert s.url == url
        assert s.digest is True
        assert s.interactive is False


def test_rss_specs_carry_a_network_timeout() -> None:
    """The hung-feed fix: every RSS source declares a finite timeout."""
    for s in digest_rss_sources(load_sources()):
        assert s.timeout_s > 0


def test_filters_split_by_kind_and_role() -> None:
    sources = load_sources()
    assert all(s.kind == "rss" and s.digest for s in digest_rss_sources(sources))
    assert all(s.kind == "openapi" and s.digest for s in digest_openapi_sources(sources))
    assert all(s.kind == "openapi" and s.interactive for s in interactive_openapi_sources(sources))


def test_disabled_source_is_excluded_from_every_filter() -> None:
    disabled = SourceSpec(name="Off", kind="rss", url="https://x/y", enabled=False)
    active = SourceSpec(name="On", kind="rss", url="https://x/z", enabled=True)
    assert digest_rss_sources([disabled, active]) == [active]


# --- Fail-closed validation: a malformed entry names the offender. ---


def test_unknown_kind_fails_closed_naming_the_source(tmp_path) -> None:
    toml = tmp_path / "bad.toml"
    toml.write_text('[[source]]\nname = "Weird"\nkind = "grpc"\nurl = "https://x/y"\n')
    with pytest.raises(SourceConfigError, match="Weird"):
        load_sources(toml)


def test_rss_without_url_fails_closed(tmp_path) -> None:
    toml = tmp_path / "bad.toml"
    toml.write_text('[[source]]\nname = "NoUrl"\nkind = "rss"\n')
    with pytest.raises(SourceConfigError, match="NoUrl"):
        load_sources(toml)


def test_openapi_without_spec_fails_closed(tmp_path) -> None:
    toml = tmp_path / "bad.toml"
    toml.write_text('[[source]]\nname = "NoSpec"\nkind = "openapi"\n')
    with pytest.raises(SourceConfigError, match="NoSpec"):
        load_sources(toml)


def test_entry_missing_name_fails_closed(tmp_path) -> None:
    toml = tmp_path / "bad.toml"
    toml.write_text('[[source]]\nkind = "rss"\nurl = "https://x/y"\n')
    with pytest.raises(SourceConfigError):
        load_sources(toml)


def test_openapi_spec_path_is_resolved_absolute(tmp_path) -> None:
    """A relative spec path in the TOML resolves against the repo root, so a
    call from any CWD (Vercel) finds the vendored spec."""
    from pathlib import Path

    for s in load_sources():
        if s.kind == "openapi":
            assert s.spec is not None
            assert Path(s.spec).is_absolute()
            assert Path(s.spec).exists()
