"""Weight is editable, and editing it cannot lose a value.

`weight` breaks ranking ties, decides which thin article is worth a Tavily
credit, and since ADR 0025 picks the survivor of a duplicate cluster. It was
documented as a range 0.5-2.0, constrained only above zero, and settable from
nowhere in the running app: `add_source` wrote 1.0 and that was the end of it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.db import Source

# The seed's own vocabulary, which is tenths. The control has to speak it.
SEEDED = {"OpenAI": 2.0, "Hugging Face": 1.6, "Ben's Bites": 0.9, "LangChain": 1.2}


@pytest.fixture
async def feeds(session: AsyncSession) -> dict[str, int]:
    for name, weight in SEEDED.items():
        session.add(Source(name=name, url=f"https://example.com/{name.lower()}.xml", weight=weight))
    await session.commit()
    rows = (await session.execute(select(Source))).scalars()
    return {s.name: s.id for s in rows}


def _post(client: TestClient, fields: dict[str, str]):
    return client.post("/sources/weights", data={"lang": "tr", **fields}, follow_redirects=True)


async def test_the_page_draws_every_weight_it_holds(
    client: TestClient, engine: AsyncEngine, feeds: dict[str, int]
) -> None:
    """The bug this file exists for: the control was four fixed options and the
    seed tunes in tenths, so nine of sixteen rows had no matching value. A
    `select` with no matching option does not refuse - it submits its first one,
    and one save wrote 0.5 over every weight the seed had set."""
    body = client.get("/sources").text
    for name, weight in SEEDED.items():
        assert f'value="{weight:.1f}"' in body, f"{name} at {weight} has no field showing it"


async def test_a_save_that_touches_nothing_changes_nothing(
    client: TestClient, engine: AsyncEngine, session: AsyncSession, feeds: dict[str, int]
) -> None:
    """Every row is in the submit whether or not it was touched, so the round
    trip has to be an identity when nobody typed."""
    said = _post(client, {f"w_{i}": f"{SEEDED[n]:.1f}" for n, i in feeds.items()}).text
    assert "değişmedi" in said

    session.expire_all()
    kept = {s.name: s.weight for s in (await session.execute(select(Source))).scalars()}
    assert kept == SEEDED


async def test_one_weight_moves_and_the_rest_stay(
    client: TestClient, engine: AsyncEngine, session: AsyncSession, feeds: dict[str, int]
) -> None:
    fields = {f"w_{i}": f"{SEEDED[n]:.1f}" for n, i in feeds.items()}
    fields[f"w_{feeds['LangChain']}"] = "1.7"
    assert "1 ağırlık kaydedildi" in _post(client, fields).text

    session.expire_all()
    kept = {s.name: s.weight for s in (await session.execute(select(Source))).scalars()}
    assert kept == {**SEEDED, "LangChain": 1.7}


@pytest.mark.parametrize("bad", ["", "abc", "0.4", "2.1", "20", "-1"])
async def test_a_value_outside_the_range_leaves_its_row_alone(
    client: TestClient, engine: AsyncEngine, session: AsyncSession, feeds: dict[str, int], bad: str
) -> None:
    """Skipped rather than refused. This is a bulk submit: refusing the whole
    form because one field would not parse would quietly discard the other
    fifteen edits, and accepting 20.0 would put one feed an order of magnitude
    above every other in a tie-break nobody would think to look at."""
    assert _post(client, {f"w_{feeds['OpenAI']}": bad}).status_code == 200
    session.expire_all()
    openai = await session.get(Source, feeds["OpenAI"])
    assert openai.weight == 2.0


async def test_a_field_naming_no_source_is_ignored(
    client: TestClient, engine: AsyncEngine, session: AsyncSession, feeds: dict[str, int]
) -> None:
    assert _post(client, {"w_99999": "1.5", "lang": "tr"}).status_code == 200
    session.expire_all()
    assert len((await session.execute(select(Source))).scalars().all()) == len(SEEDED)


async def test_the_column_is_one_form_with_one_save(
    client: TestClient, engine: AsyncEngine, feeds: dict[str, int]
) -> None:
    """One submit for the whole column: weights are relative, so the question is
    which of these feeds leads. The fields are inside the table and the form is
    not, because each row already carries the toggle's form and a form cannot
    contain another - `form="weights"` is what joins them."""
    body = client.get("/sources").text
    assert body.count('id="weights"') == 1
    assert body.count('form="weights"') == len(SEEDED) + 2  # a field each, the lang, the save
    assert body.count("/sources/weights") == 1


async def test_the_range_the_form_offers_is_the_range_the_column_allows(
    session: AsyncSession, engine: AsyncEngine, feeds: dict[str, int]
) -> None:
    """`weight > 0` alone let a typo store 20.0. The CHECK now says the same
    thing the form's `min` and `max` do."""
    from sqlalchemy.exc import IntegrityError

    session.add(Source(name="Loud", url="https://example.com/loud.xml", weight=20.0))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
