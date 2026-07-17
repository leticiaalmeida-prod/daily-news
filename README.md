# morning-news

A Telegram bot that sends a daily news digest — NYT (general) + RSS (crypto:
CoinDesk, The Block, Blockworks) — filtered to your personal and professional
interests, plus an on-demand `/news` check-in anytime. Built on
**[gecko-surf](https://pypi.org/project/gecko-surf/)** — the comprehension
engine that makes the bot first-call-correct against NYT's API without a
hand-written client — following the pattern from
[GeckoVision/ayuda-venezuela-bot](https://github.com/GeckoVision/ayuda-venezuela-bot).

## How it works

```
                         ┌─ scheduled daily ─┐
                         │  (PTB JobQueue)    │
                         ▼                    │
NYT Top Stories ─┐                                                    │
RSS (3 feeds)   ─┴─▶ fetch_candidates ──▶ filter_candidates ──▶ comprehend ──▶ digest
  (per section /                          (1 batched call)    (1 call/story)     │
   most recent)                                                                  ▼
                                                                            Telegram push

/news command ──▶ agent.respond ──▶ [Article Search / Top Stories tools],
  (on demand)      (tool-use loop)   via gecko-surf, allow-listed in surfcall_tools.py
                                      (NYT only — RSS has no query capability
                                       an interactive agent could use)
```

- `bot/spec/` — NYT's own OpenAPI (Swagger 2.0) specs for Article Search and
  Top Stories, vendored from
  [nytimes/public_api_specs](https://github.com/nytimes/public_api_specs).
- `bot/surfcall_tools.py` — the **only** module that touches gecko-surf.
  Allow-lists the two NYT reads, caps/sanitizes responses, never raises. Also
  documents (and works around) three Swagger-2.0-vs-OpenAPI-3.0 gaps found
  while wiring this up — see the module docstring. In short: NYT authenticates
  via an `api-key` **query parameter**, and gecko-surf's built-in auth
  injection is header-only, so this module builds the request with gecko's
  auth injection turned off and appends `api-key` itself, right before the
  request goes out. The key is never logged, never sent to the LLM, and never
  echoed back in a tool result.
- `bot/rss.py` — the crypto source. **Not** a gecko-surf tool — RSS is plain
  XML, not a REST API gecko-surf comprehends, so this is the one source that
  doesn't dogfood Gecko. Picked after every dedicated crypto news API checked
  turned out to be gated behind a paid/sales tier (see "Known gaps" below);
  RSS feeds from reputable outlets we pick ourselves (CoinDesk, The Block,
  Blockworks) sidestep that entirely — public, free, no sales gate. Uses
  `feedparser` since the three feeds aren't even the same XML dialect
  (CoinDesk/The Block are RSS 2.0, Blockworks is Atom). RSS only feeds the
  scheduled digest, not `/news` — no query/search capability for an
  interactive agent to call.
- `bot/digest.py` — the two-stage scheduled pipeline: one cheap batched call
  filters every fetched headline/abstract (NYT Top Stories across several
  sections + all three RSS feeds' recent entries, merged into one candidate
  list) against `bot/interests.md`, then one call per surviving article
  produces the actual digest content (a "why this matters to you" one-liner,
  summary, topic tag, relevance, and a neutral explanation — de-biased
  framing for contentious topics, background context for unfamiliar ones).
- `bot/agent.py` — a manual Claude tool-use loop (bounded, injectable LLM) for
  the on-demand `/news` command.
- `bot/bot.py` — thin Telegram transport: long-polling, rate limiting, message
  chunking, and the daily job wired through `python-telegram-bot`'s built-in
  `JobQueue`.
- `bot/interests.md` — **your interests profile.** Plain text, read verbatim
  into every prompt. This is the "finicky prompt" that determines what the
  bot considers worth showing you — priority tiers, exclusions, and the
  Gecko/founder context the "why" field reasons from.

## Setup

You need:
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- An NYT developer key with **Article Search API** and **Top Stories API**
  enabled (developer.nytimes.com → your app → APIs Access)
- An Anthropic API key

No key needed for crypto coverage — the RSS feeds are public.

```bash
uv sync
cp .env.example .env   # fill in the four required values
```

`.env` is loaded automatically on startup (`python-dotenv`, wired in
`bot/config.py`) — `uv run` does NOT do this on its own, so don't rely on
`uv run --env-file` instead; the app loads it itself regardless of how it's
launched (plain `uv run`, Docker, cron).

`bot/interests.md` already has real content (your priorities); revisit it any
time your interests change — it's read fresh on every run.

### Run

```bash
uv run python -m bot
```

First run: message the bot `/start` — it replies with your chat ID. Put that
in `TELEGRAM_CHAT_ID` in `.env` (restart) so the daily digest knows where to
push to. `TELEGRAM_CHAT_ID` is NOT required at startup — the bot runs fine
without it, the scheduled digest job just skips sending until it's set.

`NEWSBOT_MODE=recorded` runs entirely offline at $0 for the NYT side —
gecko-surf synthesizes responses instead of hitting the NYT API (comes back
mostly empty — Swagger 2.0 response shape gecko-surf's synthesizer doesn't
read, see `surfcall_tools.py` docstring — so it's a plumbing check, not a
content preview). RSS always hits the real live feeds regardless of
`NEWSBOT_MODE`, since it's not a gecko-surf call — there's no recorded mode
for it, but it's free either way.

### Tests

```bash
uv run pytest       # offline, $0 — no network, no LLM spend (RSS is mocked)
uv run ruff check .
uv run mypy bot
```

## Known gaps (found while building this)

Vendoring NYT's own OpenAPI specs and wiring them through gecko-surf surfaced
three real gaps in gecko-surf's Swagger 2.0 support (documented in detail in
`bot/surfcall_tools.py`'s module docstring): auth injection is header-only (no
query-param auth), the query-param safety check doesn't read Swagger 2.0's
`securityDefinitions`, and `base_url` isn't derived from Swagger 2.0's
`host`/`basePath`. All three are worked around at the application layer here
(not patched in gecko-surf itself) — worth fixing upstream at some point.

A fourth, more dangerous gap was found (and fixed) while this bot briefly
integrated Messari as the crypto source, before Messari's News API turned out
to be gated behind an Enterprise/sales tier (real 403 on a real signup key —
"Your Enterprise team does not have access to this endpoint") and got
replaced by RSS: passing an already-loaded spec dict (rather than a URL/path
string) leaves gecko-surf's provenance anchor "unverified" unless `base_url`
is passed explicitly — even for a fully OAS3-compliant spec. An unverified
anchor **silently degrades a live call to recorded** (fake synthesized data,
no error at all) rather than failing loudly. `SurfcallTools` always pins
`base_url` explicitly for this reason, whether or not gecko-surf could
technically derive it — see the constructor's own comment.

Also found (not a gecko-surf issue, our own bug): the response-capping
function only knew how to safely truncate a list of items sitting directly at
the top level of a response, but no real source actually shapes its data that
way — it fell back to a raw byte-truncation that could land mid-string and
produce invalid JSON on any large response (confirmed against a real ~65KB
NYT Top Stories call). Fixed in `_cap`/`_find_truncatable_list`.

## License

MIT.
