"""A reader's "wrong" says what was wrong with it.

One word covered four separate claims — the facts, that this is AI news at all,
that it is not the story above it again, and the size the editor gave it — and
`judge.calibrate` read every one of them as a grounding failure. A reader
marking a correct summary of an irrelevant story counted against a judge that
had reported nothing wrong and was right.

Existing labels are left NULL. Nobody was asked which of the four they meant and
the answer is not recoverable from a free-text note; they are set aside from the
grounding rate rather than assigned a reason on their behalf.

Revision ID: 0006_verdict_reason
Revises: 0005_prompt_version
Created: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_verdict_reason"
down_revision: str | None = "0005_prompt_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Named here as well as on the model so the constraint the database carries and
# the constraint the code enforces are one list read twice, not two lists.
REASONS = ("wrong_fact", "not_news", "duplicate", "wrong_place")


def upgrade() -> None:
    with op.batch_alter_table("verdicts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reason", sa.String(length=12), nullable=True))
        batch_op.create_check_constraint(
            "ck_verdicts_reason",
            "reason is null or reason in ('{}')".format("', '".join(REASONS)),
        )


def downgrade() -> None:
    with op.batch_alter_table("verdicts", schema=None) as batch_op:
        batch_op.drop_constraint("ck_verdicts_reason", type_="check")
        batch_op.drop_column("reason")
