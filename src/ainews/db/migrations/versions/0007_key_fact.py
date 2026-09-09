"""The summariser records the one thing that makes a story news.

Every check the evaluation layer had measured shape: word budgets, sentence
counts, tag singleton share, importance distribution, note paragraph lengths. A
regression to three generic, numberless sentences at importance 3 passes all of
them, and passes the numeral check (no figure to flag) and the grounding judge
(no claim to disagree with) as well. The 2026-09-06 run was already 70% threes.

`key_fact` is a field on an answer the run is already buying, so it costs no
extra call, and `checks.content_floor` measures whether it survives into the
summary at all.

Existing rows are left NULL. It is a question nobody asked of those articles and
inferring it from the summary would measure the check against its own input.

Revision ID: 0007_key_fact
Revises: 0006_verdict_reason
Created: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_key_fact"
down_revision: str | None = "0006_verdict_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("summaries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("key_fact", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("summaries", schema=None) as batch_op:
        batch_op.drop_column("key_fact")
