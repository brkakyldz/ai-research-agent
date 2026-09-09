"""A measurement records which prompt produced it.

The judge prompt failed 8 of 12 on 2026-09-06, was revised the same afternoon,
and the revision was scored on the same 12. The 83-92% on record is therefore a
training-set score, and nothing on the row said which of the two prompts it came
from — so a later reader averaging every `grounding` row would average two
different experiments.

Existing rows are left NULL rather than backfilled with today's hash. The prompt
files have changed since those rows were written and their real version is not
recoverable; `unknown` is the truthful value, the same answer 0003 gave for
`body_source`.

Revision ID: 0005_prompt_version
Revises: 0004_two_objects
Created: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_prompt_version"
down_revision: str | None = "0004_two_objects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("eval_results", schema=None) as batch_op:
        batch_op.add_column(sa.Column("prompt_version", sa.String(length=12), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("eval_results", schema=None) as batch_op:
        batch_op.drop_column("prompt_version")
