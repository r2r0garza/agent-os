"""artifact ingestion metadata

Revision ID: e4a1c8e91f2b
Revises: dab0966bea79
Create Date: 2026-07-15 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e4a1c8e91f2b"
down_revision: Union[str, Sequence[str], None] = "dab0966bea79"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column(
            "ingestion_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column("artifacts", sa.Column("ingestion_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("artifacts", "ingestion_error")
    op.drop_column("artifacts", "ingestion_metadata")
