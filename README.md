# ai-research-agent

A local-first LangGraph pipeline that reads the day's AI news so you don't have to
open sixteen tabs. It polls RSS feeds every three hours, throws away the same
story told by five outlets, summarises what is left with `gpt-5.6-luna`, ranks
the whole day in one pass, and serves the result as a page you read over coffee.

One machine, one container, about **$2.50 a month**.

![The digest](docs/screenshots/digest.png)

---

## Why it exists

Every AI newsletter is either a firehose or somebody else's taste. This is a
tool, not a product: one reader, one page, and every editorial decision it makes
is visible on screen and adjustable.

Two sentences shape everything below.

**Nothing happens on a clock except the part that is free.** The feed poll runs
every three hours and costs nothing. The digest spends money, so a person starts
it — the page's job is to say when that is worth doing, and then get out of the
way.

**Nothing the model says is taken on trust.** Every summary carries a *doğru ·
yanlış* under it, those labels are the ground truth a sampled grounding judge is
calibrated against, and the numbers live in a dated file the commands
regenerate.

It is also a portfolio piece, so the parts usually skipped are not: tests that
cover the failure modes rather than the happy path, and a reason written next to
every line that needed one.

## How it works

![Architecture](docs/architecture.png)

Six nodes, and each one exists because of a specific problem:

| Node | Problem it solves |
|---|---|
| **collect** | Feeds are sliding windows, so polling has to be frequent — and frequent polling gets you banned, so every request carries the stored `ETag` and most come back `304`. |
| **dedupe** | The same launch reaches us from OpenAI, TechCrunch, The Verge and Ars Technica. A canonical-URL unique index catches syndication; fuzzy title matching catches independent write-ups. |
| **enrich** | A linkblog gives you a sentence and someone else's link. Three tiers, cheapest first: the feed's own HTML, then the page itself, then one capped Tavily search. |
| **summarize** | A `Send` fan-out, one branch per story, each returning a validated Pydantic model. A branch that fails becomes an error entry, not a dead run. |
| **rank** | Importance was scored one article at a time, blind to the rest of the day. This is the only step that sees all of it. |
| **persist** | Writes the summaries and closes the run row with tokens, cost and status. |

The repository is named *agent* and the graph is not one in the tool-calling
sense: nothing in it decides which node runs next. A bulletin needs a fixed
line of six nodes, one fan-out and structured output, and that is what it is.

Every node also writes down what it did — counts in and out, model, tokens, cost,
duration — so a slow or expensive run can be read step by step at `/runs/<id>`.

The long version, from a feed being polled to a story being read, is
[`docs/HOW-IT-WORKS.md`](docs/HOW-IT-WORKS.md).

## Run it

You need Docker and an OpenAI API key. Tavily is optional — without it the
pipeline just skips the third enrichment tier.

```bash
cp env.example.template .env    # then put your OPENAI_API_KEY in it
docker compose up --build
```

Open <http://localhost:8000>. The database is empty on the first start, so go to
**çalışmalar / runs** and press the button: it polls all sixteen feeds,
summarises what it finds and writes the day's bulletin. One click gets a
question, not a run — the confirmation asks which models to use and which
language to write in, with the price per million tokens under them.

After that the feeds keep being polled every three hours and nothing else happens
by itself. `/runs` leads with when the last bulletin landed and a countdown to
when another one is worth starting. It advises and never refuses: a second run
the same day only summarises what arrived since the first, so pressing twice
costs nothing the second time — and the question says how many stories are
waiting before you answer it. A run that failed part-way offers to finish from
its checkpoint inside the same question, rather than summarising the day again.

Without Docker:

```bash
uv sync
uv run ainews digest --language tr
uv run ainews serve
```

The CLI calls exactly the same functions the feed poll and the dashboard's button
call — there is no second implementation:

```bash
uv run ainews collect     # poll the feeds; no LLM, no cost
uv run ainews digest      # the full pipeline
uv run ainews digest --resume <run>   # finish a failed run from its checkpoint
uv run ainews sources     # what is being polled and what it last said
```

## The pages

| | |
|---|---|
| `/` | Today's bulletin: the day's brief, a topic filter with the day's impact spread on the end of it, the top 15, and an expander for everything else summarised. |
| `/archive` | Past bulletins, by day. |
| `/search` | Full-text over every summary ever written (SQLite FTS5, prefix-matched so Turkish suffixes stop mattering). |
| `/sources` | Enable, disable or add a feed; last status per source. |
| `/runs` | The advice block and the press, spend for today / 7 days / 30 days, the week's story counts, and every run with its cost, duration and errors. |
| `/runs/<id>` | One run, node by node: which step took the time, which took the money, which model wrote it. |
| `/runs/verdicts` | The sentences the grounding judge could not find in the article, each with the reader's two words under it, and the reader's own labels. |

![Runs](docs/screenshots/runs.png)

A run is not a status word. Each node says what came in, what went out, where
the time went and what it cost, so "it was slow" resolves to a step:

![One run, node by node](docs/screenshots/run-detail.png)

Every page wears the same shell: a left rail that is navigation and nothing else,
and one bar across the top holding only the reader's own controls — search
(`/` or Ctrl-K), interface language, theme. Cost and duration live on `/runs`,
with the run they belong to. The rail collapses to icons with the chevron by the
brand.

The theme follows your system unless you pick one, and both the theme and the
language are a query parameter first and a cookie second (`?theme=light`) — so a
link is shareable and a screenshot is reproducible from its URL.

![The digest in the light theme](docs/screenshots/digest-light.png)

![Sources](docs/screenshots/sources.png)

## What it costs

Measured, not estimated:

| | Articles | Tokens (in / out) | Cost | Time |
|---|---|---|---|---|
| First run (a week's backlog) | 136 | 191,960 / 64,126 | **$0.115** | 85s |
| A three-hour delta | 21 | 30,600 / 8,200 | **$0.017** | 43s |
| A two-day delta | 20 | 32,195 / 12,363 | **$0.021** | 58s |

About **$0.00085 per article**, so a normal day of 100 stories is roughly
**$0.09** — **~$2.50 a month** against a $10 budget. Tavily stays on its free
tier behind a 30-credit daily cap, and in practice rarely gets there, because two
cheaper enrichment tiers run first.

That price is the reason every article gets summarised instead of a cheap
title-only filter deciding in advance what is worth reading.

## Design, in one line

A tool, not a landing page: no hero, no atmosphere, no scroll animation. Ranking
is carried by the typography itself — a headline's size and ink step come from
the importance score, and below a 3 a story collapses to its headline. Colour is
rationed and every job is named: the accent for *where am I standing* and for the
one control that spends money, `--alarm` for a failed run, and three colours on
the impact meter. The reasoning is in
[`docs/HOW-IT-WORKS.md`](docs/HOW-IT-WORKS.md#117-the-page-itself).

## Is it any good?

Output quality is a number a command regenerates, never a claim in a document.
`pytest` stays offline and free; anything that spends money is a sibling command
that says so and refuses above a cap.

```bash
uv run ainews eval record --run latest          # a run → a JSON fixture; no key, no network
uv run ainews eval judge --run latest           # 12 sampled summaries, grounding, ~$0.05
uv run ainews eval rank-stability --run latest  # 3 shuffled rank calls, Kendall τ, ~$0.01
uv run ainews eval report                       # every number, appended to docs/evals.md
uv run ainews eval report --run <id>            # one run; unchanged numbers fold to a line
```

The first measurements are in [`docs/evals.md`](docs/evals.md): 4.4% of summaries
over the word budget, two ungrounded figures, and a rank stability of **τ 0.47
and 0.50** on the two real runs — both under the 0.6 that triggers a change to
the ranker. Both were measured while the page ignored the ranker's order
(ADR 0025 fixed that); the next real run is the first honest reading.

![How the evaluation works](docs/eval-architecture.png)

The judge's failures are the part worth looking at, so they have a page rather
than a line in a log: the sentence it could not find in the article, with the
same *doğru · yanlış* under it that the stories carry. Answering one labels the
judge, which is the only way a one-reader tool learns whether its evaluator is
right.

![The reader's verdicts and the judge's failures](docs/screenshots/verdicts.png)

## Known limits

- **One language per bulletin.** `tr` or `en`, chosen at the press. The switch in
  the bar is the *interface* language and filters nothing; when the bulletin on
  screen was written in the other one, the bar says so.
- **Nothing runs while you are away.** A week unopened is a week of collected
  articles and no bulletins; the next press summarises what is still inside the
  seven-day horizon and nothing older.
- **One worker, forever.** A second uvicorn worker means a second scheduler, a
  second feed poll, and two writers on a database that has room for one.
- **No migration tool in v1.** Startup adds a missing nullable column and nothing
  else (ADR 0025); any other schema change after the archive is worth keeping
  means writing an Alembic baseline first.
- **Feeds rot.** Anthropic has no official feed, so the seed list uses a
  community mirror; Reddit rate-limits. A source that fails five times running
  disables itself and says so on `/sources`.
- **Cost is an estimate**, computed from token counts. Prompt caching makes the
  real bill lower.
- **No auth.** It binds inside the container and publishes to loopback only. Do
  not put it on a network you do not own.

## Development

```bash
uv sync
uv run pytest          # 394 tests, no API key, no network
uv run ruff check .
uv run pre-commit install
```

Tests use a fake model and `respx` for HTTP, so the whole suite runs offline in
about twenty seconds and the graph tests exercise a full fan-out, rank and
persist with nothing to pay for. `langgraph.json` is checked in for `langgraph
dev` if you want to step through the graph in Studio, and per-call tracing is
available behind a dev profile:

```bash
docker compose --profile dev up -d   # Phoenix on :6006, PHOENIX_ENABLED=true
```

Screenshots in this README are regenerated with:

```bash
uv sync --group docs && uv run playwright install chromium
uv run python scripts/screenshots.py
```

MIT licensed — see [`LICENSE`](LICENSE).
