# ai-research-agent

A local-first LangGraph agent that reads the day's AI news so you don't have to
open sixteen tabs. It polls RSS feeds every three hours, deduplicates the same
story told by five outlets, summarises what is left with `gpt-5.6-luna`, ranks
the day in one pass, and serves the result as a single page you read over coffee.

Runs on one machine, in one container, for about **$2.50 a month**.

![The digest](docs/screenshots/digest.png)

---

## Why it exists

Every AI newsletter is either a firehose or somebody else's taste. This is a tool,
not a product: one reader, one page, one language per run, and every editorial
decision it makes is visible and adjustable.

It is also a portfolio piece, so the parts that are usually skipped are not: ADRs
for every expensive choice, tests that cover the failure modes rather than the
happy path, and a design whose rationale is written next to the line it changed.

## How it works

```
                 ┌──────────── FastAPI, one uvicorn worker ─────────────┐
                 │  APScheduler: collect */3h - nothing else on a clock │
                 │  POST /runs/start  ← the button a person presses     │
                 └───────────────────────┬─────────────────────────────-┘
                                         │  run_digest(language, mode)
                                         ▼
  16 RSS feeds ─► collect ─► dedupe ─► enrich ─► [Send ×N] summarize ─► rank ─► persist
   (feedparser)   (ETag,     (canonical  (feed HTML   (one structured   (one call   (summaries,
                   304s)      URL +       → fetch →    call per story,   over the    tokens,
                              rapidfuzz)  Tavily)      tr or en)         whole day)  cost)
                                         │
                                         ▼
                            SQLite (WAL + FTS5): sources · articles · summaries · runs
                                         │
                                         ▼
                        Jinja + HTMX:  /   /archive   /sources   /runs   /search
```

Six nodes, and each one exists because of a specific problem:

| Node | Problem it solves |
|---|---|
| **collect** | Feeds are sliding windows, so polling has to be frequent — and frequent polling gets you banned, so every request carries the stored `ETag`/`Last-Modified` and most come back `304`. |
| **dedupe** | The same launch reaches us from OpenAI, TechCrunch, The Verge and Ars Technica. A canonical-URL unique index catches syndication; `rapidfuzz.token_set_ratio ≥ 85` over normalised titles catches independent write-ups. |
| **enrich** | A linkblog gives a sentence and someone else's link. Three tiers, cheapest first: the feed's own HTML, then trafilatura over the page, then one capped Tavily search. |
| **summarize** | A `Send` fan-out, one branch per story, each returning a validated Pydantic model. A branch that fails becomes an error entry, not a dead run. |
| **rank** | Importance scores were assigned one article at a time, blind to the rest of the day. This is the only step that sees all of it. |
| **persist** | Writes the summaries and closes the run row with tokens, cost and status. |

Read `PLAN.md` for the scope, `DESIGN.md` for why the page looks like that, and
`docs/decisions/` for the six choices that would be expensive to undo.

## Run it

You need Docker and an OpenAI API key. Tavily is optional — without it the
pipeline just skips the third enrichment tier.

```bash
cp env.example.template .env    # then put your OPENAI_API_KEY in it
docker compose up --build
```

Open <http://localhost:8000>. The database is empty on the first start, so press
**çalıştır / run now**: it polls all sixteen feeds, summarises what it finds and
writes the day's note.

After that, the feeds keep being polled every three hours and nothing else
happens by itself. The digest costs money, so a person starts it
([ADR 0015](docs/decisions/0015-the-digest-is-started-by-a-person.md)): `/runs`
leads with when it last ran, when the next one is worth starting and a countdown
to it, and the rail carries the same line on every page. It advises and never
refuses — a second run in one day only summarises what arrived since the first.

The database lives on a named Docker volume rather than in `./data`, because WAL
does not work on a Windows directory mounted into a Linux container
([ADR 0007](docs/decisions/0007-named-volume-for-sqlite.md)). To get at the file:

```bash
docker cp ainews:/app/data/app.db .
```

Without Docker:

```bash
uv sync
uv run ainews digest --language tr
uv run uvicorn ainews.web.app:app --port 8000
```

The CLI is the same code the feed poll and the dashboard's button call:

```bash
uv run ainews collect     # poll the feeds, no LLM, no cost
uv run ainews digest      # the full pipeline
uv run ainews sources     # what is being polled and what it last said
```

## The pages

| | |
|---|---|
| `/` | Today's digest: the day's brief, a topic filter, the top 15, and an expander for everything else summarised. Each story carries its source, why it matters and its topics. |
| `/archive` | Past runs, by day. |
| `/search` | Full-text over every summary ever written (FTS5, prefix-matched so Turkish suffixes stop mattering). |
| `/sources` | Enable, disable or add a feed; last status per source. |
| `/runs` | Every run with its cost, duration and errors. |

![Sources and runs](docs/screenshots/sources.png)

Every page wears the same shell — a left rail that is navigation and nothing
else, and a bar that holds only your own controls: search (`/` or Ctrl-K),
refresh, language, theme. Cost and duration live on `/runs`, with the run they
belong to. The theme follows your system unless you pick one. The choice is a query parameter
first and a cookie second (`?theme=light`), like the language, so it survives a
reload and a screenshot is reproducible from its URL.

![The digest in the light theme](docs/screenshots/digest-light.png)

## What it costs

Measured, not estimated. Two real runs on 2026-09-04:

| | Articles | Tokens (in / out) | Cost | Time |
|---|---|---|---|---|
| First run (a week's backlog) | 136 | 191,960 / 64,126 | **$0.115** | 85s |
| A three-hour delta ("refresh") | 21 | 30,600 / 8,200 | **$0.017** | 43s |

That works out to about **$0.00085 per article**, so a normal day of 100 or so
stories costs roughly **$0.09**, or **~$2.50 a month**. Tavily stays on the free
tier and is capped at 30 credits a day; in practice it is rarely reached, because
two cheaper enrichment tiers run first.

`gpt-5.6-luna` is $0.20 in / $1.20 out per 1M tokens. That price is why every
article gets summarised instead of a cheap title-only filter deciding in advance
what is worth reading — see [ADR 0001](docs/decisions/0001-llm-gpt-5-6-luna.md).

## Design in one line

It is a tool, not a landing page: no hero, no cards, no atmosphere, no scroll
animation. The only hierarchy device is typography, and it is not decorative —
a headline's size and ink come from the story's importance score, low-scoring
items drop their summaries, and the single colour on the page appears only on a
failed run. `DESIGN.md` lists everything that was deleted and why.

## Decisions

| | |
|---|---|
| [0001](docs/decisions/0001-llm-gpt-5-6-luna.md) | `gpt-5.6-luna` for every LLM node |
| [0002](docs/decisions/0002-fastapi-htmx-dashboard.md) | FastAPI + Jinja + HTMX, no Node toolchain |
| [0003](docs/decisions/0003-sqlite-wal-fts5.md) | SQLite in WAL mode, FTS5 for search |
| [0004](docs/decisions/0004-apscheduler-in-process.md) | APScheduler in the API process, one worker |
| [0005](docs/decisions/0005-no-alembic-in-v1.md) | No migration tool in v1 |
| [0006](docs/decisions/0006-no-tailwind.md) | No Tailwind; the mockup's CSS ships as-is |
| [0007](docs/decisions/0007-named-volume-for-sqlite.md) | A named volume for the container's database |
| [0017](docs/decisions/0017-the-language-switch-stops-choosing-the-content.md) | The switch translates the interface; the press chooses the bulletin's language |
| [0019](docs/decisions/0019-evaluation-is-a-sibling-command-not-a-test.md) | Evaluation is a sibling command, not a test; the reader's verdict is the ground truth |

## Known limits

- **One language per run.** The digest is written in `tr` or `en`, not both -
  chosen at the press on `/runs`, next to what it will cost. The switch in the
  bar is the *interface* language and translates nothing but the buttons (ADR
  0017); when the bulletin on screen was written in the other one, the bar says
  so.
- **No migrations** (ADR 0005). A schema change after the archive is worth
  keeping means writing an Alembic baseline first; before then, delete
  `data/app.db` and re-collect.
- **One worker, forever** (ADRs 0003 and 0004). A second uvicorn worker means a
  second scheduler, a second feed poll, and two writers on a database that has
  room for one.
- **Nothing runs while you are away** (ADR 0015). The feeds are polled every
  three hours; the digest waits for a press. A week unopened is a week of
  collected articles and no bulletins - the next press summarises what is still
  inside the collection horizon (`COLLECT_MAX_AGE_DAYS`, seven days) and nothing
  older.
- **Feeds rot.** Anthropic has no official feed, so the seed list uses a
  community mirror. Reddit rate-limits. A source that fails five times running
  disables itself and says so on `/sources`.
- **Cost is an estimate**, computed from token counts. Prompt caching makes the
  real bill lower.
- **No auth.** It binds inside the container and publishes to localhost. Do not
  put it on a network you do not own.
- **WAL needs a real filesystem.** On a mount without shared memory the app
  logs a warning and falls back to the default journal instead of crashing;
  reads then block behind a running digest. ADR 0007.

## Development

```bash
uv sync
uv run pytest          # 295 tests, no API key needed, no network calls
uv run ruff check .
uv run pre-commit install
```

Tests use a fake model and `respx` for HTTP, so the whole suite runs offline in
about six seconds. `langgraph.json` is checked in for `langgraph dev` if you want
to step through the graph in Studio.

### Evaluation

Every claim about output quality is a number a command regenerates, and the
reader's own verdicts on the page (*Doğru · Yanlış* under each story) are the
ground truth the model judge is calibrated against. `pytest` stays offline; the
commands that spend money say so and refuse above a cap.

```bash
uv run ainews eval record --run latest        # a run -> tests/fixtures/runs/<date>_<lang>.json, no key
uv run ainews eval judge --run latest         # 12 sampled summaries on the judge tier, ~$0.05
uv run ainews eval rank-stability --run latest  # 3 shuffled rank calls, Kendall tau, ~$0.01
uv run ainews eval report                     # every number, appended to docs/evals.md
```

Recorded fixtures are checked by `tests/test_evals_checks.py` - word budgets,
ungrounded numerals, tag vocabulary, importance spread, ranker-vs-fallback,
unrepresented fives, the editor's-note shape - with no key and no network.
`ainews eval judge --labelled` prints TPR and TNR against the reader's verdicts
once there are about sixty. At one judged run a day the whole layer costs about
$2 a month on top of the product's $2.50. [ADR 0019](docs/decisions/0019-evaluation-is-a-sibling-command-not-a-test.md)
says why it is shaped this way; [`docs/PLAN-EVALS.md`](docs/PLAN-EVALS.md) is
the plan and [`docs/evals.md`](docs/evals.md) the record.

Screenshots in this README are regenerated with:

```bash
uv sync --group docs && uv run playwright install chromium
uv run python scripts/screenshots.py
```
