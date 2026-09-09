"""A bulletin carries the checks that were run on it.

The deterministic checks cost nothing and ran only when somebody remembered
`ainews eval report` over a freshly recorded fixture — so the person running the
application, who is the person the evaluation is for, never saw a single one of
them. The press computes them now and stores them here.

Stored rather than recomputed on demand: a number is a claim about the prompts
and the check code that produced it, and both move. `eval_results.prompt_version`
exists for the same reason.

Existing bulletins are left NULL. The report recomputes for those, and says so.

Revision ID: 0008_published_checks
Revises: 0007_key_fact
Created: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_published_checks"
down_revision: str | None = "0007_key_fact"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("bulletins", schema=None) as batch_op:
        batch_op.add_column(sa.Column("checks_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("bulletins", schema=None) as batch_op:
        batch_op.drop_column("checks_json")
