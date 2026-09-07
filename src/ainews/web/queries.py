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

from ainews.config import Language, get_settings
from ainews.db import Article, Run, RunStep, Source, Summary, Verdict


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


async def latest_digest_run(session: AsyncSession) -> Run | None:
    """The most recent finished digest, whatever language it was written in.

    It used to be "the most recent digest in the page's language", which made
    the shell's TR/EN switch a content filter: a reader on a Turkish page who
    pressed `English` got "no digest yet", because the bulletin sitting in the
    database was Turkish (Berke, 2026-09-06). The switch translates the buttons
    and nothing else now, and the language a bulletin was written in is chosen
    where it is paid for - the press on `/runs` (ADR 0017). The bar names the
    bulletin's language when it differs from the page's, so a Turkish shell
    around English stories is labelled rather than silently served.
    """
    return (
        await session.execute(
            select(Run)
            .where(Run.kind != "collect")
            .where(Run.status.in_(("ok", "partial")))
            .where(Run.n_summarized > 0)
            .order_by(Run.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


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
        importance=summary.importance,
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
    """The run's stories, in reading order.

    Ranked items come first in the order the ranker chose; the rest follow by
    importance. That is the same ordering the page's typography expresses, so a
    reader scanning downward sees the ink fade monotonically.

    `language` is the page's, not the run's, and only the age string uses it:
    "3 saat" is a button, not reporting - it is written by this app rather than
    by the model, so it follows the shell even when the stories under it were
    written in the other language.
    """
    from ainews.web.views import relative_age

    query = (
        select(Summary, Article, Source.name, Verdict)
        .join(Article, Article.id == Summary.article_id)
        .join(Source, Source.id == Article.source_id)
        .outerjoin(Verdict, Verdict.summary_id == Summary.id)
        .where(Summary.run_id == run.id)
    )
    if ranked_only:
        query = query.where(Summary.rank.isnot(None))
    query = query.order_by(
        Summary.rank.asc().nullslast(),
        Summary.importance.desc(),
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
    from ainews.web.views import relative_age

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


async def tag_counts(
    session: AsyncSession,
    run: Run,
    limit: int = 12,
    *,
    ranked_only: bool = True,
) -> list[tuple[str, int]]:
    """Tags across the run, most common first.

    Counted in Python rather than in SQL because the tags live in a JSON column;
    at a hundred rows a day that is not worth a second table.

    `ranked_only` has to track the list the page is showing, and until
    2026-09-06 it did not exist: the counts were taken over every summary the
    run produced while the filter they label narrows the *ranked* fifteen. So
    the row said `agents 27` on a page headed "15 haber", and pressing it
    returned four stories. A count is a promise about what is behind the link.
    """
    query = select(Summary.tags_json).where(Summary.run_id == run.id)
    if ranked_only:
        query = query.where(Summary.rank.isnot(None))
    rows = (await session.execute(query)).scalars()
    counts: dict[str, int] = {}
    for raw in rows:
        try:
            for tag in json.loads(raw or "[]"):
                counts[tag] = counts.get(tag, 0) + 1
        except json.JSONDecodeError:
            continue
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


@dataclass(slots=True)
class Topic:
    """One topic in the side column's themes list.

    `share` is the percentage of the run's stories carrying the tag, and it is
    what the bar draws - a count alone tells the reader nothing about whether
    six is most of the day or a corner of it.

    `rising` is the only derived claim on the page, so it is deliberately dull:
    the tag has to appear at least twice today AND at least half again as often
    as its own average across the digests of the previous week. With no prior
    run to compare against, nothing rises - an empty week must not make every
    topic look like a trend.
    """

    name: str
    n: int
    share: int
    rising: bool


async def topic_pulse(
    session: AsyncSession,
    run: Run,
    *,
    limit: int = 6,
    window_days: int = 7,
    ranked_only: bool = True,
) -> list[Topic]:
    """The run's topics, with a baseline from the week behind it.

    Counted in Python for the same reason `tag_counts` is: the tags live in a
    JSON column, and a week of digests is a few hundred rows.

    `ranked_only` reaches the history query as well as today's, because a
    baseline drawn from every summary of last week against a count drawn from
    the ranked fifteen of today would make everything look like it is falling.
    Like against like, or the comparison is not one.
    """
    today = dict(await tag_counts(session, run, limit=200, ranked_only=ranked_only))
    if not today:
        return []

    counted = select(func.count()).select_from(Summary).where(Summary.run_id == run.id)
    if ranked_only:
        counted = counted.where(Summary.rank.isnot(None))
    n_stories = (await session.execute(counted)).scalar_one() or 1

    since = run.started_at - timedelta(days=window_days)
    history_query = (
        select(Summary.run_id, Summary.tags_json)
        .join(Run, Run.id == Summary.run_id)
        .where(Run.language == run.language)
        .where(Run.kind != "collect")
        .where(Run.id != run.id)
        .where(Run.started_at >= since)
        .where(Run.started_at < run.started_at)
    )
    if ranked_only:
        history_query = history_query.where(Summary.rank.isnot(None))
    prior = (await session.execute(history_query)).all()

    runs_seen: set[str] = set()
    history: dict[str, int] = {}
    for run_id, raw in prior:
        runs_seen.add(run_id)
        try:
            for tag in json.loads(raw or "[]"):
                history[tag] = history.get(tag, 0) + 1
        except json.JSONDecodeError:
            continue
    n_prior = len(runs_seen)

    topics = []
    for name, n in sorted(today.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]:
        baseline = history.get(name, 0) / n_prior if n_prior else None
        topics.append(
            Topic(
                name=name,
                n=n,
                share=round(100 * n / n_stories),
                rising=bool(baseline is not None and n >= 2 and n > 1.5 * baseline),
            )
        )
    return topics


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
    statement = select(Run).order_by(Run.started_at.desc()).limit(1)
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
    last_finished = await _latest(session, Run.kind != "collect", Run.status != "running")
    last_success = await _latest(session, Run.kind != "collect", Run.status.in_(("ok", "partial")))
    last_collect = await _latest(session, Run.kind == "collect", Run.status.in_(("ok", "partial")))
    return RunHistory(
        last_finished=last_finished,
        last_success_at=last_success.started_at if last_success else None,
        last_collect_at=last_collect.started_at if last_collect else None,
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
    return list(
        (
            await session.execute(
                select(Run)
                .where(Run.kind != "collect")
                .where(Run.n_summarized > 0)
                .order_by(Run.started_at.desc())
                .limit(limit)
            )
        ).scalars()
    )


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
    from ainews.web.views import relative_age

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
    from ainews.web.views import to_local

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
