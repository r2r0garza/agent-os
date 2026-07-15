"""task agent assignment evidence

Revision ID: 2c4b8f91a61d
Revises: 79ce63a422af
Create Date: 2026-07-15 15:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "2c4b8f91a61d"
down_revision: Union[str, Sequence[str], None] = "79ce63a422af"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("assignment_status", sa.Text(), server_default="unassigned", nullable=False))
    op.add_column(
        "tasks",
        sa.Column("assignment_candidates", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
    )
    op.add_column(
        "tasks",
        sa.Column("assignment_rationale", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.add_column("tasks", sa.Column("assignment_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "assignment_updated_at")
    op.drop_column("tasks", "assignment_rationale")
    op.drop_column("tasks", "assignment_candidates")
    op.drop_column("tasks", "assignment_status")
