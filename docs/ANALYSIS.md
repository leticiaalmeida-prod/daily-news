# daily-news — code review & analysis

Reviewed on branch `feat/gecko-review`, against `main` @ `4f8c63d`.
Toolchain results on `main` before any change: **`uv run pytest -q` → 75 passed
(~0.7s, fully offline, $0 — no network, no LLM spend)**; `ruff check .` clean;
`mypy bot api scripts` clean.

## Verdict

**B+ as shipped, A- after this branch.** This is genuinely well-built
software: clean layering, honest documentation, real regression tests grown
from real bugs, and security thinking (webhook secret verification, redacted
config errors) that many production bots don't have. The gap between B+ and
A- was two shipped security holes — a bot-token leak path into Vercel logs
and unfenced LLM prompts over untrusted feed text — both fixed on this
branch. The remaining gaps (no run history, hardcoded sources) are missing
*features*, not flaws, and are specced in `docs/PRD.md`.

## Architecture as built

```
                       ┌─────────────────────────────── Vercel ───────────────────────────────┐
 Telegram user         │  api/index.py (WSGI shim — the ONE Python entrypoint, routes by path)│
   │  /news, text      │        │                                  │                          │
   └──POST /api/webhook┼──▶ api/webhook.py                    api/cron.py ◀──GET /api/cron────┼── Vercel Cron
                       │   verify secret header               verify CRON_SECRET              │   (11:00 UTC
                       │   RateLimiter (warm, module-level)        │                          │    = 8am BRT)
                       │        │                                  │                          │
                       └────────┼──────────────────────────────────┼──────────────────────────┘
                                ▼                                  ▼
                          bot/bot.py (pure routing)          bot/digest.py
                                │                              fetch_candidates ──▶ NYT Top Stories
                          bot/agent.py                         fetch_rss_candidates ─▶ 3 RSS feeds
                          bounded tool-use loop                filter_candidates (1 batched LLM call)
                                │                              comprehend (1 LLM call/story, thread pool)
                                ▼                              format_digest
                    bot/surfcall_tools.py ◀────────────────────────┘ (NYT fetch only)
                    the ONLY gecko-surf seam:
                    allow-list · never-raises · _cap (valid-JSON truncation)
                    Swagger-2.0 workarounds (base_url, query-param auth)
                                │
                                ▼
                    NYT Article Search + Top Stories (api-key appended at the
                    last moment, never logged, never shown to the LLM)

     bot/config.py  — BotConfig.from_env (call-time, never import-time) + system prompt
     bot/secrets.py — [this branch] keychain→env secret resolution
     bot/models.py  — Candidate (shared shape; breaks digest↔rss circular import)
     bot/telegram_api.py — minimal sync Bot API client (send, chunking, set_webhook)
     bot/interests.md — the personal relevance profile, read fresh every run
```

Both entrypoints are stateless functions; scheduling lives in `vercel.json`
(Vercel Cron), incoming traffic lives with Telegram. There is no database, no
queue, no persistent process — a deliberate and appropriate choice at this
scale.

## What's genuinely well done

- **Security posture at the transport edge.** The webhook verifies
  `X-Telegram-Bot-Api-Secret-Token` before trusting a byte of body; cron
  verifies Vercel's `CRON_SECRET` convention. Both fail closed. Config errors
  name only the missing *variable*, never a value. This discipline is real
  and rarer than it should be.
- **The gecko-surf seam is exactly one module.** `surfcall_tools.py` is the
  only file that touches the engine: allow-listed operations, never-raise
  degradation, response capping. The three Swagger-2.0 gaps (and the silent
  live→recorded degradation trap) are documented *where the workaround
  lives*, with reasoning — reference-quality integration writing that fed
  real upstream bug reports.
- **`_cap` is a case study in doing truncation right.** The naive byte-cut
  bug was found against a real 65KB response, fixed by trimming the actual
  item list to keep JSON valid, flagged with `truncated: true`, and pinned
  with regression tests that explain the history.
- **Pure logic, injected effects.** `bot.py` takes a responder + clock;
  `agent.py`/`digest.py` take an injected LLM; the WSGI shim is ~60 lines.
  Result: 75 tests that run offline in under a second with light fakes, not
  mock towers.
- **The two-stage digest is textbook cost/structure design.** One cheap
  batched filter over title+abstract, then per-survivor comprehension calls
  run concurrently against the Vercel time limit — with structured output
  forced via `tool_choice` instead of parse-and-pray free text.
- **Models split (`models.py`) to break a real circular import**, `recorded`
  mode for $0 paths, and module docstrings that explain *why* (including
  decisions like dropping python-telegram-bot on serverless) — the codebase
  teaches its own history.

## Weaknesses / risks, ranked

1. **[FIXED on this branch] Bot token could leak into Vercel logs.** httpx
   formats the request URL into `HTTPStatusError`'s message — and the
   Telegram URL embeds the token (`/bot<token>/sendMessage`). Any failed
   send (bad chat_id, blocked bot, Telegram 5xx) propagated that error out
   of the Vercel function into the deployment logs. Now every Bot API
   failure is a typed `TelegramApiError` carrying only method + status +
   Telegram's token-free description, raised `from None` so the traceback
   chain can't resurrect the URL either.
2. **[FIXED on this branch] Digest prompts were injectable from feed
   content.** `filter_candidates`/`comprehend` interpolated article
   titles/abstracts — text written by strangers, where an RSS `summary` can
   be full-article HTML — with no system prompt, no delimiters, no length
   cap. The on-demand agent had the "tool results are DATA" rule; the
   unattended pipeline had none. Now: `DIGEST_SYSTEM`, `<<<ARTICLES ...
   ARTICLES>>>` fencing, whitespace-collapse + per-field caps, and
   `temperature=0` on the classification stage.
3. **No run history at all.** A digest run leaves zero trace: which sources
   answered, what was fetched, what was filtered in/out, what it cost. When
   a digest is bad (or missing), there is nothing to debug from. → PRD item
   (b), the JSONL archive — the highest-value next feature.
4. **Sources are hardcoded** (`RSS_FEEDS` tuple in `rss.py`; NYT wired
   directly in `surfcall_tools.py`/`webhook.py`/`cron.py`). Adding source #6
   is code, not config. → PRD item (a), the source registry.
5. **No Telegram `update_id` dedup.** Telegram re-delivers an update if the
   webhook doesn't answer fast enough — and this webhook does up to 4 LLM
   iterations plus NYT calls *before* answering. A retried update means a
   duplicate agent run (double spend) and a duplicate reply. Best-effort warm
   dedup (same module-level pattern as the rate limiter) would cover most of
   it.
6. **RSS fetch has no network timeout.** `feedparser.parse(url)` uses the
   default socket timeout (i.e. none); one hung feed can eat the 60s
   function budget and take the whole digest down with it. Fetch bytes with
   httpx (already a dependency, with a timeout) and hand them to feedparser.
   → folded into the registry spec (per-source `timeout_s`).
7. **[FIXED on this branch] Timing-unsafe secret comparisons** (`!=` on both
   endpoints) — now `hmac.compare_digest` over encoded bytes, with the
   missing-header branch tested. Theoretical, but free to fix.
8. Minor nits: `_reply_for` builds the LLM client and ingests both NYT specs
   even for `/help` and rate-limited replies (wasted per-request work — and
   `test_help_command_does_not_touch_llm_or_nyt` slightly overclaims: it
   proves no *network*, not no construction). `interests.md` is a fairly
   personal profile committed to the repo — fine while private; worth
   remembering if the repo ever goes public. `.env*` in `.gitignore` also
   matches `.env.example` (harmless — the file was already tracked).

## Test-suite assessment

**Before this branch: 75 passed in ~0.7s, fully offline, deterministic, $0.**
Genuinely good: negative paths are first-class (wrong secret, invalid JSON,
malformed LLM replies, failing feeds), fakes are light and scripted, and
several tests carry the story of the real bug they pin (`_cap`, the chat-id
first-run flow). The suite runs with no network and no SDK involvement,
which is exactly right for a bot whose failures all live at boundaries.

Gaps found (all covered on this branch — now **96 passed**):

- No secret-hygiene guards (import-time reads, `.env` gitignored,
  secret-shaped literals). Added — and the literal scanner immediately
  caught a too-realistic fake token in one of this branch's own new tests,
  which is the guard working as intended.
- Untested branches: `run_digest`'s empty-filter short-circuit,
  `chunk_message`'s hard-split last resort, `_reply_for` with a missing chat
  id, `handle_webhook` with a missing secret header.
- Nothing asserted what the digest *prompts* contain — the injection fix
  added prompt-content tests (fencing, caps, system/temperature wiring).

## What this branch changed

See `docs/PR-DRAFT.md` for the owner-facing write-up and the commit list.
Everything found-but-not-fixed lives in `docs/PRD.md` as specced next steps,
deliberately not implemented here to keep this branch surgical.
