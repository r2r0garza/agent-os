"""versioned skill packages

Revision ID: eb42c1a79d03
Revises: b5e2c7d9a401
Create Date: 2026-07-17 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "eb42c1a79d03"
down_revision: Union[str, Sequence[str], None] = "b5e2c7d9a401"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "skill_versions",
        sa.Column("package_manifest", postgresql.JSONB(), server_default="{}", nullable=False),
    )
    op.add_column("skill_versions", sa.Column("instructions", sa.Text(), nullable=True))
    op.add_column(
        "skill_versions",
        sa.Column("resources", postgresql.JSONB(), server_default="[]", nullable=False),
    )
    op.add_column(
        "skill_versions",
        sa.Column("declared_capabilities", postgresql.JSONB(), server_default="[]", nullable=False),
    )
    op.add_column(
        "skill_versions",
        sa.Column("provenance", postgresql.JSONB(), server_default="{}", nullable=False),
    )
    op.add_column("skill_versions", sa.Column("package_hash", sa.Text(), nullable=True))
    op.add_column(
        "skill_versions",
        sa.Column("validation_status", sa.Text(), server_default="legacy", nullable=False),
    )
    op.add_column(
        "skill_versions",
        sa.Column("validation_diagnostics", postgresql.JSONB(), server_default="[]", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_skill_versions_valid_validation_status"),
        "skill_versions",
        "validation_status IN ('legacy', 'valid')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_skill_versions_valid_validation_status"),
        "skill_versions",
        type_="check",
    )
    for column in (
        "validation_diagnostics",
        "validation_status",
        "package_hash",
        "provenance",
        "declared_capabilities",
        "resources",
        "instructions",
        "package_manifest",
    ):
        op.drop_column("skill_versions", column)
