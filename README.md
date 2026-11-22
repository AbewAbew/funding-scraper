# Funding Opportunities for Ethiopian CSOs

A free, automated pipeline that collects grant and funding calls from opportunity websites, uses an AI model to
check whether **organisations based in Ethiopia can apply**, and stores the relevant ones (with funder, amount,
deadline, focus areas and a plain-language summary) in a Supabase database.

The goal is to give rural civil society organisations the same view of funding opportunities that well-connected
urban organisations have. It runs once a day on GitHub Actions and costs nothing to operate.

## How it works

```
 Stage 0  maintenance   delete expired / stale opportunities, tidy the queue           (SQL, no AI)
 Stage 1  collect       WordPress REST API of each source -> raw_opportunities queue  (~1 min, no AI)
 Stage 2  process       for each pending post, newest first:
                          1. clearly past deadline in the text?  -> processed_expired  (no AI, whole queue first)
                          2. one AI call: locations, Ethiopia eligibility, applicant types,
                             funder, amount, deadline, focus areas, summary
                          3. deterministic Ethiopia rules (filters.assess_relevance)
                          4. relevant & still open -> processed_opportunities
```

* **Sources** (`config.SOURCES`): Global South Opportunities, Opportunities For Youth, Opportunity Desk. Each is read
  through `/wp-json/wp/v2/posts`, which returns 100 posts per request with full text, so nothing breaks when a site's
  theme changes. To add another WordPress site, add its base URL and category id.
* **AI** (`llm.py`): providers are tried in order and benched when their free quota runs out.
  1. Google **Gemma 4** (`gemma-4-31b-it`) via the Gemini API: free, no billing account needed. It "thinks"
     before answering (~1k tokens, can't be disabled), so calls take ~1 minute and use JSON mode.
  2. **Gemini Flash-Lite** (`gemini-3.5-flash-lite`): same Google key, its own free quota, fast. Used when Gemma is
     overloaded ("high demand" errors are common).
  3. **Groq** (`openai/gpt-oss-120b`), an optional free fallback (about 200k tokens/day, roughly 60 posts).
  4. OpenRouter, an optional last fallback.

  An overloaded provider is paused (15s, doubling up to 5 min) and retried, never dropped for the run. A provider
  is only benched when its daily quota runs out or it keeps failing with real errors.

  Requests are paced client-side (requests/min and tokens/min) to stay inside free tiers.
* **Budgets**: each run stops after `MAX_AI_CALLS_PER_RUN` (400) calls or `MAX_PROCESSOR_MINUTES` (120). Anything left
  stays `pending_analysis` and is handled the next day, so a big backlog is simply spread over a few runs.
* **Failures**: if every provider is down or rate-limited, the post stays pending and is retried tomorrow. If the
  model returns unusable output twice, the post is marked `processed_ai_error` so it can't block the queue.

### Queue statuses (`raw_opportunities.status`)
`pending_analysis` · `processed_relevant` · `processed_irrelevant` · `processed_expired` · `processed_ai_error` ·
`skipped_stale` (too old to be worth an AI call)

## Setup

1. **Database**: in the Supabase SQL Editor run [`sql/schema.sql`](sql/schema.sql). It works on a new project *and*
   upgrades the existing one without deleting data. (The original 2025 scripts are kept in `sql/legacy/`.)
2. **Keys**: get a free Gemini API key at <https://aistudio.google.com/apikey> and, optionally, a Groq key at
   <https://console.groq.com/keys>. Check your project's actual free limits in AI Studio; if they're higher than the
   defaults in `config.py`, raise `GEMINI_RPM` / `GEMINI_TPM`.
3. **GitHub Secrets** (Settings → Secrets and variables → Actions): `GOOGLE_API_KEY`, `SUPABASE_URL`,
   `SUPABASE_SECRET_KEY` (the `sb_secret_...` key from Supabase → Settings → API Keys), and optionally
   `GROQ_API_KEY`, `OPENROUTER_API_KEY`. The legacy `SUPABASE_SERVICE_KEY` (service_role JWT) is still accepted as a
   fallback, but Supabase deprecates it at the end of 2026.
4. **Local runs**: `cp .env.example .env` and fill it in, then:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   pytest                                   # unit tests, no keys needed
   python main.py --dry-run --limit 5       # scrape + analyse 5 posts, print them, write nothing
   python main.py                           # full run
   python main.py --stages collect          # only scrape
   python main.py --max-ai-calls 50         # smaller AI budget
   ```

## Data for front ends

Read `processed_opportunities` with the **publishable** key (`sb_publishable_...`, formerly the anon key). Row Level Security allows read-only access to that
table and nothing else. Useful columns: `title`, `link`, `funder`, `funding_amount`, `deadline` (DATE, NULL when
rolling/unspecified), `raw_deadline_text`, `geographic_scope`, `focus_areas`, `applicant_types`, `summary`,
`published_at`.

Set `DROP_INDIVIDUAL_ONLY=true` to exclude opportunities only open to individuals (scholarships, fellowships).
By default they're kept and tagged in `applicant_types`.

## Keeping it alive on free tiers

* **GitHub** disables scheduled workflows after 60 days without commits. The workflow re-enables itself through the
  API on each scheduled run. If it ever shows as disabled, open the Actions tab and click *Enable workflow*.
* **Supabase** pauses free projects after about a week of inactivity. The daily run counts as activity.
* **Free AI limits change.** If runs start logging "benched", check the limits in AI Studio or Groq, or change
  `GEMINI_MODEL` / `GROQ_MODEL`.

## Project layout

```
main.py              pipeline stages + CLI
scrapers.py          WordPress REST collector
filters.py           no-AI logic: deadline parsing, expired pre-filter, text trimming, Ethiopia rules
ai_analyzer.py       prompt, JSON parsing and validation
llm.py               multi-provider client with rate limiting and fallback
database_manager.py  Supabase access
config.py            all settings (overridable with environment variables)
sql/schema.sql       tables, RLS, bulk-update and maintenance functions
tests/               pytest suite
```
