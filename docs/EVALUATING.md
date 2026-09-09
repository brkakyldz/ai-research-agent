# Evaluating this digest

Written for whoever runs the application, not for whoever wrote it. It says what
each number means, when it is worth believing, and how to add one of your own.

`docs/evals.md` is the other half: it is the **record**, appended to by
`ainews eval report` and never edited. This file is the manual.

---

## The one-minute version

```bash
ainews eval report --no-write
```

No API key, no network, no cost. It prints every free number for the last thirty
days and the calibration for the judge, if there is one.

Everything else on this page is either how to read that output, or a command that
spends money and says how much before it does.

---

## Four layers, and only two of them cost anything

| Layer | Where | Costs | Answers |
|---|---|---|---|
| Deterministic checks | `src/ainews/quality/` | nothing | Did the writing keep its shape and carry anything concrete? |
| The reader's verdict | the story foot on every page | nothing | Was this story right, and about what? |
| The grounding judge | `ainews eval judge` | ~$0.05 a run | Does a summary state a fact its article does not support? |
| The rank probe | `ainews eval rank-stability` | ~$0.01 a run | Does the ranker's answer depend on the order it was shown? |

The first layer runs **at publish**, on its own, and the numbers land on the
bulletin. You see them on `/runs/<id>` without running anything. The second is a
control you press while reading. The last two are commands.

`src/ainews/evals/` holds the two that cost money. Nothing in the application
imports that package — delete it and one CLI subcommand stops working (ADR 0019
§2). `src/ainews/quality/` is the free half, and the pipeline calls it.

---

## What each number means

### The content floor — the one that catches a bad prompt

Everything else in the deterministic layer measures *shape*: word budgets,
sentence counts, tag spread, score distribution, note paragraph lengths. A
regression to three generic, numberless sentences at importance 3 clears every
one of them. It also clears the numeral check (there is no figure to flag) and
the grounding judge (there is no claim to disagree with).

So the summariser is asked for a `key_fact` — the one figure, name, version or
date that makes the story news — and `content_floor` asks two questions:

- **Named a key fact** — the share of stories it could name one for. A day of
  opinion pieces legitimately scores low; a day of releases scoring low is the
  prompt going vague.
- **And kept it in the writing** — of those, the share whose summary or headline
  actually contains it. This is the number to watch. A model that records the
  fact and then writes around it still *knows* what the story was and has stopped
  telling you.
- **A body figure survives into the summary** — of the stories whose article
  carried a figure, the share whose summary carries one of the article's own
  figures. It is the mirror of the ungrounded-numeral check: that one catches an
  invented figure, this one catches a summary that dropped every real one.

Measured on the three runs in the archive as of 2026-09-09: 0.73, 0.48, 0.63.

### Ungrounded numerals

A figure in the summary that is not in the body the model was shown. Zero is the
expected value; anything above it is worth opening. It compares scaled values, so
"13 milyar" against a body that says "12.9 billion" is flagged — a rounded figure
is a figure the text did not give, and the prompt says so.

The body it checks against is the text the model was **shown**, cut at 5,000
characters, not the whole article. A figure past that cut-off is one the model
could not have read.

### Tag vocabulary and singletons

`singleton_share` is the fraction of distinct tags used exactly once. Two thirds
on both early runs, which meant the topic filter row filtered nothing. There is a
preferred list (`prompts.TAG_VOCABULARY`) formatted into the prompt and read by
the check, so the two cannot disagree about what "in the vocabulary" means. The
model may still invent one — a vocabulary that cannot grow is wrong by next
quarter — so the share outside the list is reported, not asserted.

### Importance distribution and the tiers

Two different scales, deliberately (ADR 0030):

- **importance**, 1–5, is the summariser's absolute judgement with one article in
  view. The prompt says most items are 2 or 3, and a distribution that drifts up
  is grade inflation, not a better week.
- **tier** — `lead | major | notable | brief` — is the editor's placement of a
  story *against the others that day*. A bulletin that is fifteen `notable` is a
  ranker filling a page.

`contradictions` counts pairs the editor put in the opposite order to the free
importance-then-weight ordering. Zero means the rank call bought nothing.

### Agreement

Every bulletin is three rank calls over the same candidates in three shuffled
orders, aggregated by Borda count. `agreement` is the **lower** of

- mean Kendall τ across the three orders, and
- their mean Jaccard, scored against chance.

The lower of the two, because they fail independently: three readings can order
five stories identically while disagreeing about which five belong, and they can
pick the same fifteen in three unrelated orders.

The chance correction matters more than it looks. Two *random* picks of fifteen
from a pool of twenty-seven overlap 0.385 of the time — two thirds of a 0.6 gate,
bought with nothing. Corrected, indifference scores 0 and agreement scores 1.

**`agreement` is null** when fewer than two readings came back. That is the
silent fallback: the bulletin was ordered by importance and weight, with no
editor behind it. It is the one value on this page you should treat as an alarm.

### The judge

`ainews eval judge --bulletin latest` samples twelve summaries and asks a
stronger model one binary question: does this summary state anything as fact that
the article does not support? The *why-it-matters* line is the editor's inference
and is failed only for an invented fact, never for drawing a conclusion.

A judged summary whose article body cannot be attributed is reported as such
rather than as a pass: before the article and the web context were separated
(ADR 0029), a body might be the article with a week's search results appended,
and a judge reading the second and passing it has confirmed nothing.

### Calibration — and `MIN_LABELS_PER_CLASS`

The judge is a model, so it needs checking against a person. That is what the
*Doğru · Yanlış* under every story is for.

`ainews eval judge --labelled` judges every summary that carries a verdict and
prints a 2×2:

- **TPR** — of the summaries you called wrong, the share the judge also failed.
- **TNR** — of the ones you called right, the share it passed.
- **precision** — of the summaries the judge failed, the share you agreed with.

Never one accuracy figure. A judge that passes everything is 90% accurate on a
mostly-right digest and catches nothing.

**`MIN_LABELS_PER_CLASS` is 30**, and it is protecting you from a number that
moves when you label one more story. Under thirty labels per class the report
says "not enough labels to trust" and means it: with four labels, one
disagreement is twenty-five percentage points of TPR. Precision is the number
that becomes useful first — it needs one label per judge failure, not thirty, and
`/runs/verdicts` puts exactly those in front of you.

Only a **`wrong_fact`** verdict is scored against the judge. The other three
reasons are about parts of the system it never looks at, and folding them in used
to make the judge's TPR fall for being right:

| Your reason | What it measures | Where it shows up |
|---|---|---|
| A fact is wrong | the summariser's grounding | judge calibration |
| Not AI news | the summariser's `relevant` call | `Verdict.reason` counts |
| Same story again | the dedupe step | `Verdict.reason` counts |
| In the wrong place | the ranker's tiers | `Verdict.reason` counts |

The last three have no other measurement in the system at all. `In the wrong
place` in particular is the only honest signal anyone has about the ranker.

### `prompt_version`

Every measurement row records a hash of the prompt file that produced it. The
judge prompt failed 8 of 12 on 2026-09-06, was revised the same afternoon, and
the revision was scored on the same 12 — so the 83–92% on record is a
training-set score and nothing on the row said so. The report now says out loud
when a calibration averages two prompts, and when the prompt on disk is not the
one the rows were measured under.

**Re-judge the labelled set after you change the judge prompt.** Old rows are not
comparable to new ones.

### Where the numbers came from

Each bulletin's section says either *"Checks as the press ran them"* or *"Checks
recomputed now"*. The second means the bulletin predates the column that stores
them, so what you are reading is today's check code over an older day — a
measurement of neither cleanly.

---

## Changing a prompt without paying for a run

This is the part worth learning. Do **not** edit `summarize.md`, press the
button, read fifteen summaries and form an impression — the day's news is
different from yesterday's, so the impression is about the news as much as about
the prompt.

```bash
# Once: freeze forty article bodies out of your archive. No key, no cost.
ainews eval corpus --size 40

# Then, for each candidate:
cp src/ainews/pipeline/prompts/tr/summarize.md /tmp/candidate.md
$EDITOR /tmp/candidate.md
ainews eval compare --prompt-b /tmp/candidate.md
```

Both prompts see the same bodies, the same model and the same seed, so the only
difference is the prompt. It prints the two sets of checks side by side and
**names no winner** — the measures trade against each other, and a prompt that
names more figures also breaks the word budget more. About $0.02 a comparison,
and it refuses above `--max-cost` before making the first call.

The corpus file lives under `data/`, which is gitignored: it holds whole article
bodies, and those are other people's text.

`--prompt-a` defaults to the prompt that ships, because the question is nearly
always "is this candidate better than what we have".

---

## Adding a check of your own

A check is a pure function: story dicts in, plain numbers out. No I/O, no
settings, no model. That is what lets the same code run over a recorded fixture
inside `pytest` and over the live database at the end of a press.

1. **Write it** in `src/ainews/quality/checks.py`. Take `list[Story]`, return a
   dict or a list. A `Story` is what `quality/stories.py` builds — the summary,
   the headline, the tags, the scores, the editor's placement, and the body's
   *numerals and capitalised tokens* (never the body itself: the fixtures are in
   a public repository).

2. **Add it to `published_checks`** in `src/ainews/quality/stories.py` if the
   press should compute it every time, and to `render_bulletin` in
   `src/ainews/evals/report.py` so the report prints it with the function that
   produced it beside it.

3. **Bound it in a test** in `tests/test_quality_checks.py`, against the recorded
   fixtures. Set the bound from a real number you have measured — not from a
   guess. If an existing fixture predates the thing you are checking, mark it in
   `KNOWN_GAPS` with the reason, and update the count in `tests/test_known_gaps.py`.
   A bare `xfail` is a test switched off; a reason is the difference between a
   measurement and a silence.

4. **Bump `SCHEMA`** in `src/ainews/evals/record.py` if a story gained a field,
   and re-record: `ainews eval record --bulletin latest`.

Report a distribution rather than asserting a threshold unless you can defend the
threshold. A bar nobody can defend is a bar that gets raised until it passes.

---

## When to act on a number

These are the triggers `docs/PLAN-EVALS.md` set, restated as the things worth
doing something about:

- **`agreement` is null.** The rank call failed and the bulletin has no editor
  behind it. Press again.
- **`agreement` under 0.6, repeatedly.** The ranker's answer depends on the order
  it was shown. Look at what the candidate table gives it before blaming the
  prompt.
- **"And kept it in the writing" falling.** The prompt has gone vague. Compare a
  candidate against the frozen corpus before shipping one.
- **Ungrounded numerals above zero.** Open the story. One real fabrication in 91
  summaries was what started this whole layer.
- **TPR under 0.8 after a hundred labels and a prompt rewrite.** The judge model
  is the problem, not its instructions.
- **`not_news` verdicts accumulating on one source.** The relevance call is doing
  its job and the source list is not. `/sources` is where that gets fixed.

---

## What this does not measure

Turkish writing quality is judged by a person, here and nowhere else — never by a
model. There is no automated fluency score and there will not be one.

Nothing measures whether the digest picked the *right* stories in an absolute
sense. There is no ground truth for that and inventing one would be worse than
admitting it. What there is: how stably the ranker answers, how far it read
against the free ordering, and whether you told it it was wrong.
