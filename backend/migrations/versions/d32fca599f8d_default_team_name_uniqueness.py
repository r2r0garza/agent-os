"""default team name uniqueness

Revision ID: d32fca599f8d
Revises: c4f8a1d2e9b7
Create Date: 2026-07-17 00:09:53.137433

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd32fca599f8d'
down_revision: Union[str, Sequence[str], None] = 'c4f8a1d2e9b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint(op.f("uq_teams_name"), "teams", ["name"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("uq_teams_name"), "teams", type_="unique")
