"""The digest page, the archive and search.

Three routes because they are three URLs a person types or bookmarks, but one
template family: they all render the same story list, read at different times.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.db import Run, Summary, Verdict, db_session
from ainews.db.models import utcnow
from ainews.web import queries
from ainews.web.format import impact_split
from ainews.web.i18n import strings
from ainews.web.views import (
    get_templates,
    language_of,
    remember_preferences,
    shell_context,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    all: bool = Query(False, description="show everything summarised, not just the ranked top N"),
    tag: str | None = None,
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    language = language_of(request)
    # Not filtered by `language`: the shell's switch translates the interface,
    # it does not choose which bulletin exists (ADR 0017).
    run = await queries.latest_digest_run(session)

    context = await shell_context(request, session, language, page="digest", run=run)
    context.update(
        {
            "run": run,
            "editor_note": run.editor_note if run else None,
            "stories": [],
            "n_others": 0,
            "tags": [],
            "n_topics": 0,
            "split": [],
            "tag": tag,
            "show_all": all,
        }
    )

    if run is not None:
        stories = await queries.stories_for_run(
            session, run, language=language, ranked_only=not all, tag=tag
        )
        context["stories"] = stories
        # Only offer the expander when there is something behind it.
        context["n_others"] = 0 if all else await queries.count_unranked(session, run)
        # Every count on this page is a count of the list the reader can reach.
        # `all=1` widens the list, so it widens the counts with it - a filter
        # pill promising 27 stories on a page that holds fifteen, and returning
        # four when pressed, was three numbers disagreeing about one day.
        ranked_only = not all
        # One pass over the run's rows, both scopes counted. The filter row
        # follows the list on screen, so it draws twelve of whichever scope is
        # showing and the disclosure the rest.
        topics = await queries.tag_counts(session, run)
        context["tags"] = topics.shown(ranked_only)[:12]
        # The brief's footnote does not follow it. That line is the size of the
        # *digest* - the same eleven stories the rail badge counts - so opening
        # `?all=1` must not leave "11 haber" beside a topic count taken over
        # ninety-one. It counted `tags|length` until 2026-09-06, which was the
        # row's twelve-item cap reporting itself as a measurement.
        context["n_topics"] = len(topics.ranked)
        # How heavy the day in view is, drawn at the end of the topic row. It is
        # counted off the list already in `context` rather than off the run, so
        # the filtered page answers for its filter.
        #
        # `topic_pulse` was called beside it until 2026-09-08, for the right
        # rail's themes list. That list drew a name, a share and a count per
        # topic, which is the filter row three lines above it written a second
        # time; the rail came off and the query went with it.
        context["split"] = impact_split(stories)

    response = get_templates(request).TemplateResponse(request, "index.html", context)
    remember_preferences(request, response, language)
    return response


@router.get("/archive", response_class=HTMLResponse)
async def archive(
    request: Request,
    run: str | None = None,
    all: bool = Query(False, description="show everything summarised, not just the ranked top N"),
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    """One past bulletin, and the list of the others.

    `all=1` is the digest's own flag, taken here for one caller: `/runs/verdicts`
    links a labelled story by its anchor, and a verdict can be given on a story
    below the fold - the digest offers the two words on every rendered item,
    ranked or not. Without the flag that link lands on a page the story is not
    on, which is a dead anchor rather than a visible error.
    """
    language = language_of(request)
    runs = await queries.digest_runs(session)
    selected: Run | None = None
    if runs:
        selected = next((r for r in runs if r.id == run), runs[0])

    context = await shell_context(request, session, language, page="archive", run=selected)
    context.update(
        {
            "runs": runs,
            "selected": selected,
            "stories": (
                await queries.stories_for_run(
                    session, selected, language=language, ranked_only=not all
                )
                if selected
                else []
            ),
        }
    )
    response = get_templates(request).TemplateResponse(request, "archive.html", context)
    remember_preferences(request, response, language)
    return response


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    language = language_of(request)
    context = await shell_context(request, session, language, page="search")
    context.update(
        {
            "query": q,
            "stories": await queries.search_stories(session, q, language) if q.strip() else [],
        }
    )
    response = get_templates(request).TemplateResponse(request, "search.html", context)
    remember_preferences(request, response, language)
    return response


@router.post("/verdict", response_class=HTMLResponse)
async def post_verdict(
    request: Request,
    summary_id: int = Form(...),
    verdict: str = Form(...),
    note: str | None = Form(None),
    frag: str | None = Form(None),
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    """The reader's call on one summary (PLAN-EVALS E2.3).

    Answers with the story's re-rendered foot line, not a redirect: a verdict
    is given mid-read, and a page reload would throw the reader back to the
    top of a list they were halfway down. One row per summary - a second
    verdict overwrites the first - and the note is kept only with "wrong",
    because "right, and here is why" is not a thing a reader writes.

    `frag=words` answers with the two words alone. That is what the findings
    table on `/runs/verdicts` swaps: the control sits in a table cell there,
    and a story foot - chips, source link, note line - swapped into a cell
    would be a story card inside a table.
    """
    language = language_of(request)
    if verdict not in ("ok", "wrong"):
        raise HTTPException(status_code=422, detail="verdict is 'ok' or 'wrong'")
    if await session.get(Summary, summary_id) is None:
        raise HTTPException(status_code=404, detail="no such summary")

    row = (
        await session.execute(select(Verdict).where(Verdict.summary_id == summary_id))
    ).scalar_one_or_none()
    # Three cases, not two. "right" drops the note, because "right, and here is
    # why" is not a thing a reader writes. A submitted note - even an empty one -
    # is what the reader now wants. But the two words post no `note` field at
    # all, so a reader pressing "wrong" a second time on a story they have
    # already explained used to silently erase the explanation, which is the one
    # row E5 rewrites the judge prompt from.
    if verdict != "wrong":
        cleaned = None
    elif note is None:
        cleaned = row.note if row is not None else None
    else:
        cleaned = note.strip() or None
    if row is None:
        session.add(Verdict(summary_id=summary_id, verdict=verdict, note=cleaned))
    else:
        row.verdict = verdict
        row.note = cleaned
        row.created_at = utcnow()
    await session.commit()

    story = await queries.story_for_summary(session, summary_id, language)
    if frag == "words":
        return get_templates(request).TemplateResponse(
            request,
            "_verdict_words.html",
            {
                "sid": summary_id,
                "verdict": story.verdict if story else None,
                "language": language,
                "t": strings(language),
                "target": f"#vd-{summary_id}",
                "frag": "words",
            },
        )
    return get_templates(request).TemplateResponse(
        request,
        "_story_foot.html",
        {"story": story, "language": language, "t": strings(language), "saved": True},
    )
