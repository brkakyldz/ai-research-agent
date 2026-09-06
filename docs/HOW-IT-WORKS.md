# How it works

A walkthrough of the whole system, from a feed being polled to a story being read
on the page. It is a narrative, not a reference: every value it mentions lives in
the code, and the code is the authority. `PLAN.md` holds the scope, `DESIGN.md`
holds why the page looks like it does, and `docs/decisions/` holds the fifteen
choices that would be expensive to undo.

---

## 1. What the thing is

One process. It polls sixteen RSS feeds, throws away the same story told five
times, summarises what is left with an LLM, ranks the day in a single pass, and
serves the result as one page. It runs on one machine, in one container, for
about $2.50 a month.

There is exactly one thing on a clock: the feed poll, every three hours, which
costs nothing. The digest — the part that spends money — is started by a person
pressing a button (ADR 0015). The page's job is to tell them when that is worth
doing.

```
                 ┌──────────── FastAPI, one uvicorn worker ─────────────┐
                 │  APScheduler: collect */3h — nothing else on a clock │
                 │  POST /runs/start  ← the button a person presses     │
                 └───────────────────────┬─────────────────────────────-┘
                                         │  run_digest(language, mode)
                                         ▼
  16 RSS feeds ─► collect ─► dedupe ─► enrich ─► [Send ×N] summarize ─► rank ─► persist
                                         │
                                         ▼
                  SQLite (WAL + FTS5): sources · articles · summaries · runs
                                         │
                                         ▼
                 Jinja + HTMX:  /   /archive   /sources   /runs   /search
```

---

## 2. The layers, and what each one is not allowed to do

| Layer | Files | Rule it lives by |
|---|---|---|
| Configuration | `src/ainews/config.py` | The only module that reads the environment. Nothing else touches `os.environ`. |
| Sources | `src/ainews/sources/` | Talks to the outside world: HTTP, feed parsing, extraction, Tavily. Knows nothing about the graph. |
| Persistence | `src/ainews/db/` | Five tables plus an FTS5 index. Owns the connection pragmas. |
| Pipeline | `src/ainews/pipeline/` | The LangGraph nodes and the two entry points that open and close a run row. |
| Scheduling | `src/ainews/scheduler.py` | One interval job, in-process. |
| Web | `src/ainews/web/` | Routes, read queries, view helpers, templates. Never writes a summary. |
| CLI | `src/ainews/cli.py` | Calls the same functions the button and the poll call. No second implementation. |
| Evaluation | `src/ainews/evals/` | Reads the database and calls the pipeline's node functions; never imported by the pipeline or the web layer. Spends money only behind `ainews eval judge` / `rank-stability`, never in `pytest`. |

The dependency direction is one-way: web and CLI both call into pipeline, pipeline
calls into sources and db, and nothing calls back up. `pipeline/nodes/summarize.py`
never imports anything from `web/`, which is why the CLI can run a digest with no
HTTP server anywhere in the process.

---

## 3. Configuration

`Settings` (`src/ainews/config.py:23`) is a `pydantic-settings` model read from
the dotenv file once and cached with `lru_cache`. Every knob an operator is meant
to touch is a field on it, and every field has a bound where a bound is meaningful
— `digest_top_n` is `ge=1, le=100`, `dedupe_score_threshold` is `0..100`. A typo
in the environment fails at startup with a validation error naming the field,
rather than at 03:00 as a division by zero.

Two details worth naming:

- **`_blank_placeholders`** (`config.py:74`). The shipped template contains
  `sk-proj-xxxx…`. A key that still holds `xxxx` is treated as unset, so a fresh
  clone that copied the template but never edited it reports "no key" instead of
  authenticating with a placeholder and getting a 401 four nodes deep.
- **`sqlite_path`** (`config.py:91`). Derives the filesystem path from
  `DATABASE_URL`, which is what WAL setup, the health probe and the CLI's
  "database ready at …" line all print.

`openai_model_summarize` is its own knob deliberately (ADR 0001): if Turkish
quality ever disappoints, only the summarize node moves up a tier and ranking
stays on the cheap model.

---

## 4. The data model

Seven real tables in `src/ainews/db/models.py`, plus a virtual one. Two of the
seven belong to the evaluation layer and are described in §14: `verdicts`, the
reader's call on a summary, and `eval_results`, what the judge and the rank
probe measured and what it cost.

**`sources`** (`models.py:49`) — what to poll. Beyond name and URL it carries
`etag` and `modified`, the conditional-GET tokens from the last successful fetch;
`consecutive_failures`, which is what auto-disables a rotted feed; and `weight`
(0.5–2.0), an editorial figure that does two jobs — it breaks ranking ties and it
decides which thin articles are worth a Tavily credit.

**`articles`** (`models.py:81`) — one item from one feed. `url_canonical` is
`unique`, and that uniqueness *is* the first layer of deduplication. `dup_of` is a
self-referencing FK recording a fuzzy-duplicate decision; a duplicate is marked,
never deleted, so a wrong merge stays inspectable instead of becoming an article
nobody can explain the absence of.

**`runs`** (`models.py:112`) — one execution. The id is a uuid hex string and
doubles as the LangGraph checkpointer's `thread_id`, so a crashed run can be
resumed by name. It carries the counts, the token totals, the estimated cost, the
editor's note and the error text. `status` is one of `running | ok | partial |
error`, and `partial` is a real outcome, not a soft failure: one feed 404ed or
three articles would not summarise, and the other ninety-seven are still a digest.

**`summaries`** (`models.py:153`) — the LLM's read of one article, in one
language, for one run. `importance` is 1–5 and check-constrained. `rank` is set
only for the items that made the digest's top N; everything else keeps its
importance and remains reachable below the fold and in search. The unique
constraint `(article_id, run_id, language)` is what forces the deduplication in
`persist` described in §6.7.

**`daily_counters`** (`models.py:186`) — the durable half of the Tavily credit
cap. It is a table rather than a module-level integer for one reason: a process
that restarts twice a day would otherwise reset its own budget twice a day and
spend a month's free credits in a week.

**`summaries_fts`** — an FTS5 virtual table created in `db/schema.py:21` with
`content=''` (contentless: the text lives once, in `summaries`, and the index
stores only terms) plus three triggers that keep it in step on insert, delete and
update. `rowid` is the summary id, so a hit joins straight back. This is DDL
SQLAlchemy has no vocabulary for, which is why it is raw SQL applied by the same
idempotent `init_db()` (`schema.py:54`) that creates the tables.

There is no Alembic (ADR 0005). The schema is created idempotently at every
start; adding migrations later is `alembic init` plus one autogenerate.

### Connection setup

`_apply_pragmas` (`db/session.py:33`) runs on every new connection: WAL,
`synchronous=NORMAL`, `foreign_keys=ON` (SQLite leaves them off by default) and
`busy_timeout=10000` so the single writer waits rather than raising.

The WAL line is wrapped in its own `try` (`session.py:44`) and this is not
defensive habit. WAL needs a shared-memory file beside the database, and a Windows
host directory bind-mounted into a Linux container cannot provide one:
`PRAGMA journal_mode=WAL` raises `disk I/O error` and takes the whole application
down at startup with an error message that names nothing useful. Caught, it logs
what actually happened and points at ADR 0007 — and the tool still works, because
without WAL a reading page merely blocks behind a writing digest, which for one
reader is a pause and not a failure. `docker-compose.yml` uses a named volume for
exactly this reason.

---

## 5. Collection — the cheap half

`collect_articles` (`pipeline/nodes/collect.py:122`) is the single implementation
of "what does collection mean". It is called from two places: the three-hourly
scheduler job, and the first node of the digest graph.

**Conditional GET.** `fetch_feed` (`sources/rss.py:115`) sends the stored `ETag`
and `Last-Modified` back as `If-None-Match` / `If-Modified-Since`. feedparser's
own documentation warns that publishers ban clients that re-download an unchanged
feed, and this one polls sixteen of them eight times a day, so a `304`
(`rss.py:138`) costs nothing and is the normal case. Validators are only
overwritten when the server actually sends them, so a feed that drops its ETag on
one response does not lose ours.

**The User-Agent.** `rss.py:27` sends a browser string. The Verge and Ars Technica
reject the default Python one outright; that constant is what makes those two
feeds exist for us.

**Failure is per-source, never per-run.** Feeds are fetched concurrently with
`asyncio.gather`; a failure marks its own source and increments
`consecutive_failures`, and at `SOURCE_MAX_FAILURES` the source disables itself
(`collect.py:66`). That is the only thing standing between a rotted feed and a
warning line every three hours forever.

Two errors are caught that look like they should not need to be: `httpx.InvalidURL`
is *not* an `HTTPError` — httpx raises it while building the request, before any
transport runs, for a stored link like `http://[::1`. Uncaught it escapes the
`gather` and takes the whole run down over one bad row. The same trap is guarded
in `extract.fetch_article`.

**The age horizon.** `_too_old` (`collect.py:46`) drops entries older than
`COLLECT_MAX_AGE_DAYS` (7). Several feeds serve their entire archive — OpenAI
ships 1169 entries, Hugging Face 859 — and without the horizon the first collect
would summarise years of news at real cost. An item with no date is treated as
current.

**URL canonicalisation.** `canonical_url` (`sources/urls.py:96`) is the first and
cheapest deduplication layer: `utm_*` and friends stripped, `www.`/`m.`/`amp.`
hostnames folded, scheme normalised to https, trailing slash and fragment
dropped. Only *tracking* parameters are removed, from a denylist — a query string
can be load-bearing (`?p=44001` on WordPress), so an allowlist would silently
merge distinct articles. The function never raises: `urlsplit` parses lazily, so
`javascript:void(0)` — which real feeds do carry — only explodes several lines
later when `.port` tries to cast `void(0)` to an int.

With `url_canonical` unique across the table (`collect.py:78`), an article
syndicated to three feeds is stored once and attributed to whichever feed reached
us first.

**Seeding.** `sync_sources` (`sources/seed.py:59`) loads `feeds.yaml` into the
table on every start, and the sync is one-way and additive: a feed in the file
that is missing from the table is inserted, and everything else is left as the
operator left it. A feed disabled in `/sources` therefore stays disabled across
restarts, which it would not if this were an upsert. `BLOCKED_HOSTS`
(`seed.py:33`) refuses Hacker News and Reddit at the host level, even by hand in
the UI — both are firehoses of "title plus someone else's link", and hnrss.org
alone serves a dozen query variants of one feed.

---

## 6. The digest graph

`build_graph()` (`pipeline/graph.py:132`):

```
START → collect → dedupe → enrich → [Send × N] summarize → rank → persist → END
```

Only one edge is interesting, and it is `enrich → summarize`.

### 6.1 State design

`PipelineState` (`pipeline/state.py:102`) carries **ids, not bodies**. LangGraph
writes a checkpoint after every superstep, so anything in state is serialised once
per step per branch; a hundred article bodies in there would turn a cheap run into
a slow one. Bodies stay in SQLite and the graph passes primary keys.

Two fields are reducer fields — `summaries` and `errors`, both
`Annotated[list[...], operator.add]` (`state.py:114`). That is the only reason a
hundred parallel branches can write to one key without clobbering each other.

The LLM's output is a **Pydantic model, not a prompt convention**: `ArticleSummary`
and `RankedDigest` are used with `with_structured_output`, so a malformed answer
fails as a validation error at the node that produced it rather than as a
`KeyError` three nodes later.

### 6.2 collect

`collect_node` (`graph.py:47`) calls the same `collect_articles` the scheduler
calls, and returns `n_collected`, `n_new` and any per-source errors.

### 6.3 dedupe

`dedupe_candidates` (`pipeline/nodes/dedupe.py:102`) does the half the URL index
cannot: the same story written up separately by five outlets — "OpenAI releases
GPT-5.6 Luna", "OpenAI ships new cheap model", "GPT-5.6 Luna is here" — is three
URLs, three rows and one story.

Titles are normalised first (`normalize_title`): the outlet suffix after a dash or
pipe is stripped, aggregator prefixes (`Show HN:`, `Opinion:`) are removed,
punctuation goes, case folds. The comparison is `rapidfuzz.fuzz.token_set_ratio`
at `DEDUPE_SCORE_THRESHOLD` (85), and `token_set_ratio` specifically, because the
failure mode here is a headline that adds or drops words rather than misspelling
them: token set ignores order and duplication and scores on shared vocabulary,
which is exactly the shape of "OpenAI ships X" versus "X shipped by OpenAI today".

A survivor is appended to the reference set as the loop runs (`dedupe.py:139`), so
the third outlet covering a story matches the first rather than sliding through
because the second was already marked.

`_unsummarized` (`dedupe.py:60`) is where the delta lives. A candidate is an
article with no summary in any language in any run, not marked as a duplicate, and
inside the age horizon. That is what makes "run now" idempotent-in-cost: pressing
the button twice in a row summarises nothing the second time, so the page can
advise without ever having to refuse.

### 6.4 enrich

`enrich_articles` (`pipeline/nodes/enrich.py:48`) gives thin articles a body, in
three tiers, cheapest first, each running only when the one before came back
short:

1. **Clean the HTML the feed already sent.** Free. Covers most feeds.
   `clean_html` (`sources/extract.py:42`) runs trafilatura and falls back to a
   naive tag-strip, because on a two-sentence teaser trafilatura sometimes decides
   there is no article at all and returns nothing — and a teaser is better than an
   empty body.
2. **Fetch the page and extract it.** Costs a request. `fetch_article`
   (`extract.py:77`) is synchronous on purpose (trafilatura's own helpers are) and
   the caller runs it in a thread behind a semaphore of 6, so we are not hammering
   a dozen hosts at once.
3. **One capped Tavily news search.** Costs a credit, so it is reserved for items
   that are *still* empty **and** come from a source with `weight >= 1.0`. Below
   that, a thin item is usually thin because it does not say much.

`is_usable` draws the line at 400 characters — below that a "body" is a headline
restated.

The credit cap (`sources/tavily.py:41`) reserves *before* the call, not after:
a request that times out still costs its credit, which matches what Tavily bills
and keeps a failing endpoint from being retried into the monthly allowance. A
failed search returns `""` and degrades the summary; it never fails the run.

Ordering is the whole design here. Enrichment is the only step that can spend
money outside the LLM, and by the time tier 3 is reached most articles no longer
need it.

### 6.5 summarize — the fan-out

`fan_out_summaries` (`graph.py:72`) is a conditional edge that returns a list of
`Send` objects, one per surviving candidate — LangGraph's map-reduce. A hundred
independent branches run in a single superstep, each writing into a state key with
an `operator.add` reducer. With no candidates it returns the string `"persist"`
instead, so an empty day still closes its run row rather than hanging.

Two limits have to be raised for this to work at all, both set in
`runner.py:128`:

- `recursion_limit` counts supersteps and defaults to 25. A `Send` fan-out is one
  superstep however wide it is, so the real depth here is six — but the limit is
  raised to 200 anyway as free insurance against a future node that loops.
- `max_concurrency` is set to `SUMMARIZE_BATCH_SIZE` (10). A hundred simultaneous
  requests collect 429s; throttling is cheaper than retrying.

`summarize_article` (`pipeline/nodes/summarize.py:50`) has three properties that
follow from being a fan-out branch:

- It **reads its article from the database**, not from state, for the checkpoint
  reason above. Body is truncated at 5000 characters — the model's context is far
  larger, but a news summary does not need a whole page and a longer prompt is a
  slower, dearer one.
- It **returns a list of one**, because the field it writes has a concatenating
  reducer.
- It **never raises**. A branch that throws fails the whole graph, so a failed
  article becomes an entry in `errors` and ninety-nine summaries still reach the
  digest. Both the exception path and the "model returned unparsable output" path
  are handled.

The model is called with `with_structured_output(ArticleSummary, include_raw=True)`.
`include_raw` is there specifically so `usage_from_message` (`pipeline/llm.py:56`)
can read `usage_metadata` off the raw response — without the raw message there is
no honest way to cost a run.

### 6.6 rank — one call over the whole day

The importance scores from summarize were each assigned in isolation: one article,
no idea what else happened that day. Five separate 4s are common and mean nothing
relative to each other. `rank_summaries` (`pipeline/nodes/rank.py:58`) is the only
place with the whole day in view, which is what lets it say "these three are the
same event" and "this 4 is really today's 5".

The model is shown a numbered candidate table (source, weight, importance,
headline, summary) and asked for **candidate numbers, not article ids**. Ids are
long, easy to transpose and carry no meaning; short ordinals that exist only
inside one prompt are much harder to get subtly wrong, and validating them is a
range check — anything out of range or repeated is dropped rather than trusted
(`rank.py:92`).

If the call fails, the digest still ships: `_fallback_order` (`rank.py:34`) sorts
by importance, then source weight, which is what a person would do with the same
table, and the editor's note says so in the reader's language.

It also writes `editor_note` — three paragraphs, 25–40 words each. The word count
is deliberate and dated: "one or two sentences" was the ask until 2026-09-05 and
the model answered with whatever length it liked. A number of words is a
constraint it actually honours (`state.py:66`).

The ranking call's tokens ride back on a synthetic payload with `article_id = -1`
(`graph.py:109`), so `persist` can add them to the run's total without a second
state channel.

### 6.7 persist

`persist_run` (`pipeline/nodes/persist.py:35`) is where a run becomes something
the dashboard can read a week later. It writes one `Summary` row per article with
its rank if it made the cut, then closes the run row with counts, tokens, cost,
the note, up to 20 error lines, and `ok` or `partial`.

One subtlety: payloads are collapsed to one per article first
(`persist.py:50`). A retried `Send` — a resumed run, a superstep replayed —
appends its payload a second time, because `summaries` is a concatenating reducer.
The unique index on `(article_id, run_id, language)` would then abort the whole
commit and lose every summary in the run.

Cost is computed here from the token counts each node carried back, priced against
the table in `llm.py:23`. The prices are checked into the repo on purpose: a cost
the dashboard shows has to be reproducible, and a figure fetched at runtime is
not. An unknown model is priced as the most expensive one we know — a surprise in
the billing dashboard is worse than a pessimistic number in ours. The figure is an
estimate and is labelled as one; prompt caching makes the real bill lower.

### 6.8 The two entry points

`pipeline/runner.py` holds the only two ways work starts.

`run_collect` (`runner.py:80`) — the three-hourly poll. No LLM, no cost, no
digest.

`run_digest` (`runner.py:101`) — the full graph, with checkpoints written to
`checkpoints.db`, a *separate* file from `app.db` (`graph.py:15`). They are
machine state with a different lifecycle: deleting checkpoints costs nothing,
deleting `app.db` costs the archive.

Both open a run row before the work and close it afterwards, **including on
failure** (`_fail_run`, `runner.py:70`). A run that crashed and left
`status='running'` forever would be the one thing the `/runs` page could not
explain.

**Concurrency** is one module-level claim: `_digest_lock` plus `_digest_running`
(`runner.py:41`). SQLite has one writer (ADR 0003) and everything shares one
process (ADR 0004), so that is the whole concurrency story — but only if every
path takes it. It lives in the runner rather than the web layer because the CLI
never goes through a route, and two digests over the same candidates means paying
the model twice for one day.

---

## 7. Scheduling — and why there is almost none of it

`build_scheduler` (`scheduler.py:44`) registers exactly one job: the feed poll, on
an `IntervalTrigger` of `COLLECT_INTERVAL_HOURS`. It runs inside the API process
(ADR 0004), which is why `docker-compose` and the Dockerfile both pin a single
uvicorn worker — a second worker would mean a second scheduler.

Two job defaults make a laptop safe as a host. Close the lid over four collect
intervals and APScheduler would otherwise fire four missed polls at once, all
writing to a database with one writer: `coalesce=True` turns those four into one,
and `max_instances=1` stops a slow poll from being overlapped by the next. The
misfire grace is 30 minutes — a poll four hours late is still useful; one from
yesterday is not, because the next cycle covers it.

The job swallows its own exceptions (`scheduler.py:33`): the run row already
records the failure, and a scheduler that dies on one bad night stops every later
night.

**ADR 0015 took the clock off the digest.** `DIGEST_CRON_HOUR`/`MINUTE` became
`DIGEST_SUGGEST_AFTER_HOURS` — an interval the page counts down from, advice
rather than a trigger.

---

## 8. The dashboard

FastAPI + Jinja + HTMX, no Node toolchain (ADR 0002), no Tailwind (ADR 0006), and
no third-party assets at all — fonts and `htmx.min.js` are served from `/static`,
and there is a test that asserts it.

### 8.1 Application lifecycle

`lifespan` (`web/app.py:34`) creates the schema, seeds the feed list, and starts
the scheduler; on the way out it shuts the scheduler down, folds the WAL back into
the database file with `wal_checkpoint(TRUNCATE)` and disposes the engine. Seeding
runs on *every* start, not just the first, so a feed added to `feeds.yaml` in a
later version reaches an existing installation — safe precisely because the sync
is additive.

### 8.2 Routes

| Route | Does |
|---|---|
| `GET /` | Today's digest: the brief, a topic filter, the ranked top N, an expander for everything else summarised. |
| `GET /archive` | Past digest runs, one selected. |
| `GET /search` | FTS5 over every summary ever written. |
| `GET /sources` | Feed list, with enable/disable and add. |
| `GET /runs` | The advice block, the run log, failures, spend. |
| `POST /runs/start` | The only thing that starts a digest in the running app. |
| `GET /runs/status` | HTMX poll while a run is in flight. |
| `POST /sources/toggle`, `POST /sources/add` | Form posts, 303 back to `/sources`. |
| `GET /health` | Database reachable, keys present (never their values). |

### 8.3 The button, end to end

`start_run` (`web/routes/runs.py:95`) refuses for exactly two reasons: no API key,
or a run already in flight. Then:

1. **Claim before creating the task.** `create_task` only schedules; a second
   press arriving before the task's first line would find the claim free and start
   a second — paid — digest. The claim is taken synchronously first.
2. **Fire and forget.** A digest takes about two minutes and an HTTP request that
   waits that long times out somewhere in between. The route returns "working"
   immediately.
3. **Hold a reference to the task.** The event loop keeps only a weak reference,
   so a fire-and-forget digest can be garbage-collected mid-run;
   `_tasks` (`runs.py:46`) is what stops that.
4. **The response is itself the poller.** It returns a `<span>` carrying
   `hx-get="/runs/status" hx-trigger="every 3s"`, which swaps itself every three
   seconds until `run_status` (`runs.py:124`) sees the run is over and returns a
   fragment that reloads the page.

`_guarded` releases the claim in a `finally`, so it is held for exactly as long as
the run lasts however it ends.

### 8.4 The advice

`build_advice` (`web/views.py:310`) is the replacement for the clock. It produces
one of five states:

| State | Meaning | Button |
|---|---|---|
| `running` | a digest is in flight | dead |
| `blocked` | no `OPENAI_API_KEY` | dead |
| `never` | no successful digest yet | live |
| `waiting` | one landed inside the suggested window | live |
| `due` | the window has passed | live |

It **advises and never refuses**: the only two things that actually stop a press
are facts about the machine, not opinions about timing.

The anchor is `last_success_at`, not the last finished run
(`web/queries.py:241`). A digest that ended in an error produced no bulletin, so
it must not push the next suggestion a day into the future — a failed run is
something you are told about and then asked to repeat, not something that counts
as done. `run_history` (`queries.py:263`) does three one-row lookups down the
`started_at` index rather than one pass over recent runs, because "the last
successful digest" can be arbitrarily far back and a week of failures would fall
outside any window a single query picked.

`format_gap` (`views.py:249`) says a span in the units a person would use, coarser
the further out it is. The countdown script in `runs.html` reproduces those
branches exactly — one rule drawn twice, because the server has to render a first
frame that JavaScript then keeps ticking. The *words* are not drawn twice: both
copies read them off `i18n`, which is also why "3h 25m" closes up in English and
"3 sa 25 dk" does not in Turkish — that space is `u_sep`, a translated string like
any other.

The advice object is built once in `shell_context` (`views.py:390`) and read by
two places, the rail's foot on every page and the block at the head of `/runs`, so
the two can never disagree.

### 8.5 Read queries

`web/queries.py` holds every read, apart from the routes, because all five pages
want the same few shapes and a query written twice is a query that disagrees with
itself later.

- `latest_digest_run` (`queries.py:34`) is **not** language-scoped, and neither
  are `digest_runs` or `search_stories`. It was until 2026-09-06, which made the
  bar's TR/EN switch a content filter: with one Turkish bulletin in the
  database, `?lang=en` answered "No digest yet". The switch translates the
  interface and nothing else now (ADR 0017); the page draws the latest bulletin
  whatever language it holds, and the bar names that language when it is not the
  page's.
- `stories_for_run` returns ranked items in the ranker's order, then the rest by
  importance — the same ordering the page's typography expresses, so a reader
  scanning downward sees the ink fade monotonically.
- `topic_pulse` (`queries.py:175`) is the side column's themes list. `share` is
  the percentage of the run's stories carrying a tag, because a count alone does
  not say whether six is most of the day or a corner of it. `rising` is the only
  derived claim on the page and is deliberately dull: at least twice today **and**
  at least half again its own average across the previous week's digests. With no
  prior run, nothing rises — an empty week must not make every topic look like a
  trend.
- `search_stories` (`queries.py:315`) goes through FTS5. `_fts_query` quotes every
  token and appends `*`, which does two jobs: it stops FTS5 treating `AND`, `-` or
  `"` as operators and erroring on an ordinary search, and it makes Turkish
  suffixes stop mattering — "model" then finds "modeli" and "modelleri", which is
  the whole difference between a search box that works in Turkish and one that
  does not.
- `recent_activity` (`queries.py:383`) buckets a month of runs by the reader's
  *local* calendar day in Python rather than with a `GROUP BY`, because the bucket
  is a local date and SQLite would need to be told the offset — a rule that then
  disagrees with `to_local` the first time the timezone setting changes.

Three counting rules were fixed on 2026-09-06 and are worth stating, because each
was a number on the page promising something the link behind it did not deliver:

- `count_ranked` (`views.py:203`) counts rows. It used to be
  `min(n_summarized, digest_top_n)` — a guess, and wrong on a day the ranker
  returns fewer than the cap. On 2026-09-05 a run summarised 136 and ranked 11,
  and the page drew eleven stories under a badge reading 15.
- `tag_counts` takes a `ranked_only` flag that tracks the list the page is
  showing. Counted over every summary while labelling a filter that narrows the
  ranked fifteen, it said `agents 27` on a page of fifteen and returned four when
  pressed.
- `topic_pulse` passes the same flag into its *history* query, so today's ranked
  count is compared against last week's ranked count. Like against like, or the
  comparison is not one.

### 8.6 Language and theme

`web/i18n.py` is two dictionaries of about 120 strings each. Two dictionaries beat
a translation framework at this size, and a missing key is a `KeyError` in a
template render rather than a silent English fallback nobody notices.

Both language and theme resolve by the same three-step rule: an explicit query
parameter wins, then the cookie it set, then the default. The query parameter is
what makes a link shareable and a screenshot reproducible; the cookie is what
makes tomorrow's first visit already right. Because the server knows the theme
before it renders, the theme switch is three plain links — no JavaScript and no
flash of the wrong theme. `system` is not a colour: it renders no `data-theme` at
all and lets `color-scheme: light dark` follow the OS.

Both controls draw every slot and mark the current one, rather than showing only
the state you would switch to — `english` on a Turkish page reads as plausibly
either.

### 8.7 The page itself

The design rationale is `DESIGN.md` and ADRs 0008–0015; the short version:

- **A console shell.** A left rail that is navigation and nothing else, with a
  count under each link so "is there anything new" is answered without a click,
  and its foot carrying the run button, what it does and when it is next worth
  doing. A sticky bar holding only the reader's own controls.
- **No panel around the feed.** Rows sit on the page, separated by air and a
  hairline (ADR 0013).
- **Ranking is carried by the typography.** Headline size and ink step come from
  the importance score, so `p1`–`p5` on the `<li>` is doing the work a badge would
  otherwise do. Below a score of 3 an item collapses to its headline: the reader
  is scanning by then, and a summary they will not read is a summary in the way of
  the next headline they will.
- **The story block never reorders.** Source and age, headline, summary, *why it
  matters* on its own inset plate, topics, source link — the same six things in
  the same order on every item, which is what separates this from a feed reader.
- **The impact meter** is three bars and a word (`impact_band`, `views.py:102`).
  Five bars with no legend was a shape nobody had been told how to read; three
  bands and a name is a scale. The band is computed in the template from data, the
  colours live in `theme.css` on their own tokens — never `--alarm` reused
  (ADR 0012).
- **`lang="en"` on source names** is load-bearing, not decoration. The page is
  `lang="tr"` and a Turkish locale maps `i` to `İ`, so any uppercasing renders
  "OpenAI" as "OPENAİ".

---

## 9. Running it

```bash
cp env.example.template .env    # then put OPENAI_API_KEY in it
docker compose up --build
```

Then <http://localhost:8000> and press **çalıştır / run now**. Without Docker:

```bash
uv sync
uv run ainews init            # create the database, seed the feeds
uv run ainews collect         # poll the feeds — no LLM, no cost
uv run ainews digest          # the full pipeline
uv run ainews sources         # what is polled and what it last said
uv run ainews serve           # the dashboard on HOST:PORT
```

The CLI calls exactly the functions the poll and the button call. `serve` is
handled outside `dispatch` because `uvicorn.run` owns its own event loop and must
not be started from inside one (`cli.py:65`).

To read the database off the container:

```bash
docker cp ainews:/app/data/app.db .
```

---

## 10. Failure modes, and what each one degrades to

| What breaks | What happens |
|---|---|
| One feed 404s | That source is marked; the other fifteen carry on. Five in a row and it disables itself. |
| A stored feed URL is malformed | `InvalidURL` is caught at the fetch; the run continues. |
| Trafilatura finds no article | Falls back to a tag-strip, then to the teaser; the summary says less. |
| No Tavily key, or the cap is hit | Tier 3 is skipped silently; enrichment ends at tier 2. |
| Tavily errors | Credit is still spent (as billed), `""` returned, run continues. |
| One article's summarize call fails | An entry in `errors`; the other ninety-nine ship. Run status is `partial`. |
| The rank call fails | Fallback ordering by importance then source weight; the note says so. |
| The model returns nonsense positions | Out-of-range and repeated ordinals are dropped; empty result falls back. |
| A run crashes | `_fail_run` closes the row as `error`, and the failed run does not reset the countdown. |
| WAL cannot be enabled | Warning naming the cause, and the app runs on the rollback journal. |
| No `OPENAI_API_KEY` | The button is dead and says why; collection still runs. |
| A second press mid-run | Refused, not queued — and a second run later costs nothing for stories already summarised. |

---

## 11. Cost

`gpt-5.6-luna` at $0.20 / $1.20 per million tokens (ADR 0001), one summarize call
per new story with the body capped at 5000 characters, plus one rank call over the
day. Measured at roughly **$2.50 a month** against a $10 budget. Tavily's free
tier is 1,000 credits a month and the daily cap defaults to 30.

Every run row carries its own token counts and estimated cost, and `/runs` totals
them for today, the last 7 days and the last 30.

---

## 12. Tests

`pytest` with `asyncio_mode = "auto"`, against an in-memory or temporary SQLite
database. They cover failure modes rather than the happy path, and the names read
as claims:

- `test_a_source_disables_itself_after_repeated_failures`
- `test_archive_entries_older_than_the_horizon_are_skipped`
- `test_the_same_story_from_three_outlets_survives_once`
- `test_duplicates_are_marked_not_deleted`
- `test_already_summarised_articles_are_never_candidates_again`
- `test_a_failing_search_still_costs_its_credit`
- `test_the_counter_is_stored_not_held_in_memory`
- `test_one_failing_article_does_not_fail_the_run`
- `test_ranking_falls_back_to_importance_when_the_model_fails`
- `test_out_of_range_positions_from_the_model_are_dropped`
- `test_a_sleeping_laptop_produces_one_catch_up_not_four`
- `test_a_filesystem_without_shared_memory_degrades_instead_of_crashing`
- `test_two_simultaneous_presses_start_one_run`
- `test_a_failed_run_does_not_reset_the_countdown`
- `test_search_matches_a_turkish_suffix`
- `test_the_page_loads_no_third_party_assets`

The graph tests run the whole pipeline against a fake model, so a full fan-out,
rank and persist is exercised with no network and no spend.

---

## 13. The decisions, in one list

| ADR | Decision |
|---|---|
| 0001 | `gpt-5.6-luna` for every LLM node |
| 0002 | FastAPI + Jinja + HTMX, no Node toolchain |
| 0003 | SQLite in WAL mode, FTS5 for search |
| 0004 | APScheduler in-process, one uvicorn worker *(partially superseded by 0015)* |
| 0005 | No migration tool in v1; idempotent schema at startup |
| 0006 | No Tailwind; the CSS is the mockup's, hand-written |
| 0007 | The container's database is on a named volume |
| 0008 | A console shell, and a light theme beside the dark one |
| 0009 | The editorial story block; shell split into navigation, controls, record |
| 0010 | The activity column, and a blue secondary colour |
| 0011 | Two faces for small text; the brief moves into the side column |
| 0012 | The impact meter takes the third colour; *why it matters* becomes a plate |
| 0013 | The page stops being a dashboard *(partially superseded by 0014)* |
| 0014 | No lead story, and a side column that looks like a bar |
| 0015 | The digest is started by a person, and the page advises when |
| 0016 | The shell stops whispering: nothing under 12px, sentence case in `i18n.py` |
| 0017 | The switch translates the interface; the press chooses the bulletin's language |
| 0019 | Evaluation is a sibling command, not a test; the reader's verdict is the ground truth |

---

## 14. Evaluation

Everything above produces output; nothing above says whether it is any good.
`docs/PLAN-EVALS.md` is the plan and ADR 0019 the shape; this section is the
mechanism.

**Four layers, cheapest first.**

1. **Deterministic checks over stored rows** (`evals/checks.py`) - pure
   functions, rows in and numbers out: word budgets and the sentence histogram,
   numerals in the summary that the article body did not contain (after a
   Turkish/English normaliser, so "12,9 milyar" meets "$12.9 billion"), the tag
   vocabulary's singleton share, the importance spread, how far the ranker's
   order departs from the free importance-then-weight order, importance-5
   stories with no ranked story in their dedupe cluster, and the editor's-note
   shape. `ainews eval record --run <id>` writes a run to
   `tests/fixtures/runs/<date>_<lang>.json` - deterministic, one story per line,
   numerals taken from the text the model was shown, **never the article body**
   (the repository is public) - and `tests/test_evals_checks.py` asserts bounds
   over every fixture, offline. A fixture recorded before a fix landed is marked
   `xfail(strict=True)` with the reason beside it, so the mark comes off loudly.
2. **The reader's verdict** (`verdicts`, `POST /verdict`, `_story_foot.html`) -
   two words under every story, *Doğru · Yanlış*, saved in place by HTMX with an
   optional one-line reason when it is wrong. Binary, not a scale: a person can
   say "wrong" in one click and cannot honestly say "3". These labels are the
   ground truth everything below is calibrated against.
3. **The sampled grounding judge** (`evals/judge.py`, `ainews eval judge`) -
   `gpt-5.6-terra` at temperature 0 reads the body the summariser read and
   answers one binary question: does the summary state anything as fact the
   text does not support? The why-it-matters line is the editor's inference and
   is failed only for an invented fact, not for drawing a conclusion. Twelve
   summaries a run, seeded; cost estimated from body length and refused above
   `--max-cost` before the first call; one `eval_results` row per judgement.
   `--labelled` judges every summary that carries a verdict and prints TPR and
   TNR separately, never one accuracy figure.
4. **The rank-stability probe** (`evals/stability.py`,
   `ainews eval rank-stability`) - shuffles the candidate table three ways,
   calls `rank_summaries` for each, reports mean pairwise Kendall τ and top-N
   Jaccard. A call that fell back to importance order is counted, not read as
   stability.

`ainews eval report` runs the checks over the live database, reads the verdicts
and the judge and probe rows, and appends one dated section to `docs/evals.md`
with every number beside the function that produced it. A section is never
edited.

**Measured on the first run (2026-09-04, 91 stories, see `docs/evals.md`):**
4.4% of summaries over the 55-word budget, two ungrounded figures (one the
probe had already found by hand), 67.6% tag singletons, ranker/fallback overlap
6 of 11, one importance-5 story left unrepresented by the same-day source purge,
and a rank-stability τ of **0.47** with top-N Jaccard 0.53 - under the 0.6 that
E5 names as the trigger for permutation self-consistency in production. One run
is one measurement; the trigger asks for it across runs.

