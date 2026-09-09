"""Export a real day, and load it back into an empty database.

Two functions with one shape between them, so a field added to a model that is
not added here is a field the demo silently loses. The export writes plain
dicts keyed by the model's own column names; the seed reads them back in
dependency order, remapping ids as it goes.

**Timestamps are stored as offsets**, in seconds, from the newest bulletin's
`created_at`. On seed, that anchor becomes *now*, so the newest bulletin is
today's and the run log's countdown reads as it did on the day. Absolute dates
would put the front page in the past on the second day after the export, and
`/` shows today's bulletin - a demo whose front page is empty demonstrates
nothing.

The day a bulletin was *really* published is kept in `recorded_day`, which the
band above the reading names. The screen never claims the news is fresh.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.clock import local_day
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
)

log = logging.getLogger(__name__)

DEMO_PATH = Path(__file__).resolve().parent / "demo.json"
SCHEMA = 1
DEFAULT_BULLETINS = 3


class DemoEmpty(RuntimeError):
    """Nothing to export: the archive holds no published bulletin."""


class DemoNotEmpty(RuntimeError):
    """The database already holds a bulletin. Seeding would mix two archives."""


@dataclass(slots=True)
class DemoData:
    schema: int = SCHEMA
    # The local day the newest exported bulletin was really published. The band
    # above the reading names it, so the demo never claims to be this morning.
    recorded_day: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    articles: list[dict[str, Any]] = field(default_factory=list)
    summaries: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    run_steps: list[dict[str, Any]] = field(default_factory=list)
    bulletins: list[dict[str, Any]] = field(default_factory=list)
    bulletin_items: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    eval_results: list[dict[str, Any]] = field(default_factory=list)


def _offset(moment: datetime | None, anchor: datetime) -> float | None:
    return None if moment is None else (moment - anchor).total_seconds()


def _moment(offset: float | None, anchor: datetime) -> datetime | None:
    return None if offset is None else anchor + timedelta(seconds=offset)


# -- export -------------------------------------------------------------------


async def export_demo(session: AsyncSession, *, bulletins: int = DEFAULT_BULLETINS) -> DemoData:
    """The newest `bulletins` published days, and everything they need to draw.

    Everything, because the demo's job is the *whole* application: the reading,
    the archive, the run log, one run node by node, the reader's labels and the
    numbers the press measured. Exporting the bulletin alone would leave four of
    the six screens empty, which is the state the demo exists to fix.
    """
    chosen = list(
        (
            await session.execute(
                select(Bulletin)
                .order_by(Bulletin.day.desc(), Bulletin.version.desc())
                .limit(bulletins)
            )
        ).scalars()
    )
    if not chosen:
        raise DemoEmpty("no bulletin in this archive; press the button once first")

    anchor = max(b.created_at for b in chosen)
    data = DemoData(recorded_day=chosen[0].day)

    # Every summary of every exported day, not only the published ones: "the
    # other N" under the bulletin is one of the screens, and a demo that drew an
    # empty one would be demonstrating the wrong thing.
    days = {b.day for b in chosen}
    languages = {b.language for b in chosen}
    published_ids = set(
        (
            await session.execute(
                select(BulletinItem.summary_id).where(
                    BulletinItem.bulletin_id.in_([b.id for b in chosen])
                )
            )
        ).scalars()
    )
    summaries = list(
        (
            await session.execute(
                select(Summary).where(Summary.language.in_(languages)).order_by(Summary.id)
            )
        ).scalars()
    )
    summaries = [s for s in summaries if s.id in published_ids or local_day(s.created_at) in days]

    article_ids = {s.article_id for s in summaries}
    articles = list(
        (
            await session.execute(
                select(Article).where(Article.id.in_(article_ids)).order_by(Article.id)
            )
        ).scalars()
    )
    source_ids = {a.source_id for a in articles}
    sources = list(
        (
            await session.execute(
                select(Source).where(Source.id.in_(source_ids)).order_by(Source.id)
            )
        ).scalars()
    )
    run_ids = {b.run_id for b in chosen if b.run_id}
    runs = list(
        (
            await session.execute(select(Run).where(Run.id.in_(run_ids)).order_by(Run.started_at))
        ).scalars()
    )
    steps = list(
        (
            await session.execute(
                select(RunStep).where(RunStep.run_id.in_(run_ids)).order_by(RunStep.started_at)
            )
        ).scalars()
    )
    items = list(
        (
            await session.execute(
                select(BulletinItem)
                .where(BulletinItem.bulletin_id.in_([b.id for b in chosen]))
                .order_by(BulletinItem.bulletin_id, BulletinItem.position)
            )
        ).scalars()
    )
    summary_ids = {s.id for s in summaries}
    verdicts = [
        v
        for v in (await session.execute(select(Verdict).order_by(Verdict.id))).scalars()
        if v.summary_id in summary_ids
    ]
    evals = list(
        (
            await session.execute(
                select(EvalResult)
                .where(EvalResult.bulletin_id.in_([b.id for b in chosen]))
                .order_by(EvalResult.id)
            )
        ).scalars()
    )

    data.sources = [
        {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "kind": s.kind,
            "weight": s.weight,
            "enabled": s.enabled,
        }
        for s in sources
    ]
    data.articles = [
        {
            "id": a.id,
            "source_id": a.source_id,
            "url": a.url,
            "url_canonical": a.url_canonical,
            "title": a.title,
            "published_at": _offset(a.published_at, anchor),
            "fetched_at": _offset(a.fetched_at, anchor),
            "dup_of": a.dup_of,
            # The body is deliberately not here. See the package docstring: the
            # repository is public and an article is someone else's text.
            "body_source": a.body_source,
        }
        for a in articles
    ]
    data.summaries = [
        {
            "id": s.id,
            "article_id": s.article_id,
            "language": s.language,
            "title_local": s.title_local,
            "summary": s.summary,
            "why_it_matters": s.why_it_matters,
            "tags_json": s.tags_json,
            "importance": s.importance,
            "key_fact": s.key_fact,
            "relevant": s.relevant,
            "kind": s.kind,
            "model": s.model,
            "tokens_in": s.tokens_in,
            "tokens_out": s.tokens_out,
            "est_cost_usd": s.est_cost_usd,
            "created_at": _offset(s.created_at, anchor),
        }
        for s in summaries
    ]
    data.runs = [
        {
            "id": r.id,
            "kind": r.kind,
            "language": r.language,
            "status": r.status,
            "started_at": _offset(r.started_at, anchor),
            "finished_at": _offset(r.finished_at, anchor),
            "n_collected": r.n_collected,
            "n_new": r.n_new,
            "n_summarized": r.n_summarized,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "est_cost_usd": r.est_cost_usd,
            "model_summarize": r.model_summarize,
            "model_rank": r.model_rank,
            "error": r.error,
        }
        for r in runs
    ]
    data.run_steps = [
        {
            "run_id": s.run_id,
            "node": s.node,
            "status": s.status,
            "started_at": _offset(s.started_at, anchor),
            "finished_at": _offset(s.finished_at, anchor),
            "n_in": s.n_in,
            "n_out": s.n_out,
            "model": s.model,
            "tokens_in": s.tokens_in,
            "tokens_out": s.tokens_out,
            "est_cost_usd": s.est_cost_usd,
            "detail": s.detail,
        }
        for s in steps
    ]
    data.bulletins = [
        {
            "id": b.id,
            # Kept as a day offset rather than a date, so the newest exported
            # bulletin becomes today and the ones before it keep their spacing.
            "day_offset": (
                datetime.fromisoformat(b.day).date() - datetime.fromisoformat(chosen[0].day).date()
            ).days,
            "language": b.language,
            "version": b.version,
            "editor_note": b.editor_note,
            "model_rank": b.model_rank,
            "agreement": b.agreement,
            "est_cost_usd": b.est_cost_usd,
            "checks_json": b.checks_json,
            "run_id": b.run_id,
            "created_at": _offset(b.created_at, anchor),
        }
        for b in chosen
    ]
    data.bulletin_items = [
        {
            "bulletin_id": i.bulletin_id,
            "summary_id": i.summary_id,
            "position": i.position,
            "tier": i.tier,
            "reason": i.reason,
        }
        for i in items
    ]
    data.verdicts = [
        {
            "summary_id": v.summary_id,
            "verdict": v.verdict,
            "reason": v.reason,
            "note": v.note,
            "created_at": _offset(v.created_at, anchor),
        }
        for v in verdicts
    ]
    data.eval_results = [
        {
            "bulletin_id": e.bulletin_id,
            "summary_id": e.summary_id,
            "kind": e.kind,
            "passed": e.passed,
            "detail": e.detail,
            "model": e.model,
            "prompt_version": e.prompt_version,
            "tokens_in": e.tokens_in,
            "tokens_out": e.tokens_out,
            "est_cost_usd": e.est_cost_usd,
            "created_at": _offset(e.created_at, anchor),
        }
        for e in evals
    ]
    return data


def write_demo(data: DemoData, path: Path | None = None) -> Path:
    path = path or DEMO_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": data.schema,
        "recorded_day": data.recorded_day,
        "sources": data.sources,
        "articles": data.articles,
        "summaries": data.summaries,
        "runs": data.runs,
        "run_steps": data.run_steps,
        "bulletins": data.bulletins,
        "bulletin_items": data.bulletin_items,
        "verdicts": data.verdicts,
        "eval_results": data.eval_results,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def load_demo(path: Path | None = None) -> DemoData:
    path = path or DEMO_PATH
    if not path.exists():
        raise LookupError(f"no demo data at {path}; run `ainews demo export` first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(
            f"demo data at {path} is schema {payload.get('schema')}, expected {SCHEMA}"
        )
    return DemoData(**payload)


# -- seed ---------------------------------------------------------------------


async def seed_demo(
    session: AsyncSession, data: DemoData | None = None, *, force: bool = False
) -> int:
    """Load the recorded day into this database. Returns the summaries written.

    Refuses on a database that already holds a bulletin unless `force`. Two
    archives in one file is the failure that cannot be undone from here: the
    demo's summaries and a real run's would be indistinguishable afterwards, and
    one of them is a recording.
    """
    data = data or load_demo()
    existing = (await session.execute(select(func.count(Bulletin.id)))).scalar_one()
    if existing and not force:
        raise DemoNotEmpty(
            f"this database already holds {existing} bulletin(s); "
            "seeding would mix a recording into a real archive"
        )

    anchor = datetime.now(UTC)
    today = local_day(anchor)

    # The feed list is seeded from `feeds.yaml` at every start, so by the time
    # this runs the sources already exist and `sources.url` is unique. They are
    # matched rather than inserted: the demo's sources *are* the shipped feeds,
    # and a second row per feed would double every count on `/sources`.
    existing = {
        url: sid for sid, url in (await session.execute(select(Source.id, Source.url))).all()
    }
    source_ids: dict[int, int] = {}
    for row in data.sources:
        if row["url"] in existing:
            source_ids[row["id"]] = existing[row["url"]]
            continue
        source = Source(
            name=row["name"],
            url=row["url"],
            kind=row["kind"],
            weight=row["weight"],
            enabled=row["enabled"],
        )
        session.add(source)
        await session.flush()
        source_ids[row["id"]] = source.id

    article_ids: dict[int, int] = {}
    for row in data.articles:
        article = Article(
            source_id=source_ids[row["source_id"]],
            url=row["url"],
            url_canonical=row["url_canonical"],
            title=row["title"],
            published_at=_moment(row["published_at"], anchor),
            fetched_at=_moment(row["fetched_at"], anchor) or anchor,
            body_source=row["body_source"],
        )
        session.add(article)
        await session.flush()
        article_ids[row["id"]] = article.id
    # A second pass: `dup_of` points at an article that may not have existed yet.
    for row in data.articles:
        if row["dup_of"] and row["dup_of"] in article_ids:
            article = await session.get(Article, article_ids[row["id"]])
            if article is not None:
                article.dup_of = article_ids[row["dup_of"]]

    summary_ids: dict[int, int] = {}
    for row in data.summaries:
        summary = Summary(
            article_id=article_ids[row["article_id"]],
            language=row["language"],
            title_local=row["title_local"],
            summary=row["summary"],
            why_it_matters=row["why_it_matters"],
            tags_json=row["tags_json"],
            importance=row["importance"],
            key_fact=row["key_fact"],
            relevant=row["relevant"],
            kind=row["kind"],
            model=row["model"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            est_cost_usd=row["est_cost_usd"],
            created_at=_moment(row["created_at"], anchor) or anchor,
        )
        session.add(summary)
        await session.flush()
        summary_ids[row["id"]] = summary.id

    run_ids: dict[str, str] = {}
    for row in data.runs:
        run = Run(
            kind=row["kind"],
            language=row["language"],
            status=row["status"],
            started_at=_moment(row["started_at"], anchor) or anchor,
            finished_at=_moment(row["finished_at"], anchor),
            n_collected=row["n_collected"],
            n_new=row["n_new"],
            n_summarized=row["n_summarized"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            est_cost_usd=row["est_cost_usd"],
            model_summarize=row["model_summarize"],
            model_rank=row["model_rank"],
            error=row["error"],
        )
        session.add(run)
        await session.flush()
        run_ids[row["id"]] = run.id

    for row in data.run_steps:
        session.add(
            RunStep(
                run_id=run_ids[row["run_id"]],
                node=row["node"],
                status=row["status"],
                started_at=_moment(row["started_at"], anchor) or anchor,
                finished_at=_moment(row["finished_at"], anchor),
                n_in=row["n_in"],
                n_out=row["n_out"],
                model=row["model"],
                tokens_in=row["tokens_in"],
                tokens_out=row["tokens_out"],
                est_cost_usd=row["est_cost_usd"],
                detail=row["detail"],
            )
        )

    base = datetime.fromisoformat(today).date()
    bulletin_ids: dict[int, int] = {}
    for row in data.bulletins:
        bulletin = Bulletin(
            day=(base + timedelta(days=row["day_offset"])).isoformat(),
            language=row["language"],
            version=row["version"],
            editor_note=row["editor_note"],
            model_rank=row["model_rank"],
            agreement=row["agreement"],
            est_cost_usd=row["est_cost_usd"],
            checks_json=row["checks_json"],
            run_id=run_ids.get(row["run_id"]) if row["run_id"] else None,
            created_at=_moment(row["created_at"], anchor) or anchor,
        )
        session.add(bulletin)
        await session.flush()
        bulletin_ids[row["id"]] = bulletin.id

    for row in data.bulletin_items:
        session.add(
            BulletinItem(
                bulletin_id=bulletin_ids[row["bulletin_id"]],
                summary_id=summary_ids[row["summary_id"]],
                position=row["position"],
                tier=row["tier"],
                reason=row["reason"],
            )
        )

    for row in data.verdicts:
        session.add(
            Verdict(
                summary_id=summary_ids[row["summary_id"]],
                verdict=row["verdict"],
                reason=row["reason"],
                note=row["note"],
                created_at=_moment(row["created_at"], anchor) or anchor,
            )
        )

    for row in data.eval_results:
        session.add(
            EvalResult(
                bulletin_id=bulletin_ids.get(row["bulletin_id"]) if row["bulletin_id"] else None,
                summary_id=summary_ids.get(row["summary_id"]) if row["summary_id"] else None,
                kind=row["kind"],
                passed=row["passed"],
                detail=row["detail"],
                model=row["model"],
                prompt_version=row["prompt_version"],
                tokens_in=row["tokens_in"],
                tokens_out=row["tokens_out"],
                est_cost_usd=row["est_cost_usd"],
                created_at=_moment(row["created_at"], anchor) or anchor,
            )
        )

    await session.commit()
    log.info(
        "seeded the recorded day of %s: %d summaries, %d bulletins",
        data.recorded_day,
        len(data.summaries),
        len(data.bulletins),
    )
    return len(data.summaries)
