"""The article and the web context stop being one column.

`enrich` appended Tavily's snippets onto `body_text` with two newlines and no
marker, so the field meant "the article, plus whatever a search for its title
returned that week". Two readers take it to mean the article: the grounding
judge, which asks whether a summary is supported by "the text the summariser was
shown", and the fixture recorder, which stores that text's numerals as the
grounding floor. A claim that came from a wrong snippet was therefore judged
grounded (ADR 0029).

`extra_text` holds the search text from here on. `body_source` says where
`body_text` came from.

Rows written before this revision are set to `unknown`, which is the truthful
value rather than a placeholder: their bodies may or may not carry appended
search text, the two were joined by a separator that appears in ordinary prose,
and nothing can now tell them apart. Anything that wants to claim a body is
article text has to check, and for history the answer is no.

Revision ID: 0003_provenance
Revises: 0002_retire_manual
Created: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_provenance"
down_revision: str | None = "0002_retire_manual"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("extra_text", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("body_source", sa.String(length=10), nullable=True))

    op.execute(
        "UPDATE articles SET body_source = 'unknown'"
        " WHERE body_text IS NOT NULL AND body_text != ''"
    )


def downgrade() -> None:
    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.drop_column("body_source")
        batch_op.drop_column("extra_text")
