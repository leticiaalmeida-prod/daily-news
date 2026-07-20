"""The source registry — every news source as declarative config, not code.

Sources used to be hardcoded in three places: the ``RSS_FEEDS`` tuple in
``rss.py``, the NYT wiring in ``surfcall_tools.py``, and the call sites in
``api/``. Adding source #6 meant editing code in 2-3 modules. This module
turns that into: add five lines to ``sources.toml``. See ``PRD.md §a``.

The TOML (parsed with stdlib ``tomllib``, no dependency) is read at CALL time,
never import time — same rule as secrets: a bad/missing config must fail at
use with a clear message, not crash every route at import. ``rss.py`` and
``surfcall_tools.py`` stay the fetch ENGINES; this module only decides WHICH
sources exist and hands each engine its parameters.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Repo root (parent of bot/), so a relative spec path in the TOML resolves the
# same from any working directory — a Vercel function does not run from here.
_REPO_ROOT = Path(__file__).parent.parent
SOURCES_PATH = Path(__file__).parent / "sources.toml"

_KINDS = ("rss", "openapi")


class SourceConfigError(Exception):
    """A source registry entry is malformed. Names the offending source (and
    field) so a config typo is a loud, obvious failure — never a silently
    dropped or misbehaving feed."""


@dataclass(frozen=True)
class SourceSpec:
    """One news source. ``kind`` picks the fetch engine; the rest are that
    engine's parameters. Everything here is safe to log (no secret values —
    ``auth`` is a *reference* to a secret, resolved by bot/secrets.py at call
    time, never the secret itself)."""

    name: str
    kind: str  # "rss" | "openapi"
    enabled: bool = True
    digest: bool = True  # feeds the scheduled digest (api/cron.py)
    interactive: bool = False  # exposed to the on-demand /news agent
    # rss
    url: str | None = None
    limit: int = 20
    # openapi
    spec: str | None = None  # absolute path, resolved by load_sources
    allowlist: tuple[str, ...] = ()
    auth: str | None = None  # a bot/secrets.py env-var ref, NOT a value
    query_auth_param: str | None = None  # e.g. NYT's "api-key" query param
    # both
    timeout_s: float = 10.0


def _resolve_spec(raw: str) -> str:
    """A spec path in the TOML may be repo-relative; return it absolute."""
    path = Path(raw)
    return str(path if path.is_absolute() else (_REPO_ROOT / path).resolve())


def _spec_from_entry(entry: dict[str, Any], name: str) -> SourceSpec:
    kind = entry.get("kind")
    if kind not in _KINDS:
        raise SourceConfigError(
            f"source {name!r}: kind must be one of {_KINDS}, got {kind!r}"
        )
    if kind == "rss" and not entry.get("url"):
        raise SourceConfigError(f"source {name!r}: kind 'rss' requires a 'url'")
    if kind == "openapi" and not entry.get("spec"):
        raise SourceConfigError(f"source {name!r}: kind 'openapi' requires a 'spec'")

    spec = entry.get("spec")
    return SourceSpec(
        name=name,
        kind=kind,
        enabled=bool(entry.get("enabled", True)),
        digest=bool(entry.get("digest", True)),
        interactive=bool(entry.get("interactive", False)),
        url=entry.get("url"),
        limit=int(entry.get("limit", 20)),
        spec=_resolve_spec(spec) if spec else None,
        allowlist=tuple(entry.get("allowlist", ())),
        auth=entry.get("auth"),
        query_auth_param=entry.get("query_auth_param"),
        timeout_s=float(entry.get("timeout_s", 10.0)),
    )


def load_sources(path: Path = SOURCES_PATH) -> list[SourceSpec]:
    """Parse the registry. Raises ``SourceConfigError`` (naming the offender)
    on any malformed entry — fail closed, never skip-and-limp. A source with
    ``enabled = false`` is loaded but excluded from every filter below."""
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    entries = data.get("source", [])
    out: list[SourceSpec] = []
    for i, entry in enumerate(entries):
        name = entry.get("name")
        if not name:
            raise SourceConfigError(f"source #{i}: missing required 'name'")
        out.append(_spec_from_entry(entry, name))
    return out


# --- Role filters: which sources feed which path. All exclude disabled. ---


def _enabled(sources: list[SourceSpec]) -> list[SourceSpec]:
    return [s for s in sources if s.enabled]


def digest_rss_sources(sources: list[SourceSpec]) -> list[SourceSpec]:
    return [s for s in _enabled(sources) if s.kind == "rss" and s.digest]


def digest_openapi_sources(sources: list[SourceSpec]) -> list[SourceSpec]:
    return [s for s in _enabled(sources) if s.kind == "openapi" and s.digest]


def interactive_openapi_sources(sources: list[SourceSpec]) -> list[SourceSpec]:
    return [s for s in _enabled(sources) if s.kind == "openapi" and s.interactive]
