"""The gecko-surf<->LLM seam for NYT (Article Search + Top Stories).

(Crypto coverage comes from RSS instead — see rss.py's docstring for why:
every crypto news API checked, including an earlier Messari integration that
lived in this module, turned out to be gated behind a paid/sales tier. This
module, and its Swagger-2.0-vs-OpenAPI-3.0 workarounds below, is now NYT-only;
the general ``session``/``query_auth`` shape is still deliberately generic in
case a future header-or-query-auth source is added here again.)

Wraps gecko-surf's engine (ingest -> tools -> caller) and exposes it to a
Claude tool-use loop / the digest pipeline. This is the only place the bot
touches gecko-surf, and the only place a source's response is handled — so
it is where the safety boundary lives, mirroring the pattern from
GeckoVision/ayuda-venezuela-bot:

- **Allow-list**: only the specific read operations we actually use are ever
  exposed or callable, per source.
- **Never raises**: ``call`` returns a typed error string, so a bad call
  degrades the caller instead of crashing the bot.
- **Sanitize + cap**: the response is length-capped; a query-string secret
  (see below) is never returned to the caller or logged.

Swagger 2.0 gaps this module works around
-------------------------------------------
NYT's specs are Swagger 2.0 and authenticate via an ``api-key`` QUERY
parameter, which gecko-surf has no injection path for (see ``query_auth``
below) — three Swagger-2.0-vs-OpenAPI-3.0 gaps, none of them config
mistakes, gecko-surf just doesn't read this older format in these spots yet:

1. **Auth is header-only.** ``session.auth_headers()`` is merged straight
   into request headers (``gecko.caller.build_request``); there's no
   query-param injection path.
2. **The query-param safety check doesn't see Swagger 2.0 auth either.**
   ``gecko.tools.auth_location_is_safe`` (meant to refuse auto-injecting a
   secret into a loggable query/path/cookie location) only reads OAS3's
   ``components.securitySchemes``; it silently no-ops on Swagger 2.0's
   ``securityDefinitions``, so it wouldn't have caught #1 either.
3. **``base_url`` comes out empty.** gecko-surf derives ``base_url`` from
   OAS3's ``servers[].url``; Swagger 2.0 encodes the host as separate
   ``host`` / ``basePath`` / ``schemes`` fields, which gecko-surf doesn't
   read. Left alone, every request resolves to a host-less relative URL.

None of these are patched in gecko-surf itself — the workarounds stay
entirely at this layer, and only activate for a spec that needs them:

- **#3** is handled unconditionally for any spec with no ``servers[]``
  entry (i.e. any Swagger 2.0 spec passed in): ``base_url`` is derived from
  ``host``/``basePath``/``schemes`` ourselves. Also always pinned explicitly
  even when ``servers[]`` IS present (see the constructor's own comment) —
  an unrelated gecko-surf gotcha found while this module briefly supported
  Messari, which stays documented here since it applies to any spec passed
  as an in-memory dict, not just a Swagger 2.0 one.
- **#1/#2** only apply when the caller passes ``query_auth`` (NYT's case). A
  dummy, always-present session (``_UnlockSession``) unlocks the auth-gated
  tools in gecko-surf's usability gate (``AgentApiClient._usable_tool_names``)
  — #1's flip side: gecko-surf hides a tool it can't authenticate for. For a
  real network call, the request is built with gecko's own auth injection
  turned OFF (``inject_auth=False``) and the query param is appended
  ourselves right before calling ``gecko.caller.execute``.

Recorded mode is unaffected by any of this — it never reaches auth,
``base_url``, or the wire. Its synthesized example data comes back mostly
empty for NYT specifically, for the same Swagger-2.0-vs-OAS3 reason as #1/#2:
NYT's ``responses.200.schema`` isn't the OAS3
``responses.200.content['application/json'].schema`` shape gecko-surf's
example synthesizer reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from gecko import caller
from gecko.client import AgentApiClient
from gecko.ingest import load_spec
from gecko.mcp_server import McpSurface
from gecko.modes import CallMode

# Tool names as synthesized by gecko-surf from each spec. Neither NYT spec sets
# `operationId`, so gecko-surf derives `{method}_{path}` and sanitizes it (see
# `gecko.tools.tool_name`) — recomputed by hand in scratch testing against the
# vendored specs. Covered by a test that fails loudly if a spec update changes
# the derived name.
ARTICLE_SEARCH_TOOL = "get__articlesearch_json"
TOP_STORIES_TOOL = "get___section___format_"

NYT_READS: set[str] = {ARTICLE_SEARCH_TOOL, TOP_STORIES_TOOL}

_NYT_API_KEY_PARAM = "api-key"


class ToolProvider(Protocol):
    """The seam the agent loop / digest pipeline depend on."""

    def tools_for_llm(self) -> list[dict[str, Any]]: ...
    def call(self, name: str, args: dict[str, Any] | None) -> str: ...


class _UnlockSession:
    """Satisfies gecko's ``AuthSession`` protocol with a non-empty, inert header.
    Used only for a query-auth source (``query_auth`` set — currently NYT).

    Its only job is to make ``AgentApiClient._session_has_auth`` true so the
    auth-gated tools show up as usable. The header it names is NEVER actually
    sent: every live call for a query-auth source goes through
    ``prepare(..., inject_auth=False)``, so gecko never merges this session's
    headers into a real request. Recorded-mode calls don't touch auth/the
    wire at all, so it's inert there too.
    """

    def auth_headers(self) -> dict[str, str]:
        return {"X-Gecko-Unused-Placeholder": "1"}


def _swagger2_base_url(spec: dict[str, Any]) -> str:
    """Derive ``base_url`` from a Swagger 2.0 spec's ``host``/``basePath``/``schemes``
    — the shape gecko-surf's own ``base_url = servers[0]['url']`` doesn't read
    (see module docstring, gap #3). Prefers https when offered."""
    host = spec.get("host", "")
    base_path = spec.get("basePath", "")
    schemes = spec.get("schemes") or ["https"]
    scheme = "https" if "https" in schemes else schemes[0]
    return f"{scheme}://{host}{base_path}"


def _add_query_param(url: str, name: str, value: str) -> str:
    """Append ``name=value`` to ``url``'s query string without disturbing the rest."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append((name, value))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _find_truncatable_list(
    data: Any,
) -> tuple[str | None, list[Any]] | None:
    """Locate the list of items worth trimming inside a source's response
    envelope. Checked shallow, in order: ``data`` itself being a list, or the
    first list-valued field one level inside it — NYT nests its article list
    this way (``data.results`` for Top Stories, ``data.response.docs`` for
    Article Search), never at the top level directly. Returns
    ``(key_or_None, list)`` — ``key`` is ``None`` when ``data`` IS the list."""
    if isinstance(data, list):
        return None, data
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, list):
                return key, val
    return None


def _cap(payload: dict[str, Any], max_chars: int) -> str:
    """Serialize ``payload`` to JSON within ``max_chars``, keeping it VALID.

    Finds the actual list of items to trim (see ``_find_truncatable_list``)
    and drops trailing items from THAT list until it fits, rather than
    byte-truncating the serialized JSON blindly. A real bug found while
    testing this against live NYT data, not a hypothetical: a single Top
    Stories section response is ~65KB, and the old version — which only knew
    how to truncate a list sitting directly at ``payload["data"]``, which no
    real source actually does — fell through to a raw ``text[:max_chars]``
    byte cut that landed mid-string and produced INVALID json, silently
    corrupting every capped response. A ``truncated`` flag (added at the top
    level of OUR wrapper dict, never inside the source's own response shape)
    tells the caller the tail was dropped. Falls back to a byte cap only if no
    list is found anywhere (defensive; not expected for a real source).
    """
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    located = _find_truncatable_list(payload.get("data"))
    if located is not None:
        key, items = located
        keep = list(items)
        while True:
            trimmed_data: Any
            if key is None:
                trimmed_data = keep
            else:
                trimmed_data = {**payload["data"], key: keep}
            trial = json.dumps(
                {**payload, "data": trimmed_data, "truncated": True},
                ensure_ascii=False,
                default=str,
            )
            if len(trial) <= max_chars or not keep:
                return trial
            keep = keep[:-1]
    return text[:max_chars]


class SurfcallTools:
    def __init__(
        self,
        spec_path: str | Path,
        *,
        session: Any,
        mode: CallMode = "recorded",
        allowlist: set[str],
        max_chars: int = 30_000,
        query_auth: tuple[str, str] | None = None,
    ) -> None:
        """``session`` must satisfy gecko's ``AuthSession`` protocol
        (``auth_headers() -> dict[str, str]``) — for NYT (the only source
        wired here) that's ``_UnlockSession`` paired with
        ``query_auth=(param_name, value)``, since gecko-surf can't inject a
        query-string secret natively (see module docstring). A standard
        header-auth source would just need any object whose
        ``auth_headers()`` returns the real header — gecko-surf's own
        injection handles that natively, no ``query_auth`` needed.

        ``max_chars`` default of 30k (~7.5k tokens) is sized for real NYT Top
        Stories responses (~65KB/29 articles for a busy section) to keep a
        useful double-digit number of articles per call rather than the 2-3
        the old 6000-char default left after the truncation-shape bug (see
        ``_cap``) was fixed — still small enough to be a reasonable per-call
        cost for the interactive /news agent, which is the one path that
        actually spends tokens on this (the scheduled digest's fetch_candidates
        only parses this JSON in Python, never sends the raw text to an LLM)."""
        self.mode = mode
        self.allowlist = set(allowlist)
        self.max_chars = max_chars
        self.query_auth = query_auth
        spec = load_spec(str(spec_path))
        # ALWAYS pass base_url explicitly — for two independent reasons:
        #
        # 1. Swagger 2.0 has no `servers[]`; gecko-surf's own base_url
        #    derivation only reads that OAS3 field, so without this every
        #    request would resolve to a host-less relative URL (gap #3, see
        #    module docstring).
        # 2. Even for a proper OAS3 spec with `servers[]` (Messari): we hand
        #    gecko-surf an already-loaded dict, not a URL or a path string, so
        #    its provenance anchor comes out "unverified" UNLESS `base_url` is
        #    passed explicitly (see `gecko.client.AgentApiClient`'s own
        #    docstring on `anchor_for`). An unverified anchor silently
        #    disables live auth injection and DEGRADES a "live" call to
        #    "recorded" (synthesized, fake data) — with no error, no
        #    exception, nothing in the response to flag it happened. Found
        #    this the hard way while verifying Messari: a live call quietly
        #    returned schema-synthesized placeholder data instead of hitting
        #    the network. Always pinning `base_url` here avoids that trap for
        #    any source using gecko's native call path (i.e. everything
        #    except the NYT query-auth workaround, which bypasses this
        #    degradation check by calling `prepare()` directly).
        servers = spec.get("servers") or []
        base_url = servers[0].get("url", "") if servers else _swagger2_base_url(spec)
        self.client = AgentApiClient(spec, base_url=base_url, session=session)
        self._surface = McpSurface(self.client, mode=mode)

    @property
    def tool_names(self) -> set[str]:
        return {t["name"] for t in self.tools_for_llm()}

    def tools_for_llm(self) -> list[dict[str, Any]]:
        """Allow-listed tool defs in the Anthropic tool shape."""
        out: list[dict[str, Any]] = []
        for t in self._surface.list_tools():
            if t["name"] not in self.allowlist:
                continue
            out.append(
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["inputSchema"],
                }
            )
        return out

    def call(self, name: str, args: dict[str, Any] | None) -> str:
        """Execute an allow-listed read and return sanitized, capped JSON. Never raises."""
        if name not in self.allowlist:
            return json.dumps({"error": f"tool not allowed: {name}"})
        args = args or {}
        try:
            if self.mode == "live" and self.query_auth is not None:
                result = self._call_live_query_auth(name, args)
            else:
                # Recorded mode, or a live call against a header-auth source —
                # gecko-surf's native path handles both correctly.
                result = self._surface.call_tool(name, args)
        except Exception:  # noqa: BLE001 - degrade the reply, never crash the bot
            return json.dumps({"error": "could not reach the API right now"})
        if isinstance(result, dict):
            payload: dict[str, Any] = {
                "status": result.get("status"),
                "data": result.get("data"),
            }
        else:
            payload = {"data": result}
        return _cap(payload, self.max_chars)

    def _call_live_query_auth(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Build the request with gecko's OWN auth injection off, add the
        query-string secret ourselves, then execute — see module docstring."""
        assert self.query_auth is not None
        param_name, value = self.query_auth
        req = self.client.prepare(name, args, inject_auth=False)
        req.url = _add_query_param(req.url, param_name, value)
        status, body = caller.execute(req)
        return {"status": status, "data": body}


class MultiSurfaceTools:
    """Combine any number of surfaces behind ONE
    ``ToolProvider`` — duck-types ``SurfcallTools`` (same ``tools_for_llm`` /
    ``call`` / ``tool_names``). Tool names are unique across every spec in use,
    so a name routes unambiguously — no namespacing needed."""

    def __init__(self, surfaces: list[SurfcallTools]) -> None:
        self.surfaces = surfaces
        self._owner: dict[str, SurfcallTools] = {}
        for surface in surfaces:
            for name in surface.tool_names:
                self._owner.setdefault(name, surface)

    @property
    def tool_names(self) -> set[str]:
        return set(self._owner)

    def tools_for_llm(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for surface in self.surfaces:
            out.extend(surface.tools_for_llm())
        return out

    def call(self, name: str, args: dict[str, Any] | None) -> str:
        surface = self._owner.get(name)
        if surface is None:
            return json.dumps({"error": f"tool not allowed: {name}"})
        return surface.call(name, args)


def build_nyt_tools(*, nyt_api_key: str, mode: CallMode = "live") -> MultiSurfaceTools:
    """The NYT surface: Article Search + Top Stories, each scoped to its own
    single tool via ``allowlist`` (defensive even though each spec only has
    one operation today). Query-string auth workaround (see module
    docstring): both specs are Swagger 2.0 and gecko-surf can't inject
    NYT's `api-key` natively."""
    from .config import ARTICLE_SEARCH_SPEC_PATH, TOP_STORIES_SPEC_PATH

    query_auth = (_NYT_API_KEY_PARAM, nyt_api_key)
    return MultiSurfaceTools(
        [
            SurfcallTools(
                ARTICLE_SEARCH_SPEC_PATH,
                session=_UnlockSession(),
                mode=mode,
                allowlist={ARTICLE_SEARCH_TOOL},
                query_auth=query_auth,
            ),
            SurfcallTools(
                TOP_STORIES_SPEC_PATH,
                session=_UnlockSession(),
                mode=mode,
                allowlist={TOP_STORIES_TOOL},
                query_auth=query_auth,
            ),
        ]
    )


