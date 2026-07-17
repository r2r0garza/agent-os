"""audit events event type occurred at index

Revision ID: a3f0c9d1e5b2
Revises: d32fca599f8d
Create Date: 2026-07-17 08:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f0c9d1e5b2'
down_revision: Union[str, Sequence[str], None] = 'd32fca599f8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        op.f("ix_audit_events_event_type_occurred_at"),
        "audit_events",
        ["event_type", "occurred_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_audit_events_event_type_occurred_at"), table_name="audit_events")
