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

from ainews.clock import day_bounds, to_local
from ainews.config import Language, get_settings
from ainews.db import (
    Article,
    Bulletin,
    BulletinItem,
    EvalResult,
    Run,
    RunStep,
    Source,
    Summary,
    Verdict,
    bulletin_runs,
)
from ainews.web.bulletin import tier_step
from ainews.web.format import relative_age
from ainews.web.i18n import Strings


@dataclass(slots=True)
class Page[T]:
    """One screenful of a list that has more behind it, and how much more.

    Four lists were capped and silent: the archive at 30, search at 60, the
    verdicts at 200, the run log at 40. A page that shows thirty of forty-seven
    and says nothing is not a page with a limit on it, it is a page that is
    wrong - the reader has no way to tell a short list from a truncated one, and
    the app is a record whose whole point is that the archive is kept.

    Not offset paging. This is a single-reader tool and the question is "is there
    more", not "take me to page four": `more` says so and `next_limit` doubles
    the window, so a reader who wants everything presses twice and a reader who
    does not pays for thirty rows.
    """

    rows: list[T]
    total: int
    limit: int

    @property
    def more(self) -> bool:
        return self.total > len(self.rows)

    @property
    def next_limit(self) -> int:
        return max(self.limit * 2, len(self.rows) + 1)

    def __iter__(self):
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __bool__(self) -> bool:
        return bool(self.rows)


# A ceiling on `?limit=`, so a typed or pasted URL cannot ask for a million rows
# and turn a read into a table scan the page then tries to render.
LIMIT_CEILING = 2000


def page_limit(raw: int | None, default: int) -> int:
    """`?limit=` from the query string, clamped to something a page can draw."""
    if raw is None or raw < 1:
        return default
    return min(raw, LIMIT_CEILING)


@dataclass(slots=True)
class Story:
    """One story as the page draws it.

    `tier` and `importance` are both here and neither is the other's fallback
    (ADR 0030). A story inside a bulletin has a tier - the editor's placement -
    and a story outside one has only the summariser's 1-5 score, given with one
    article in view. `step` picks whichever applies, and the two never mix
    within a list because a list is either the bulletin or the rest of the day.
    """

    id: int
    source: str
    url: str
    title_local: str
    summary: str
    why_it_matters: str
    importance: int
    tags: list[str]
    age: str
    tier: str | None = None
    # The ranker's own sentence for the placement, where it gave one.
    reason: str | None = None
    # The reader's verdict on this summary, if one was given: "ok" | "wrong".
    verdict: str | None = None
    # Which of the four a "wrong" was about, or `None` on an `ok` and on the
    # labels given before the reader was asked.
    verdict_reason: str | None = None
    verdict_note: str | None = None

    @property
    def step(self) -> int:
        """The ink-and-size step the headline is drawn at (`theme.css`, `.p1`-`.p5`)."""
        return tier_step(self.tier) or self.importance


def archive_bulletins() -> Any:
    """What `/archive` lists, and what the rail badge counts.

    One selector for both, because they were two and disagreed: the badge
    counted `kind == "digest"` while every press wrote `manual`, so it read 1
    over a list of 3 (ADR 0026). A count is a promise about what is behind the
    link, and it can only keep that promise by being the same query.

    Every version, not only the current one. A superseded bulletin is what the
    reader was shown that morning, and an archive with the replaced pages
    removed is not a record - the row says which version it is and the template
    marks the ones that were overtaken.
    """
    return select(Bulletin).order_by(
        Bulletin.day.desc(), Bulletin.version.desc(), Bulletin.id.desc()
    )


async def latest_bulletin(session: AsyncSession) -> Bulletin | None:
    """The bulletin the front page shows: the newest version of the newest day.

    Not filtered by the page's language. That made the shell's TR/EN switch a
    content filter: a reader on a Turkish page who pressed `English` got "no
    digest yet", because the bulletin sitting in the database was Turkish. The
    switch translates the buttons and nothing else (ADR 0017); the bar names the
    bulletin's language when it differs from the page's, so a Turkish shell
    around English stories is labelled rather than silently served.

    One query, where there used to be two and a policy module. A press writes a
    new version of the day (ADR 0030), so there is no longer a two-story
    supplement to detect, no floor for it to clear and no horizon to reach back
    over for the fuller bulletin it supplements.
    """
    return (await session.execute(archive_bulletins().limit(1))).scalar_one_or_none()


async def bulletin_by_id(session: AsyncSession, bulletin_id: int) -> Bulletin | None:
    return await session.get(Bulletin, bulletin_id)


async def newer_version(session: AsyncSession, bulletin: Bulletin) -> Bulletin | None:
    """The version that replaced this one, if any - what marks an archive row as
    superseded and what the reader is offered instead of a page they have to
    work out is stale."""
    return (
        await session.execute(
            archive_bulletins()
            .where(Bulletin.day == bulletin.day)
            .where(Bulletin.language == bulletin.language)
            .where(Bulletin.version > bulletin.version)
            .limit(1)
        )
    ).scalar_one_or_none()


async def bulletins_page(session: AsyncSession, limit: int = 30) -> Page[Bulletin]:
    """Every bulletin there is, newest first - the archive does not filter by
    language for the same reason the digest does not (ADR 0017); each row in the
    picker carries its own language, so a mixed archive reads as a list of
    bulletins rather than as a page that lost half its history."""
    rows = list((await session.execute(archive_bulletins().limit(limit))).scalars())
    return Page(rows=rows, total=await count_archive(session), limit=limit)


async def count_archive(session: AsyncSession) -> int:
    """How many bulletins the archive holds - the rail badge's number, counted
    off the very query that draws the list."""
    return (
        await session.execute(select(func.count()).select_from(archive_bulletins().subquery()))
    ).scalar_one()


def _to_story(
    summary: Summary,
    article: Article,
    source_name: str,
    age: str,
    verdict: Verdict | None = None,
    item: BulletinItem | None = None,
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
        # The summariser's own score, given with one article in view. What the
        # headline is drawn at is `Story.step`, which prefers the tier where
        # there is one and never mixes the two scales (ADR 0030).
        importance=summary.importance,
        tags=tags,
        age=age,
        tier=item.tier if item else None,
        reason=item.reason if item else None,
        verdict=verdict.verdict if verdict else None,
        verdict_reason=verdict.reason if verdict else None,
        verdict_note=verdict.note if verdict else None,
    )


def _matches(story: Story, tag: str | None) -> bool:
    """The exact membership test the SQL narrowing cannot make.

    `tags_json` is a JSON array in a text column, so `"openai"` as a substring
    can only appear as a whole element - but the query's `contains` is a
    substring match, and this is what makes the filter mean what it says.
    """
    return not tag or tag in story.tags


def _tagged(query: Any, tag: str | None) -> Any:
    """A cheap narrowing, not the decision.

    What it saves is loading ninety rows to keep ten; what it refuses to do is
    make SQLite parse the JSON, which fails the whole query on one malformed row
    rather than skipping it.
    """
    return query.where(Summary.tags_json.contains(f'"{tag}"', autoescape=True)) if tag else query


def _story_columns() -> Any:
    return (
        select(Summary, Article, Source.name, Verdict)
        .join(Article, Article.id == Summary.article_id)
        .join(Source, Source.id == Article.source_id)
        .outerjoin(Verdict, Verdict.summary_id == Summary.id)
    )


async def stories_for_bulletin(
    session: AsyncSession,
    bulletin: Bulletin,
    *,
    language: Language,
    tag: str | None = None,
) -> list[Story]:
    """The bulletin, in the order the editor put it in.

    `position` and only `position`. The tier is what the typography draws, not
    what the list sorts by - two majors and a notable can read major, notable,
    major if that is the day's argument, and sorting by tier would rewrite the
    editor's order into groups. This is the whole of what replaced
    `bulletin.reading_order`, which existed to reconcile two scores that no
    longer both exist.

    `language` is the page's, not the bulletin's, and only the age string uses
    it: "3 saat" is a button, not reporting - it is written by this app rather
    than by the model, so it follows the shell even when the stories under it
    were written in the other language.
    """
    query = _tagged(
        _story_columns()
        .add_columns(BulletinItem)
        .join(BulletinItem, BulletinItem.summary_id == Summary.id)
        .where(BulletinItem.bulletin_id == bulletin.id)
        .order_by(BulletinItem.position),
        tag,
    )
    stories = [
        _to_story(
            summary, article, name, relative_age(article.published_at, language), verdict, item
        )
        for summary, article, name, verdict, item in (await session.execute(query)).all()
    ]
    return [story for story in stories if _matches(story, tag)]


def _rest_of_day(bulletin: Bulletin) -> Any:
    """The day's summaries the bulletin left out.

    Bucketed on `created_at` against the local day's UTC bounds, not on the
    collection window the ranker drew from: the window moves and the archive
    does not, so a bulletin read a month later would otherwise show a different
    set of also-rans each time it was opened.

    Irrelevant summaries are left out here as well as out of the pool (ADR
    0031). This block is the rest of the day's *reading*, not an audit of the
    summariser: an item the summariser called not-AI-news is noise wherever it
    is drawn, and one of the feeds is a personal blog that also publishes on map
    projections. The cost is that a wrong `relevant=false` is invisible on the
    page and has to be found through search.
    """
    start, end = day_bounds(bulletin.day)
    in_bulletin = (
        select(BulletinItem.id)
        .where(BulletinItem.summary_id == Summary.id)
        .where(BulletinItem.bulletin_id == bulletin.id)
        .exists()
    )
    return (
        _story_columns()
        .where(Summary.language == bulletin.language)
        .where(Summary.created_at >= start)
        .where(Summary.created_at < end)
        .where(Summary.relevant.is_(True))
        .where(~in_bulletin)
    )


async def other_stories(
    session: AsyncSession,
    bulletin: Bulletin,
    *,
    language: Language,
    tag: str | None = None,
) -> list[Story]:
    """What the day held and the editor did not publish, heaviest first.

    Appended after the bulletin rather than merged into it. They carry no tier,
    so they draw at the summariser's own score, and keeping them in their own
    block is what stops the two scales being compared by eye - the failure ADR
    0030 names, where a ranked 3-that-became-5 was drawn larger than an honest
    unranked 3 in a layout whose only ranking indicator is size.
    """
    query = _tagged(_rest_of_day(bulletin), tag).order_by(
        Summary.importance.desc(), Summary.id.asc()
    )
    stories = [
        _to_story(summary, article, name, relative_age(article.published_at, language), verdict)
        for summary, article, name, verdict in (await session.execute(query)).all()
    ]
    return [story for story in stories if _matches(story, tag)]


async def count_others(session: AsyncSession, bulletin: Bulletin) -> int:
    """How many stories sit behind the expander - counted, so the label is a
    promise about what is behind the link rather than an estimate of it."""
    return (
        await session.execute(
            select(func.count()).select_from(_rest_of_day(bulletin).order_by(None).subquery())
        )
    ).scalar_one()


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


@dataclass(slots=True)
class TagCounts:
    """The day's tags counted two ways, from one pass over its rows.

    Both numbers are on screen at once whenever `?all=1` is on: the filter row
    counts the list the reader can reach, and the brief's footnote counts the
    *bulletin* - the same stories the rail badge counts - so opening the full
    list must not leave "11 haber" beside a topic count taken over ninety-one.
    Two calls would scan every `tags_json` in the day a second time to produce a
    single integer.
    """

    published: list[tuple[str, int]]
    every: list[tuple[str, int]]

    def shown(self, published_only: bool) -> list[tuple[str, int]]:
        return self.published if published_only else self.every


async def tag_counts(session: AsyncSession, bulletin: Bulletin) -> TagCounts:
    """Tags across the day, most common first, in both scopes.

    Counted in Python rather than in SQL because the tags live in a JSON column;
    at a hundred rows a day that is not worth a second table, and a malformed
    value is skipped here rather than failing a `json_each` mid-query.

    The published scope has to track the list the page is showing. Before it
    existed the counts were taken over every summary the run produced while the
    filter they label narrowed the *ranked* fifteen, so the row said
    `agents 27` on a page headed "15 haber" and pressing it returned four
    stories. A count is a promise about what is behind the link.
    """
    start, end = day_bounds(bulletin.day)
    published = set(
        (
            await session.execute(
                select(BulletinItem.summary_id).where(BulletinItem.bulletin_id == bulletin.id)
            )
        ).scalars()
    )
    rows = (
        await session.execute(
            select(Summary.id, Summary.tags_json)
            .where(Summary.language == bulletin.language)
            .where(Summary.created_at >= start)
            .where(Summary.created_at < end)
        )
    ).all()
    # A bulletin may carry a story summarised on an earlier day - the pool is a
    # window, not a calendar (`nodes/rank.day_pool`) - so its tags are counted
    # from the item list rather than from the day's rows alone.
    missing = published - {summary_id for summary_id, _ in rows}
    if missing:
        rows = list(rows) + list(
            (
                await session.execute(
                    select(Summary.id, Summary.tags_json).where(Summary.id.in_(missing))
                )
            ).all()
        )

    in_bulletin: dict[str, int] = {}
    every: dict[str, int] = {}
    for summary_id, raw in rows:
        try:
            tags = json.loads(raw or "[]")
        except json.JSONDecodeError:
            continue
        for tag in tags:
            every[tag] = every.get(tag, 0) + 1
            if summary_id in published:
                in_bulletin[tag] = in_bulletin.get(tag, 0) + 1

    def ordered(counts: dict[str, int]) -> list[tuple[str, int]]:
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    return TagCounts(published=ordered(in_bulletin), every=ordered(every))


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
    # The sources' own clocks, not the last `collect` run: the feeds are polled
    # by every run, and a digest's first node is the same poll. Reading the run
    # table here makes `/runs` say the sources were last read two days ago while
    # the digest that just finished read them a minute earlier.
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


async def recent_runs(session: AsyncSession, limit: int = 12) -> Page[Run]:
    """The run log: polls and bulletins together, because the log is about the
    machine rather than about the reading."""
    rows = list(
        (await session.execute(select(Run).order_by(Run.started_at.desc()).limit(limit))).scalars()
    )
    total = (await session.execute(select(func.count()).select_from(Run))).scalar_one()
    return Page(rows=rows, total=total, limit=limit)


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
) -> Page[Story]:
    """Full-text hits across every bulletin, in every language.

    `language` reaches only the age string. The index is not filtered by it: a
    reader looking for a company name wants the article, and refusing to show it
    because the digest that covered it was written in the other language is the
    same content filter the shell's switch stopped being (ADR 0017).
    """
    expression = _fts_query(raw_query)
    if not expression:
        return Page(rows=[], total=0, limit=limit)

    # The count first, because "60 of 214 hits" is the sentence a search box owes
    # its reader, and FTS5 answers it without materialising the rows.
    total = (
        await session.execute(
            text("SELECT count(*) FROM summaries_fts WHERE summaries_fts MATCH :q"),
            {"q": expression},
        )
    ).scalar_one()
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
        return Page(rows=[], total=total, limit=limit)

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

    return Page(
        rows=[
            _to_story(summary, article, name, relative_age(article.published_at, language), verdict)
            for summary, article, name, verdict in rows
        ],
        total=total,
        limit=limit,
    )


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


def _home_bulletin() -> Any:
    """The newest bulletin a summary appears in, as a correlated subquery.

    What a link to a labelled story needs. A summary can be in two bulletins -
    two versions of one day - and the newest is the page the reader would be
    shown if they went looking, so it is the page the link lands on. `NULL` for
    a summary the editor never published, which the templates draw as a row
    with no link rather than as a link to nothing.
    """
    return (
        select(BulletinItem.bulletin_id)
        .join(Bulletin, Bulletin.id == BulletinItem.bulletin_id)
        .where(BulletinItem.summary_id == Summary.id)
        .order_by(Bulletin.day.desc(), Bulletin.version.desc())
        .limit(1)
        .scalar_subquery()
    )


@dataclass(slots=True)
class LabelledStory:
    """One row of `/runs/verdicts`: a verdict, and enough of the story to place it.

    Not a `Story`. That shape is the reading - a summary, a why-it-matters, a
    tag list - and this table draws none of it: the column that matters here is
    the reader's own sentence, and the story is the address it was written at.
    """

    summary_id: int
    bulletin_id: int | None
    decided_at: datetime
    source: str
    title_local: str
    verdict: str
    # Which of the four a "wrong" was about, or `None`. Drawn beside the word
    # rather than in the note column: only `wrong_fact` is about the summariser,
    # and a table that does not say which is which is a table where three
    # different findings look like the same one.
    reason: str | None
    note: str | None


async def labelled_stories(session: AsyncSession, limit: int = 200) -> Page[LabelledStory]:
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
    times read as unsorted. 200 is a window rather than a cap now: the page says
    how many labels there are and offers the rest (`Page`).
    """
    rows = (
        await session.execute(
            select(Verdict, Summary, Source.name, _home_bulletin().label("bulletin_id"))
            .join(Summary, Summary.id == Verdict.summary_id)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .order_by(Verdict.created_at.desc())
            .limit(limit)
        )
    ).all()
    total = (await session.execute(select(func.count(Verdict.id)))).scalar_one()
    return Page(
        rows=[
            LabelledStory(
                summary_id=summary.id,
                bulletin_id=bulletin_id,
                decided_at=verdict.created_at,
                source=source_name,
                title_local=summary.title_local,
                verdict=verdict.verdict,
                reason=verdict.reason,
                note=verdict.note,
            )
            for verdict, summary, source_name, bulletin_id in rows
        ],
        total=total,
        limit=limit,
    )


@dataclass(slots=True)
class Finding:
    """One summary the grounding judge failed, and whether the reader has answered.

    The judge's most information-dense output - the sentence it could not
    support - was written to `eval_results.detail`, printed once by the command
    that produced it, and never put in front of the person whose verdict it is
    calibrated against. This is that row, shaped for `/runs/verdicts`.
    """

    summary_id: int
    bulletin_id: int | None
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
            select(EvalResult, Summary, Source.name, Verdict, _home_bulletin().label("bulletin_id"))
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
    for result, summary, source_name, verdict, bulletin_id in rows:
        if summary.id in seen:
            continue
        seen.add(summary.id)
        if result.passed is not False or not result.detail:
            continue
        findings.append(
            Finding(
                summary_id=summary.id,
                bulletin_id=bulletin_id,
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


@dataclass(slots=True)
class QualityRow:
    """One measured thing about a published day, as the page draws it."""

    label: str
    value: str
    # The bar's fill, 0..1, or None for a row that is a count rather than a
    # share. A count has no denominator, and a bar drawn for one would be
    # inventing a scale.
    share: float | None = None


async def published_quality(session: AsyncSession, run_id: str, t: Strings) -> list[QualityRow]:
    """What the press measured about the bulletin it published.

    Read off `bulletins.checks_json`, which the press wrote (PLAN-V2 5.5). Not
    recomputed here: the web layer draws what was measured, and a page that
    re-scored an old day with today's checks would be quietly reporting a
    different experiment under the run's own heading.

    Empty for a run that published nothing, and for one published before the
    column existed - the block is simply not drawn, which is what the run log
    already does for a run with no steps.
    """
    bulletin = (
        await session.execute(
            select(Bulletin)
            .where(Bulletin.run_id == run_id)
            .order_by(Bulletin.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if bulletin is None or not bulletin.checks_json:
        return []
    try:
        stored = json.loads(bulletin.checks_json)
    except json.JSONDecodeError:
        return []

    content = stored.get("content") or {}
    budget = stored.get("budget") or {}
    over = (budget.get("over_share") or {}).get("summary")
    tags = stored.get("tags") or {}

    def share_row(label: str, value: float | None, *, invert: bool = False) -> QualityRow:
        """`invert` turns a share that went wrong into the share that went right.

        The number and the bar are the same quantity. Inverting only the bar
        would draw a full bar beside a 0%, which is the one thing a row with
        both cannot do.
        """
        if value is None:
            return QualityRow(label, t["quality_na"])
        shown = 1.0 - value if invert else value
        return QualityRow(label, f"{shown:.0%}", shown)

    return [
        share_row(t["quality_key_fact"], content.get("key_fact_share")),
        share_row(t["quality_key_fact_kept"], content.get("key_fact_kept")),
        share_row(t["quality_numeral_recall"], content.get("numeral_recall")),
        # Drawn as "kept the budget" rather than "went over it", so every bar on
        # the block fills in the same direction and a long bar is always good.
        share_row(t["quality_budget"], over, invert=True),
        share_row(t["quality_vocabulary"], tags.get("in_vocabulary_share")),
        QualityRow(t["quality_ungrounded"], str(len(stored.get("ungrounded") or []))),
    ]
