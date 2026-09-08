"""Read queries for the dashboard.

Kept apart from the routes because all four pages want the same three shapes -
a run, its stories, the tag counts - and a query written twice is a query that
disagrees with itself later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Language, Settings, get_settings
from ainews.db import Article, EvalResult, Run, RunStep, Source, Summary, Verdict, bulletin_runs
from ainews.web.format import relative_age, to_local


@dataclass(slots=True)
class Story:
    id: int
    source: str
    url: str
    title_local: str
    summary: str
    why_it_matters: str
    importance: int
    tags: list[str]
    age: str
    # The reader's verdict on this summary, if one was given: "ok" | "wrong".
    verdict: str | None = None
    verdict_note: str | None = None


def _finished_digests() -> Any:
    """A bulletin that exists: it ran, it did not fail, and it wrote something."""
    return bulletin_runs().where(Run.status.in_(("ok", "partial"))).where(Run.n_summarized > 0)


def archive_runs() -> Any:
    """What `/archive` lists, and what the rail badge counts.

    One selector for both, because they were two and disagreed: the badge
    counted `kind == "digest"` while every press wrote `manual`, so it read 1
    over a list of 3 (ADR 0026). A count is a promise about what is behind the
    link, and it can only keep that promise by being the same query.
    """
    return bulletin_runs().where(Run.n_summarized > 0)


async def latest_digest_run(session: AsyncSession, settings: Settings | None = None) -> Run | None:
    """The bulletin the front page shows, whatever language it was written in.

    It used to be "the most recent digest in the page's language", which made
    the shell's TR/EN switch a content filter: a reader on a Turkish page who
    pressed `English` got "no digest yet", because the bulletin sitting in the
    database was Turkish. The switch translates the buttons
    and nothing else now, and the language a bulletin was written in is chosen
    where it is paid for - the press on `/runs` (ADR 0017). The bar names the
    bulletin's language when it differs from the page's, so a Turkish shell
    around English stories is labelled rather than silently served.

    And it is not simply the newest. Every press is a delta - only what has not
    been summarised is a candidate - so a second press ten minutes after a
    fifteen-story bulletin summarises the two stories that arrived in between,
    ranks them, and writes a three-paragraph note about a day of two stories.
    Until 2026-09-08 that two-story run became the front page and the morning's
    bulletin dropped into the archive. A run that summarised fewer than half of
    `digest_top_n` is a supplement: the page shows the last full bulletin from
    the same bulletin-day instead - within `digest_suggest_after_hours`, the
    interval `/runs` already counts a day by - and the supplement stays in the
    archive. When the last full one is older than that, the small run is the
    day's bulletin and is shown, because a stale full page would be worse.
    """
    settings = settings or get_settings()
    latest = (await session.execute(_finished_digests().limit(1))).scalar_one_or_none()
    if latest is None:
        return None
    floor = max(1, settings.digest_top_n // 2)
    if latest.n_summarized >= floor:
        return latest
    since = latest.started_at - timedelta(hours=settings.digest_suggest_after_hours)
    fuller = (
        await session.execute(
            _finished_digests()
            .where(Run.n_summarized >= floor)
            .where(Run.started_at >= since)
            .where(Run.started_at < latest.started_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    return fuller or latest


def _to_story(
    summary: Summary,
    article: Article,
    source_name: str,
    age: str,
    verdict: Verdict | None = None,
) -> Story:
    try:
        tags = json.loads(summary.tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    return Story(
        id=summary.id,
        source=source_name,
        url=article.url,
        title_local=summary.title_local,
        summary=summary.summary,
        why_it_matters=summary.why_it_matters,
        # The editor's score where the ranker gave one, the summariser's where
        # it did not (ADR 0025). The headline size is drawn from this.
        importance=summary.shown_importance,
        tags=tags,
        age=age,
        verdict=verdict.verdict if verdict else None,
        verdict_note=verdict.note if verdict else None,
    )


async def stories_for_run(
    session: AsyncSession,
    run: Run,
    *,
    language: Language,
    ranked_only: bool = True,
    tag: str | None = None,
) -> list[Story]:
    """The run's stories, heaviest first.

    Importance leads the sort since 2026-09-08, and `rank` only breaks its ties.
    It was the other way round, and the docstring claimed exactly what this
    paragraph claims - that the reader scanning downward sees the ink fade
    monotonically - while the query made it false: `rank` is the ranker's own
    order over the run, `importance` is the model's 1-5 score on one story, and
    the two disagree constantly. A real day came out p4, p3, p3, p2, p3, which
    on a page whose only ranking indicator is the size of the headline (ADR
    0014) reads as no order at all: the stories read as randomly arranged.

    Whose importance is the second half of that fix (ADR 0025, the same day).
    Sorting by the summariser's score meant the one node that sees the whole
    day - and is told to correct the isolated scores - had its corrections
    thrown away, because the schema gave it nowhere to put them. It has now:
    `editor_importance`, set on every ranked story, and this sort reads it
    where it exists. The summariser's score still orders the stories below the
    fold and every run from before the column.

    `rank` still decides *which* stories are here - `ranked_only` is the top-N
    filter, and that is the job it was written for. What it no longer does is
    decide the order they are read in, which is what the typography is already
    saying.

    `language` is the page's, not the run's, and only the age string uses it:
    "3 saat" is a button, not reporting - it is written by this app rather than
    by the model, so it follows the shell even when the stories under it were
    written in the other language.
    """
    query = (
        select(Summary, Article, Source.name, Verdict)
        .join(Article, Article.id == Summary.article_id)
        .join(Source, Source.id == Article.source_id)
        .outerjoin(Verdict, Verdict.summary_id == Summary.id)
        .where(Summary.run_id == run.id)
    )
    if ranked_only:
        query = query.where(Summary.rank.isnot(None))
    if tag:
        # A cheap narrowing, not the decision. `tags_json` is a JSON array in a
        # text column, so `"openai"` as a substring can only appear as a whole
        # element - but it could also be part of a longer word inside a *value*
        # this column does not hold, so the exact membership test below still
        # runs. What this saves is loading ninety rows to keep ten; what it
        # refuses to do is make SQLite parse the JSON, which fails the whole
        # query on one malformed row rather than skipping it.
        query = query.where(Summary.tags_json.contains(f'"{tag}"', autoescape=True))
    query = query.order_by(
        func.coalesce(Summary.editor_importance, Summary.importance).desc(),
        Summary.rank.asc().nullslast(),
        Summary.id.asc(),
    )

    stories = []
    for summary, article, source_name, verdict in (await session.execute(query)).all():
        story = _to_story(
            summary, article, source_name, relative_age(article.published_at, language), verdict
        )
        if tag and tag not in story.tags:
            continue
        stories.append(story)
    return stories


async def story_for_summary(
    session: AsyncSession, summary_id: int, language: Language
) -> Story | None:
    """One story by its summary id - what `POST /verdict` re-renders."""
    row = (
        await session.execute(
            select(Summary, Article, Source.name, Verdict)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .outerjoin(Verdict, Verdict.summary_id == Summary.id)
            .where(Summary.id == summary_id)
        )
    ).first()
    if row is None:
        return None
    summary, article, source_name, verdict = row
    return _to_story(
        summary, article, source_name, relative_age(article.published_at, language), verdict
    )


async def count_unranked(session: AsyncSession, run: Run) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Summary)
            .where(Summary.run_id == run.id)
            .where(Summary.rank.is_(None))
        )
    ).scalar_one()


@dataclass(slots=True)
class TagCounts:
    """The run's tags counted two ways, from one pass over its rows.

    Both numbers are on screen at once whenever `?all=1` is on: the filter row
    counts the list the reader can reach, and the brief's footnote counts the
    *digest* - the same stories the rail badge counts - so opening the full list
    must not leave "11 haber" beside a topic count taken over ninety-one. That
    was two calls to this query until 2026-09-08, the second one scanning every
    `tags_json` in the run a second time to produce a single integer.
    """

    ranked: list[tuple[str, int]]
    every: list[tuple[str, int]]

    def shown(self, ranked_only: bool) -> list[tuple[str, int]]:
        return self.ranked if ranked_only else self.every


async def tag_counts(session: AsyncSession, run: Run) -> TagCounts:
    """Tags across the run, most common first, in both scopes.

    Counted in Python rather than in SQL because the tags live in a JSON column;
    at a hundred rows a day that is not worth a second table, and a malformed
    value is skipped here rather than failing a `json_each` mid-query.

    The ranked scope has to track the list the page is showing, and until
    2026-09-06 it did not exist: the counts were taken over every summary the
    run produced while the filter they label narrows the *ranked* fifteen. So
    the row said `agents 27` on a page headed "15 haber", and pressing it
    returned four stories. A count is a promise about what is behind the link.
    """
    rows = (
        await session.execute(
            select(Summary.tags_json, Summary.rank).where(Summary.run_id == run.id)
        )
    ).all()
    ranked: dict[str, int] = {}
    every: dict[str, int] = {}
    for raw, rank in rows:
        try:
            tags = json.loads(raw or "[]")
        except json.JSONDecodeError:
            continue
        for tag in tags:
            every[tag] = every.get(tag, 0) + 1
            if rank is not None:
                ranked[tag] = ranked.get(tag, 0) + 1

    def ordered(counts: dict[str, int]) -> list[tuple[str, int]]:
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    return TagCounts(ranked=ordered(ranked), every=ordered(every))


@dataclass(slots=True)
class RunHistory:
    """The three timestamps the run advice is computed from.

    `last_success_at` is deliberately not `last_finished.started_at`: a digest
    that ended in an error produced no bulletin, so it must not push the next
    suggestion a day into the future. A failed run is something you are told
    about and then asked to repeat, not something that counts as done.
    """

    last_finished: Run | None
    last_success_at: datetime | None
    last_collect_at: datetime | None


async def _latest(session: AsyncSession, *conditions: Any) -> Run | None:
    """The newest bulletin run matching the conditions, or none."""
    statement = bulletin_runs().limit(1)
    for condition in conditions:
        statement = statement.where(condition)
    return (await session.execute(statement)).scalar_one_or_none()


async def run_history(session: AsyncSession) -> RunHistory:
    """Three one-row lookups down the `started_at` index, on every page.

    Three queries rather than one pass over the recent runs, because "the last
    successful digest" can be arbitrarily far back - a week of failures would
    fall outside any window a single query picked, and the countdown would
    silently restart.
    """
    last_finished = await _latest(session, Run.status != "running")
    last_success = await _latest(session, Run.status.in_(("ok", "partial")))
    # The feeds are polled by every run, not only by the `collect` kind: a
    # digest's first node is the same poll. Until 2026-09-08 this read the last
    # `collect` run, so `/runs` said the sources were last read two days ago
    # while the digest that had just finished had read them a minute earlier.
    # The sources' own clocks are the record of when they were last touched.
    last_polled = (
        await session.execute(
            select(Source)
            .where(Source.enabled.is_(True), Source.last_fetched_at.isnot(None))
            .order_by(Source.last_fetched_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return RunHistory(
        last_finished=last_finished,
        last_success_at=last_success.started_at if last_success else None,
        last_collect_at=last_polled.last_fetched_at if last_polled else None,
    )


async def recent_runs(session: AsyncSession, limit: int = 12) -> list[Run]:
    return list(
        (await session.execute(select(Run).order_by(Run.started_at.desc()).limit(limit))).scalars()
    )


async def digest_runs(session: AsyncSession, limit: int = 30) -> list[Run]:
    """Every bulletin there is, newest first - the archive does not filter by
    language for the same reason the digest does not (ADR 0017); each row in the
    picker carries its own language, so a mixed archive reads as a list of
    bulletins rather than as a page that lost half its history."""
    return list((await session.execute(archive_runs().limit(limit))).scalars())


async def count_archive(session: AsyncSession) -> int:
    """How many bulletins the archive holds - the rail badge's number, counted
    off the very query that draws the list."""
    return (
        await session.execute(select(func.count()).select_from(archive_runs().subquery()))
    ).scalar_one()


def _fts_query(raw: str) -> str:
    """Turn a typed phrase into an FTS5 MATCH expression.

    Every token is quoted and given a prefix `*`, which does two jobs: it stops
    FTS5 from interpreting `AND`, `-` or `"` as operators and erroring on a
    perfectly ordinary search, and it makes Turkish suffixes stop mattering -
    "model" then finds "modeli" and "modelleri", which is the whole difference
    between a search box that works in Turkish and one that does not.
    """
    tokens = [t for t in raw.replace('"', " ").split() if t]
    return " ".join(f'"{token}"*' for token in tokens)


async def search_stories(
    session: AsyncSession, raw_query: str, language: Language, limit: int = 60
) -> list[Story]:
    """Full-text hits across every bulletin, in every language.

    `language` reaches only the age string. The index is not filtered by it: a
    reader looking for a company name wants the article, and refusing to show it
    because the digest that covered it was written in the other language is the
    same content filter the shell's switch stopped being (ADR 0017).
    """
    expression = _fts_query(raw_query)
    if not expression:
        return []

    hits = (
        await session.execute(
            text(
                "SELECT rowid FROM summaries_fts WHERE summaries_fts MATCH :q "
                "ORDER BY rank LIMIT :limit"
            ),
            {"q": expression, "limit": limit},
        )
    ).scalars()
    ids = list(hits)
    if not ids:
        return []

    rows = (
        await session.execute(
            select(Summary, Article, Source.name, Verdict)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .outerjoin(Verdict, Verdict.summary_id == Summary.id)
            .where(Summary.id.in_(ids))
            .order_by(Summary.created_at.desc())
        )
    ).all()

    return [
        _to_story(summary, article, name, relative_age(article.published_at, language), verdict)
        for summary, article, name, verdict in rows
    ]


@dataclass(slots=True)
class Day:
    """One local day in the activity chart."""

    label: str
    n: int
    today: bool


@dataclass(slots=True)
class Activity:
    """What the machine has been doing, for the column beside the digest.

    Every number here is read off `runs` and `sources`, which the pipeline has
    been writing since M1 - nothing is modelled, projected or rounded up from a
    sample. That is the whole condition on this panel existing: a chart drawn
    against invented figures is worse than no chart, and until someone looked
    there was an assumption that the series was not there.
    """

    last: Run | None
    days: list[Day]
    cost_today: float
    cost_week: float
    cost_month: float
    n_sources: int
    n_failing: int


async def recent_activity(session: AsyncSession, n_days: int = 7) -> Activity:
    """The last 30 days of runs, bucketed by the reader's own calendar day.

    One query and a loop rather than a GROUP BY: the bucket is a *local* date
    and SQLite would have to be told the offset, which is a rule that then
    disagrees with `to_local` the first time the timezone setting changes.
    A month of runs is at most a few hundred rows.
    """
    since = datetime.now(UTC) - timedelta(days=30)
    runs = list(
        (
            await session.execute(
                select(Run).where(Run.started_at >= since).order_by(Run.started_at.desc())
            )
        ).scalars()
    )

    today = to_local(datetime.now(UTC)).date()  # type: ignore[union-attr]
    wanted = [today - timedelta(days=offset) for offset in range(n_days - 1, -1, -1)]
    stories: dict[object, int] = dict.fromkeys(wanted, 0)
    cost: dict[object, float] = {}

    for run in runs:
        local = to_local(run.started_at)
        if local is None:
            continue
        day = local.date()
        cost[day] = cost.get(day, 0.0) + (run.est_cost_usd or 0.0)
        if day in stories:
            stories[day] += run.n_summarized

    week = [today - timedelta(days=offset) for offset in range(7)]
    return Activity(
        last=runs[0] if runs else None,
        days=[Day(label=day.strftime("%d"), n=stories[day], today=day == today) for day in wanted],
        cost_today=cost.get(today, 0.0),
        cost_week=sum(cost.get(day, 0.0) for day in week),
        cost_month=sum(cost.values()),
        n_sources=(
            await session.execute(
                select(func.count()).select_from(Source).where(Source.enabled.is_(True))
            )
        ).scalar_one(),
        n_failing=(
            await session.execute(
                select(func.count()).select_from(Source).where(Source.consecutive_failures > 0)
            )
        ).scalar_one(),
    )


@dataclass(slots=True)
class VerdictProgress:
    """How many summaries carry the reader's own call on them.

    The only eval number the interface can move. Everything else in
    `PLAN-EVALS.md` is produced by spending money at a terminal; a label is
    produced by a person reading a story and pressing a word, and nothing in the
    app had ever mentioned that the count exists.

    It matters because the calibration half of the evaluation plan is parked
    behind it. `judge.calibrate` computes TPR against `wrong` labels and TNR
    against `ok` ones; `judge_labelled` refuses to trust a class under thirty,
    and E5 gates the judge-prompt rewrite on 100. With three labels and no
    `wrong` among them, TPR has no denominator at all.

    Read straight off `verdicts` and `summaries` - two tables `db/models.py`
    owns and the web layer already reads. Nothing from `evals/` is imported,
    which is ADR 0019 §2: a recorded measurement is not a measurement being
    made.
    """

    labelled: int
    wrong: int
    total: int

    @property
    def ok(self) -> int:
        return self.labelled - self.wrong


async def verdict_progress(session: AsyncSession) -> VerdictProgress:
    """One row of counts. Never divides, so an empty database is not a case."""
    labelled, wrong = (
        await session.execute(
            select(
                func.count(Verdict.id),
                func.count(case((Verdict.verdict == "wrong", 1))),
            )
        )
    ).one()
    # Every summary that has ever been written, not just today's: a reader
    # labelling an archived story is doing exactly the work the judge needs, and
    # a denominator that shrank overnight would be a number that goes backwards.
    total = (await session.execute(select(func.count(Summary.id)))).scalar_one()
    return VerdictProgress(labelled=labelled, wrong=wrong, total=total)


@dataclass(slots=True)
class LabelledStory:
    """One row of `/runs/verdicts`: a verdict, and enough of the story to place it.

    Not a `Story`. That shape is the reading - a summary, a why-it-matters, a
    tag list - and this table draws none of it: the column that matters here is
    the reader's own sentence, and the story is the address it was written at.
    """

    summary_id: int
    run_id: str
    decided_at: datetime
    source: str
    title_local: str
    verdict: str
    note: str | None


async def labelled_stories(session: AsyncSession, limit: int = 200) -> list[LabelledStory]:
    """Every verdict the reader has given, newest press first.

    Both words, not only `wrong`. The count this page hangs off says "karar
    verilen 4/118", so a list that answered with two rows would be a second
    number disagreeing with the first one - and the calibration the labels are
    for needs both classes, TPR against `wrong` and TNR against `ok`
    (`evals/judge.py`). The notes are what E5 rewrites a prompt from and they
    only ever sit on a `wrong`, which is why that column is mostly a dash and
    that is not a fault in it.

    Sorted by when the verdict was given rather than by what it says. It is the
    first column, it is a log, and a hidden "wrong first" rule would make the
    times read as unsorted. `limit` is a ceiling on a page with no paging: 200
    labels is twice what E5 asks for and this list has four rows.
    """
    rows = (
        await session.execute(
            select(Verdict, Summary, Source.name)
            .join(Summary, Summary.id == Verdict.summary_id)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .order_by(Verdict.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        LabelledStory(
            summary_id=summary.id,
            run_id=summary.run_id,
            decided_at=verdict.created_at,
            source=source_name,
            title_local=summary.title_local,
            verdict=verdict.verdict,
            note=verdict.note,
        )
        for verdict, summary, source_name in rows
    ]


@dataclass(slots=True)
class Finding:
    """One summary the grounding judge failed, and whether the reader has answered.

    The judge's most information-dense output - the sentence it could not
    support - was written to `eval_results.detail`, printed once by the command
    that produced it, and never put in front of the person whose verdict it is
    calibrated against. This is that row, shaped for `/runs/verdicts`.
    """

    summary_id: int
    run_id: str
    judged_at: datetime
    model: str
    source: str
    title_local: str
    claim: str
    verdict: str | None


async def judge_findings(session: AsyncSession, limit: int = 200) -> list[Finding]:
    """Every summary whose latest judgement is a FAIL, newest judgement first.

    Read off `eval_results` and `verdicts` - tables `db/models.py` owns - and
    not through `evals/`, which this layer does not import (ADR 0019 §2): a
    recorded measurement is not a measurement being made. The latest row per
    summary, because a summary can be judged twice on two tiers and only the
    last word stands; a summary whose latest judgement passed is not listed
    even if an earlier one failed.
    """
    rows = (
        await session.execute(
            select(EvalResult, Summary, Source.name, Verdict)
            .join(Summary, Summary.id == EvalResult.summary_id)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .outerjoin(Verdict, Verdict.summary_id == Summary.id)
            .where(EvalResult.kind == "grounding")
            .order_by(EvalResult.created_at.desc(), EvalResult.id.desc())
        )
    ).all()
    seen: set[int] = set()
    findings: list[Finding] = []
    for result, summary, source_name, verdict in rows:
        if summary.id in seen:
            continue
        seen.add(summary.id)
        if result.passed is not False or not result.detail:
            continue
        findings.append(
            Finding(
                summary_id=summary.id,
                run_id=summary.run_id,
                judged_at=result.created_at,
                model=result.model,
                source=source_name,
                title_local=summary.title_local,
                claim=result.detail,
                verdict=verdict.verdict if verdict else None,
            )
        )
        if len(findings) == limit:
            break
    return findings


async def run_by_id(session: AsyncSession, run_id: str) -> Run | None:
    return await session.get(Run, run_id)


async def steps_for_run(session: AsyncSession, run_id: str) -> list[RunStep]:
    """One run's nodes, in the order they ran.

    Ordered by the clock rather than by a stored sequence, which is what lets
    the fan-out's row - written after the fact, from between its neighbours'
    timestamps (ADR 0022) - land in its place without anything renumbering.
    """
    return list(
        (
            await session.execute(
                select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.started_at)
            )
        ).scalars()
    )


@dataclass(slots=True)
class SourceRow:
    source: Source
    n_articles: int


async def source_rows(session: AsyncSession) -> list[SourceRow]:
    counts = dict(
        (
            await session.execute(
                select(Article.source_id, func.count()).group_by(Article.source_id)
            )
        ).all()
    )
    sources = (
        await session.execute(select(Source).order_by(Source.enabled.desc(), Source.name))
    ).scalars()
    return [SourceRow(source=s, n_articles=counts.get(s.id, 0)) for s in sources]


def digest_top_n() -> int:
    return get_settings().digest_top_n


def as_dicts(stories: list[Story]) -> list[dict[str, Any]]:
    return [s.__dict__ for s in stories]
