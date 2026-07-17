# morning-news

A Telegram bot that sends a daily news digest — NYT (general) + RSS (crypto:
CoinDesk, The Block, Blockworks) — filtered to your personal and professional
interests, plus an on-demand `/news` check-in anytime. Built on
**[gecko-surf](https://pypi.org/project/gecko-surf/)** — the comprehension
engine that makes the bot first-call-correct against NYT's API without a
hand-written client — following the pattern from
[GeckoVision/ayuda-venezuela-bot](https://github.com/GeckoVision/ayuda-venezuela-bot),
including that repo's own evolution: it started as a Python long-poll bot,
then moved to a Vercel webhook so the bot doesn't depend on any one machine
staying on. This repo follows the same path — built and tested locally first,
now deployed the same way.

## How it works

```
Telegram ──POST──▶ api/webhook.py ──▶ agent.respond ──▶ [Article Search / Top Stories
  (on demand,        (verifies secret    (tool-use loop)   tools], via gecko-surf,
   /news or           token header,                        allow-listed in
   plain text)         rate-limited)                        surfcall_tools.py
                                                              (NYT only — RSS has no
                                                               query capability an
                                                               interactive agent
                                                               could use)

Vercel Cron ──GET──▶ api/cron.py ──▶ run_digest ──▶ Telegram push
  (11:00 UTC =        (verifies                 │
   8am BRT,             CRON_SECRET)             ▼
   see vercel.json)                    NYT Top Stories ─┐
                                        RSS (3 feeds)   ─┴─▶ fetch_candidates ──▶
                                                              filter_candidates
                                                              (1 batched call) ──▶
                                                              comprehend
                                                              (parallel, 1 call/story)
```

Both functions are stateless — no persistent process, no long-polling loop,
no in-process scheduler. Vercel Cron decides *when* the digest fires; Telegram
decides *when* `/news`/messages fire. See "Known gaps" below for the two real
bugs (one silent, one obvious) found while wiring gecko-surf up, and the repo
structure below for what each module owns.

- `bot/spec/` — NYT's own OpenAPI (Swagger 2.0) specs for Article Search and
  Top Stories, vendored from
  [nytimes/public_api_specs](https://github.com/nytimes/public_api_specs).
- `bot/surfcall_tools.py` — the **only** module that touches gecko-surf.
  Allow-lists the two NYT reads, caps/sanitizes responses, never raises. Also
  documents (and works around) real gecko-surf gaps found while wiring this
  up — see the module docstring. In short: NYT authenticates via an `api-key`
  **query parameter**, and gecko-surf's built-in auth injection is
  header-only, so this module builds the request with gecko's auth injection
  turned off and appends `api-key` itself, right before the request goes out.
  The key is never logged, never sent to the LLM, and never echoed back in a
  tool result.
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
  list) against `bot/interests.md`, then one call *per surviving article*
  (run concurrently, `ThreadPoolExecutor` — real wall-clock savings against
  Vercel's function time limit, not just a nicety) produces the actual digest
  content: a "why this matters to you" one-liner, summary, topic tag,
  relevance, and a neutral explanation — de-biased framing for contentious
  topics, background context for unfamiliar ones.
- `bot/agent.py` — a manual Claude tool-use loop (bounded, injectable LLM) for
  the on-demand `/news` command.
- `bot/bot.py` — pure Telegram message/command logic (routing, rate
  limiting) — no transport, no network calls. Transport lives in `api/`.
- `bot/telegram_api.py` — a minimal synchronous Telegram Bot API client
  (`sendMessage`, `setWebhook`, message chunking). Not `python-telegram-bot`:
  once there's no long-polling loop or `JobQueue` to justify an async
  framework, plain HTTP calls (`httpx`) are simpler and fit a stateless
  serverless handler directly, no event-loop bridging needed.
- `api/webhook.py` — Vercel serverless function, Telegram's webhook target.
  Verifies the `X-Telegram-Bot-Api-Secret-Token` header before trusting a
  request body (a real, new attack surface a webhook has that long-polling
  never did — long-polling only ever pulled from Telegram, never exposed a
  public endpoint). Keeps a rate limiter at MODULE level (not per-request) —
  same pattern as `ayuda-venezuela-bot`'s own webhook route — so it persists
  across Vercel's "warm" function reuse; best-effort, not guaranteed across
  cold starts, but real protection most of the time beats none.
- `api/cron.py` — Vercel serverless function, the scheduled digest. Verifies
  Vercel's own `CRON_SECRET` convention (auto-sent as
  `Authorization: Bearer <CRON_SECRET>` on real cron invocations).
- `vercel.json` — the digest schedule lives HERE, not in Python: Vercel Cron
  is UTC-only, so `"0 11 * * *"` encodes 8am Brasília time (BRT is UTC-3
  year-round — Brazil doesn't observe DST, so this offset never drifts).
- `scripts/set_webhook.py` — one-time (or per-redeploy-URL) helper to
  register the deployed URL with Telegram.
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
- A Vercel account, project linked to this repo

No key needed for crypto coverage — the RSS feeds are public.

```bash
uv sync
cp .env.example .env   # fill in the required values (see the file's comments)
```

Generate `TELEGRAM_WEBHOOK_SECRET` and `CRON_SECRET` yourself:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

`.env` is loaded automatically on startup (`python-dotenv`, wired in
`bot/config.py`) for local tooling (`scripts/set_webhook.py`, tests). It is
**not** deployed to Vercel (`.vercelignore`/`.gitignore`) — set the same
values as real Environment Variables in the Vercel project instead.

`bot/interests.md` already has real content (your priorities); revisit it any
time your interests change — it's read fresh on every run.

### Deploy

```bash
npx vercel link        # first time only
npx vercel env add TELEGRAM_BOT_TOKEN
npx vercel env add TELEGRAM_WEBHOOK_SECRET
npx vercel env add CRON_SECRET
npx vercel env add NYT_API_KEY
npx vercel env add ANTHROPIC_API_KEY
npx vercel --prod
uv run python scripts/set_webhook.py https://<your-deployment>.vercel.app
```

Then message the bot `/start` in Telegram — it replies with your chat ID. Add
that as `TELEGRAM_CHAT_ID` in Vercel's env vars too (redeploy), so
`api/cron.py` knows where to push the daily digest. Without it, `/news` still
works — `api/cron.py` just skips sending, no error.

### Tests

```bash
uv run pytest              # offline, $0 — no network, no LLM spend (RSS is mocked)
uv run ruff check .
uv run mypy bot api scripts
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
