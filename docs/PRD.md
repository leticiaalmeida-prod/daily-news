# daily-news — next-stage PRD

What the bot should grow next, specced so each item is a bounded change that
keeps the current architecture (stateless Vercel functions, one gecko-surf
seam, pure testable logic) intact. Ordered rationale at the end.

---

## (a) Source registry — sources become config, not code

**Problem.** Sources are hardcoded in three places: the `RSS_FEEDS` tuple in
`bot/rss.py`, the NYT-only wiring in `bot/surfcall_tools.py`
(`build_nyt_tools`), and the call sites in `api/webhook.py` / `api/cron.py`.
Adding source #6 means editing code in 2–3 modules; disabling a flaky feed
means a deploy of code, not config.

**Spec.** One declarative registry, `bot/sources.toml`, parsed with stdlib
`tomllib` into frozen dataclasses at call time (never import time — same rule
as secrets):

```toml
[[source]]
name = "NYT Top Stories"
kind = "openapi"                      # "rss" | "openapi"
spec = "bot/spec/top_stories_v2.json" # openapi only
allowlist = ["get___section___format_"]
auth = "NYT_API_KEY"                  # a bot/secrets.py ref (keychain -> env)
query_auth_param = "api-key"          # optional: NYT's query-param workaround
digest = true                         # feeds the scheduled digest
interactive = true                    # exposed to the /news agent
enabled = true
timeout_s = 15

[[source]]
name = "CoinDesk"
kind = "rss"
url = "https://www.coindesk.com/arc/outboundfeeds/rss/"
digest = true
interactive = false                   # RSS has no query capability
enabled = true
limit = 20
timeout_s = 10
```

```python
@dataclass(frozen=True)
class SourceSpec:
    name: str
    kind: Literal["rss", "openapi"]
    enabled: bool = True
    digest: bool = True
    interactive: bool = False
    url: str | None = None            # rss
    spec: str | None = None           # openapi
    allowlist: tuple[str, ...] = ()
    auth: str | None = None           # secrets.resolve_secret ref
    query_auth_param: str | None = None
    limit: int = 20
    timeout_s: float = 10.0
```

**Behavior rules.**
- Validation fails closed at load with an error naming the offending source
  and field (unknown `kind`, `rss` without `url`, `openapi` without `spec`).
  A disabled source is skipped silently; a *broken* config is loud.
- Dispatch: `kind == "rss"` → the existing `fetch_rss_candidates` (upgraded
  to fetch bytes via httpx **with `timeout_s`** — closing the current
  no-timeout gap — then `feedparser.parse` on the bytes); `kind == "openapi"`
  → a `SurfcallTools` built from `spec` + `allowlist` + the resolved auth.
  `digest=true` sources feed `run_digest`'s candidate list; `interactive=true`
  sources' tools go to the /news agent.
- `auth` names an env var and resolves through `bot/secrets.py` (keychain →
  env), so a new keyed source is `gecko auth set daily_news --account
  <name>` plus five lines of TOML — no Python.

**Migration (behavior-identical at each step).**
1. Add `bot/sources.py` (loader + dataclass) and a `sources.toml` mirroring
   exactly today's four sources; test that the parsed registry equals the
   current hardcoded configuration.
2. Point `run_digest`/`build_nyt_tools` call sites at the registry.
3. Delete `RSS_FEEDS` and the NYT-specific factory.

**Tests.** Registry parse + validation (unknown kind fails closed, disabled
skipped), dispatch-by-kind with fakes, an end-to-end `run_digest` over a
registry of fakes, timeout honored (fake transport that sleeps).

---

## (b) JSONL digest archive — every run leaves a record

**Problem.** The bot keeps no history at all. When a digest is bad, missing,
or suspiciously thin, there is nothing to debug from; there is also no data
to evaluate prompt changes against (did editing `interests.md` help?) and no
cost visibility. Every run is amnesiac.

**Spec.** One JSONL record appended per digest run:

```json
{"ts": "2026-07-18T11:00:04Z", "date": "2026-07-18", "trigger": "cron",
 "mode": "live", "model": "claude-haiku-4-5",
 "sources": [
   {"name": "NYT Top Stories", "ok": true, "fetched": 52, "elapsed_ms": 812},
   {"name": "CoinDesk", "ok": true, "fetched": 20, "elapsed_ms": 340},
   {"name": "The Block", "ok": false, "fetched": 0, "elapsed_ms": 10004}
 ],
 "candidates_total": 87, "filtered": 12, "comprehended": 11,
 "selected": [
   {"title": "…", "url": "https://…", "source": "CoinDesk",
    "relevance": "must-read", "topic": "AI regulation"}
 ],
 "tokens": {"filter": {"in": 14210, "out": 310},
            "comprehend": {"in": 9800, "out": 4100}},
 "duration_ms": 41250, "digest_chars": 4812}
```

- Token counts come from `response.usage` on every Anthropic reply — already
  returned today, currently discarded. Capture and sum per stage.
- **Content discipline:** headline/url/source/category metadata only — never
  article bodies, never secrets. (Titles and URLs are public metadata; this
  keeps the archive small and safe to store anywhere.)

**Storage reality (the honest part).** Vercel's filesystem is ephemeral —
`open("digests.jsonl", "a")` works locally and silently evaporates in prod.
So:
- **Local/dev:** append to `var/digests.jsonl` (gitignored). This alone makes
  manual runs debuggable and is step 1.
- **Prod:** pick one durable sink. Recommended: **Vercel Blob**, one object
  per run (`digests/2026-07-18T11.json`) — no append semantics needed, and a
  small script can concatenate objects back into one JSONL for analysis.
  Zero-infra alternative: send the JSON record to yourself on Telegram as a
  document (the transport already exists); private-repo commit also works but
  couples runs to git credentials.
- The writer is one function (`archive_run(record)`) behind a seam, so the
  sink is swappable and the pipeline never fails because archiving failed
  (best-effort, log-and-continue).

**What it buys.** Debuggability (which feed died, what got filtered out),
reproducibility (re-run the filter over an archived candidate list when
tuning `interests.md`), cost tracking per run, and an eval corpus — after two
weeks there are ~14 labeled runs to measure any prompt change against.

---

## (c) Multi-source dedup — same story, two feeds, one entry

**Problem.** Three crypto feeds regularly cover the same announcement; today
each copy is filtered, comprehended, and shown independently — paying twice
and reading twice.

**Spec — deterministic first, before `filter_candidates` (saves LLM tokens
too):**
1. **URL canonicalization (exact dup):** lowercase host, strip fragment,
   strip tracking params (`utm_*`, `ref`, `cmpid`, …), drop trailing slash.
   Same canonical URL ⇒ same story.
2. **Title near-dup (cross-source):** normalize titles (lowercase, strip
   punctuation), then within the run's candidate list flag pairs with token
   Jaccard ≥ 0.6 or `difflib.SequenceMatcher` ratio ≥ 0.85 as the same
   story. Cheap at ~100 candidates/run (~5k pairs).
3. **Survivor policy:** registry order = source priority (put NYT first,
   then preferred crypto outlets). Keep the survivor, carry
   `also_in: ["The Block"]` on the candidate so the digest can render
   "(also covered by The Block)" — signal, not noise.
4. Record dedup decisions in the archive (b) so the thresholds can be tuned
   from evidence. Only reach for LLM-based dedup if the archive shows the
   deterministic pass actually missing real duplicates — not before.

---

## (d) gecko-surf upgrade path: 0.4.7 → 0.4.13

From the gecko-surf CHANGELOG, what landed in between and what it means here:

| Version | Change | Relevance to daily-news |
|---|---|---|
| 0.4.8 | `gecko login`; real `gecko-surf/<ver>` User-Agent (Cloudflare 1010 fix) | Outbound calls stop looking like `Python-urllib` — matters if a future registry source sits behind Cloudflare. |
| 0.4.9 | **Multi-scheme header auth injection** via `--auth-keychain` | Future *header*-auth sources need none of the NYT query-auth workaround — `SurfcallTools`' generic session path plus the keychain covers them, including two-token APIs. |
| 0.4.10 | Bundled txline surface | Not relevant. |
| 0.4.11 | **`gecko auth test --live`** — proves a credential authenticates, not just resolves | Useful for the keychain entries added on this branch (`daily_news:*`) once a header-auth source exists; NYT's query-param auth still can't be tested through it. |
| 0.4.12 | npx bundle fix; `auth test --live` treats a **silently-degraded live→recorded call as inconclusive** | That silent degradation is exactly the trap documented in `surfcall_tools.py`'s constructor (found via Messari). Upstream now guards it in one path — the app-side `base_url` pinning stays, but the ecosystem is converging on the honest behavior. |
| 0.4.13 | Chain plans in `search_capabilities` (multi-call sequencing) | Not used — the bot calls two single-step tools directly. |

**Key fact:** the three Swagger-2.0 gaps this repo works around (header-only
auth injection, `securityDefinitions` unread by the query-param safety check,
no `host`/`basePath` → `base_url`) are **not fixed in any of these releases**
— the workarounds stay, and the upgrade removes no code.

**Steps.**
1. `pyproject.toml`: `gecko-surf>=0.4.13`; then `uv lock` and regenerate
   `requirements.txt` (`uv export --no-dev --no-hashes ...`) — **Vercel
   deploys from `requirements.txt`, not the lockfile**, so forgetting this
   step means prod silently stays on 0.4.7.
2. `uv run pytest -q` — the tool-name derivation test fails loudly if
   `{method}_{path}` sanitization changed; the base-url test fails if
   Swagger-2.0 handling shifted.
3. One recorded run (`NEWSBOT_MODE=recorded`, $0), then one live `/news`
   smoke. Watch specifically for the seams the workaround relies on:
   `AgentApiClient.prepare(..., inject_auth=False)`, `caller.execute`, and
   the usability-gate behavior `_UnlockSession` satisfies.

Risk: low. The app pins public-ish seams and its tests guard them; the
biggest realistic failure is step 1 being half-done (lockfile vs
requirements.txt drift — which already bit gecko-surf itself, see its 0.4.6
note).

---

## Prompt & context engineering — recommended next steps

The digest fencing (this branch) closed the safety gap; these close the
efficiency/quality gaps, roughly in value order:

1. **Project tool results before the LLM sees them.** The /news agent
   receives up to 30k chars of raw NYT JSON per tool call (`max_chars`
   default). The agent only ever uses title / abstract / url / section /
   byline / date — projecting to those fields in `surfcall_tools.call`
   would cut per-call context by roughly an order of magnitude, with `_cap`
   kept as the backstop. Biggest single token lever in the codebase.
2. **Question-shaped tool descriptions.** The tool defs are derived from
   NYT's spec (`get__articlesearch_json`); a small description override in
   the registry ("Search the NYT archive by keyword — use for *anything
   about X?*" / "Today's top stories by section — use for *what's happening
   now?*") improves first-call tool choice, which is the Gecko thesis
   applied to our own bot.
3. **Prompt caching.** `SYSTEM_PROMPT + interests.md` is a static prefix on
   every call (1 + N calls per digest run). Marking it with Anthropic
   `cache_control` makes the repeated prefix nearly free; `interests.md`
   changes at most daily.
4. **Split the model knob.** One `NEWSBOT_MODEL` drives everything. Filter
   is pure classification (cheapest model, now `temperature=0`);
   comprehension and the /news agent are where quality shows. Two knobs
   (`NEWSBOT_FILTER_MODEL`, `NEWSBOT_MODEL`) with sane defaults.
5. **Wire token accounting into the archive (b)** — `response.usage` is
   already in every reply and currently dropped. This is what turns items
   1–4 from vibes into measured wins.
6. **Decide on /news conversation memory.** `agent.respond` accepts
   `history` but every caller passes none — either add best-effort
   warm-instance history (same pattern as the rate limiter) or remove the
   parameter. Keep the signature honest.

---

## Suggested sequencing

1. **(b) archive** — smallest change, makes everything after it measurable.
2. **(a) registry** — includes the RSS timeout fix; unblocks source #6..#N.
3. **(c) dedup** — tuned with the archive's evidence.
4. **(d) upgrade** — opportunistic, low risk, do alongside any of the above.
5. Context-engineering items 1–3 — guided by (b)'s token numbers.
