"""model profile probe evidence

Revision ID: b5e2c7d9a401
Revises: a3f0c9d1e5b2
Create Date: 2026-07-17 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b5e2c7d9a401"
down_revision: Union[str, Sequence[str], None] = "a3f0c9d1e5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_profile_probes",
        sa.Column("model_profile_version_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "capability_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "pricing_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "request_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "diagnostics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'degraded', 'failed')",
            name="ck_model_profile_probes_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["model_profile_version_id"],
            ["model_profile_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_model_profile_probes_model_profile_version_id"),
        "model_profile_probes",
        ["model_profile_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_model_profile_probes_model_profile_version_id"),
        table_name="model_profile_probes",
    )
    op.drop_table("model_profile_probes")
