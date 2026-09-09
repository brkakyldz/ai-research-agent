"""Retire the `manual` run kind on archives that still carry it.

ADR 0026 reduced `Run.kind` to `collect | digest`. `manual` had meant "a person
pressed the button" against `digest` meaning "the scheduler fired", and ADR 0015
took the clock off, so every run written after it was `manual` and no `digest`
row was ever produced again - which made the rail's archive badge count one
above a listing of three.

The rewrite shipped as an entry in `DATA_FIXUPS`, a list applied on every start,
because there was no migration tool. There is one now, and this is where it
belongs: applied once, recorded, and not re-executed on every boot for the rest
of the project's life.

Idempotent by construction, and a no-op on any database created after ADR 0026,
which is every database a new installation will ever have.

Revision ID: 0002_retire_manual
Revises: 0001_baseline
Created: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_retire_manual"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE runs SET kind = 'digest' WHERE kind = 'manual'")


def downgrade() -> None:
    # The distinction this column carried is gone, not merely unwritten: which
    # rows were pressed and which were scheduled is not recoverable from
    # anything else on the row. Rewriting them all back to `manual` would invent
    # a history, so the downgrade restores the schema's tolerance and leaves the
    # data alone. `runs.kind` has no constraint change to reverse - SQLite baked
    # the CHECK into the table at creation - so there is nothing else to undo.
    pass
