"""Two prompts over the same bodies: `ainews eval compare`.

The one instrument a person who did not write this application needs before they
can improve a prompt. Without it, changing `summarize.md` means pressing the
button, paying for a run, reading fifteen summaries and forming an impression -
and the day's news is different from yesterday's, so the impression is about the
news as much as about the prompt.

A **frozen corpus** removes that. `ainews eval corpus` writes thirty to fifty
article bodies out of the local archive into a JSONL file; `compare` summarises
every one of them twice, once with each prompt, and runs the free checks over
both sets. The bodies are the same, the model is the same, the seed is the same,
so the difference is the prompt.

The corpus file is **gitignored**. It carries whole article bodies, which is
someone else's text, and the repository is public - the same reason the fixtures
carry a body's numerals and never the body (ADR 0019 §3).

Nothing here writes to `eval_results`. Those rows are measurements of a published
bulletin, and this measures a prompt that has not shipped.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import PROJECT_ROOT, Settings, get_settings
from ainews.db import Article, Source
from ainews.pipeline.llm import summarizer, usage_from_message
from ainews.pipeline.nodes.summarize import MAX_BODY_CHARS, build_prompt
from ainews.pipeline.pricing import estimate_cost, resolve_model
from ainews.pipeline.prompts import load_prompt
from ainews.pipeline.state import ArticleSummary
from ainews.quality import checks
from ainews.quality.stories import capitalised_tokens

log = logging.getLogger(__name__)

CORPUS_PATH = PROJECT_ROOT / "data" / "corpus" / "summarize.jsonl"
DEFAULT_SIZE = 40
# Roughly what one summarise call answers with, for the estimate before the
# first one is made. The judge's guard uses the same shape.
OUTPUT_TOKENS_GUESS = 220
CHARS_PER_TOKEN = 4


class CostGuard(RuntimeError):
    """The estimate is above `--max-cost`. Raised before any call is made."""


@dataclass(slots=True)
class CorpusItem:
    """One article as the summariser would be shown it, and nothing else.

    Frozen at the moment the corpus was cut. An article's body can be repaired
    later (`ainews repair`), and a corpus that re-read the database would make
    two comparisons a week apart incomparable for a reason neither of them is
    about.
    """

    article_id: int
    source: str
    title: str
    url: str
    published: str
    body: str
    extra: str = ""


@dataclass(slots=True)
class Side:
    """One prompt's answers over the corpus, and what they cost."""

    name: str
    prompt_version: str
    stories: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    est_cost_usd: float = 0.0


@dataclass(slots=True)
class Comparison:
    model: str
    language: str
    n_items: int
    a: Side
    b: Side


# -- the corpus ---------------------------------------------------------------


async def freeze_corpus(
    session: AsyncSession, *, size: int = DEFAULT_SIZE, seed: int = 0
) -> list[CorpusItem]:
    """`size` articles with real bodies, sampled from the archive.

    Only articles whose body came from the feed or a fetch (ADR 0029): an
    `unknown` body may be the article with a week's search results appended, and
    a prompt comparison run over one of those would be comparing two readings of
    a text neither prompt was written for.

    Ordered by a seeded hash of the id rather than by date, so the corpus is a
    cross-section of the archive and not last Tuesday.
    """
    rows = (
        await session.execute(
            select(Article, Source.name)
            .join(Source, Source.id == Article.source_id)
            .where(Article.body_text.isnot(None))
            .where(func.length(Article.body_text) > 400)
            .where(Article.body_source.in_(("feed", "fetch")))
            .where(Article.dup_of.is_(None))
            # Knuth's multiplicative hash, in SQL, with the seed in the
            # multiplier. A deterministic scatter, so the same seed cuts the
            # same corpus and two comparisons a week apart differ by the prompt
            # rather than by the sample. The seed cannot be an *offset*: adding
            # a constant to every key leaves the order it produces unchanged.
            .order_by((Article.id * (2654435761 + seed * 104729)) % 1000003, Article.id)
            .limit(size)
        )
    ).all()
    return [
        CorpusItem(
            article_id=article.id,
            source=source_name,
            title=article.title,
            url=article.url,
            published=article.published_at.isoformat() if article.published_at else "unknown",
            body=(article.body_text or "")[:MAX_BODY_CHARS],
            extra=(article.extra_text or "")[:2000],
        )
        for article, source_name in rows
    ]


def write_corpus(items: list[CorpusItem], path: Path | None = None) -> Path:
    path = path or CORPUS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
    return path


def read_corpus(path: Path | None = None) -> list[CorpusItem]:
    path = path or CORPUS_PATH
    if not path.exists():
        raise LookupError(f"no corpus at {path}; run `ainews eval corpus` first")
    items = [
        CorpusItem(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not items:
        raise LookupError(f"the corpus at {path} is empty")
    return items


# -- the comparison -----------------------------------------------------------


def load_side(name: str, prompt_path: Path | None, language: str) -> tuple[str, str]:
    """`(template, version)` for one side. `None` means the prompt on disk."""
    import hashlib

    template = (
        load_prompt("summarize", language)
        if prompt_path is None
        else prompt_path.read_text(encoding="utf-8").strip()
    )
    return template, hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]


def estimate(items: list[CorpusItem], template: str, language: str, model: str) -> float:
    tokens_in = sum(
        len(
            build_prompt(
                language,
                source=item.source,
                title=item.title,
                url=item.url,
                published=item.published,
                body=item.body,
                extra=item.extra,
                template=template,
            )
        )
        // CHARS_PER_TOKEN
        for item in items
    )
    return estimate_cost(model, tokens_in, OUTPUT_TOKENS_GUESS * len(items))


async def _one(
    item: CorpusItem, template: str, language: str, model: str, settings: Settings
) -> tuple[dict[str, Any] | None, str, int, int]:
    prompt = build_prompt(
        language,
        source=item.source,
        title=item.title,
        url=item.url,
        published=item.published,
        body=item.body,
        extra=item.extra,
        template=template,
    )
    try:
        client = summarizer(settings, model).with_structured_output(
            ArticleSummary, include_raw=True
        )
        response = await client.ainvoke(prompt)
    except Exception as exc:
        return None, f"article {item.article_id}: {type(exc).__name__}: {exc}"[:200], 0, 0

    parsed = response.get("parsed") if isinstance(response, dict) else None
    usage = usage_from_message(response.get("raw") if isinstance(response, dict) else None)
    if parsed is None:
        return (
            None,
            f"article {item.article_id}: unparsable output",
            usage.tokens_in,
            usage.tokens_out,
        )

    # The same dict shape `quality.stories` builds, so the same checks run over
    # it. `weight` and the editor's fields are absent because a prompt
    # comparison has no bulletin in it: nothing was ranked and nothing was
    # published, and the checks that read those simply have nothing to say.
    story = {
        "article_id": item.article_id,
        "source": item.source,
        "weight": 1.0,
        "title": item.title,
        "title_local": parsed.title_local,
        "summary": parsed.summary,
        "why_it_matters": parsed.why_it_matters,
        "tags": parsed.tags,
        "importance": parsed.importance,
        "key_fact": parsed.key_fact,
        "relevant": parsed.relevant,
        "kind": parsed.kind,
        "position": None,
        "tier": None,
        "reason": None,
        "body_numerals": sorted(checks.numeral_values(item.body)),
        "body_capitalised": capitalised_tokens(item.body),
        "body_source": "corpus",
    }
    return story, "", usage.tokens_in, usage.tokens_out


async def run_side(
    items: list[CorpusItem],
    *,
    name: str,
    template: str,
    version: str,
    language: str,
    model: str,
    settings: Settings,
    concurrency: int,
) -> Side:
    side = Side(name=name, prompt_version=version)
    gate = asyncio.Semaphore(concurrency)

    async def one(item: CorpusItem) -> tuple[dict[str, Any] | None, str, int, int]:
        async with gate:
            return await _one(item, template, language, model, settings)

    for story, failure, tin, tout in await asyncio.gather(*(one(item) for item in items)):
        side.tokens_in += tin
        side.tokens_out += tout
        if story is None:
            side.failures.append(failure)
        else:
            side.stories.append(story)
    side.est_cost_usd = estimate_cost(model, side.tokens_in, side.tokens_out)
    return side


async def compare(
    items: list[CorpusItem],
    *,
    prompt_a: Path | None,
    prompt_b: Path,
    language: str = "tr",
    model: str | None = None,
    max_cost: float = 0.10,
    settings: Settings | None = None,
) -> Comparison:
    """Both prompts over every body, then the free checks over both answers.

    `prompt_a` defaults to the prompt on disk, because the question is nearly
    always "is this candidate better than what ships".
    """
    settings = settings or get_settings()
    model = resolve_model(model, settings.openai_model_summarize)
    template_a, version_a = load_side("a", prompt_a, language)
    template_b, version_b = load_side("b", prompt_b, language)

    total = estimate(items, template_a, language, model) + estimate(
        items, template_b, language, model
    )
    if total > max_cost:
        raise CostGuard(
            f"comparing {len(items)} bodies on {model} twice is about ${total:.4f}, "
            f"above --max-cost ${max_cost:.4f}"
        )

    side_a = await run_side(
        items,
        name=str(prompt_a) if prompt_a else f"{language}/summarize.md (shipped)",
        template=template_a,
        version=version_a,
        language=language,
        model=model,
        settings=settings,
        concurrency=settings.summarize_batch_size,
    )
    side_b = await run_side(
        items,
        name=str(prompt_b),
        template=template_b,
        version=version_b,
        language=language,
        model=model,
        settings=settings,
        concurrency=settings.summarize_batch_size,
    )
    return Comparison(model=model, language=language, n_items=len(items), a=side_a, b=side_b)


# -- rendering ----------------------------------------------------------------


def _row(label: str, left: str, right: str) -> str:
    return f"{label:<38} {left:>14}  {right:>14}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def format_comparison(result: Comparison) -> str:
    """Side by side, with A first, and no verdict.

    No winner is printed. The measures disagree often - a prompt that names more
    figures also breaks the word budget more - and a command that picked for the
    reader would be hiding exactly the trade it was built to show.
    """
    lines = [
        f"{result.n_items} bodies · {result.language} · {result.model}",
        "",
        _row("", "A", "B"),
        _row("prompt", result.a.prompt_version, result.b.prompt_version),
        "-" * 70,
    ]
    for side, label in ((result.a, "A"), (result.b, "B")):
        if side.failures:
            lines.append(f"{label}: {len(side.failures)} call(s) failed, not counted")

    floor_a = checks.content_floor(result.a.stories)
    floor_b = checks.content_floor(result.b.stories)
    budget_a = checks.word_budget(result.a.stories)
    budget_b = checks.word_budget(result.b.stories)
    tags_a = checks.tag_vocabulary(result.a.stories)
    tags_b = checks.tag_vocabulary(result.b.stories)
    imp_a = checks.importance_distribution(result.a.stories)
    imp_b = checks.importance_distribution(result.b.stories)

    lines += [
        _row("summarised", str(len(result.a.stories)), str(len(result.b.stories))),
        _row(
            "named a key fact",
            _pct(floor_a["key_fact_share"]),
            _pct(floor_b["key_fact_share"]),
        ),
        _row(
            "kept it in the writing",
            _pct(floor_a["key_fact_kept"]),
            _pct(floor_b["key_fact_kept"]),
        ),
        _row(
            "a body figure reached the summary",
            _pct(floor_a["numeral_recall"]),
            _pct(floor_b["numeral_recall"]),
        ),
        _row(
            "invented a figure",
            str(len(checks.ungrounded_numerals(result.a.stories))),
            str(len(checks.ungrounded_numerals(result.b.stories))),
        ),
        _row(
            "over the 55-word budget",
            _pct(budget_a["over_share"]["summary"]),
            _pct(budget_b["over_share"]["summary"]),
        ),
        _row(
            "tags in the vocabulary",
            _pct(tags_a["in_vocabulary_share"]),
            _pct(tags_b["in_vocabulary_share"]),
        ),
        _row("tag singletons", _pct(tags_a["singleton_share"]), _pct(tags_b["singleton_share"])),
        _row(
            "importance 4 or 5",
            _pct(imp_a[4] + imp_a[5]),
            _pct(imp_b[4] + imp_b[5]),
        ),
        _row(
            "called it irrelevant",
            str(sum(1 for s in result.a.stories if s["relevant"] is False)),
            str(sum(1 for s in result.b.stories if s["relevant"] is False)),
        ),
        "-" * 70,
        _row("spent", f"${result.a.est_cost_usd:.4f}", f"${result.b.est_cost_usd:.4f}"),
    ]
    return "\n".join(lines)
