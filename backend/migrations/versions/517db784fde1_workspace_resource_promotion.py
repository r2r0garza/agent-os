"""workspace resource leases and conflict-aware promotion

Revision ID: 517db784fde1
Revises: 2c4b8f91a61d
Create Date: 2026-07-15 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "517db784fde1"
down_revision: Union[str, Sequence[str], None] = "2c4b8f91a61d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspace_resources",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("resource_key", sa.Text(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_fencing_token", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("last_fencing_token >= 0", name="workspace_resource_fencing_non_negative"),
        sa.CheckConstraint("revision >= 0", name="workspace_resource_revision_non_negative"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "resource_key", name="uq_workspace_resources_project_key"),
    )
    op.create_table(
        "workspace_resource_leases",
        sa.Column("resource_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("task_lease_token", sa.BigInteger(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("expected_revision", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("expected_revision >= 0", name="workspace_resource_lease_revision_non_negative"),
        sa.CheckConstraint("fencing_token > 0", name="workspace_resource_lease_fencing_positive"),
        sa.ForeignKeyConstraint(["resource_id"], ["workspace_resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resource_id", name="uq_workspace_resource_leases_resource"),
    )
    op.create_table(
        "workspace_promotions",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expected_revisions", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("resulting_revisions", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("conflict_details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status in ('promoted', 'conflict', 'denied')", name="workspace_promotion_status_valid"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_workspace_promotions_run"),
    )


def downgrade() -> None:
    op.drop_table("workspace_promotions")
    op.drop_table("workspace_resource_leases")
    op.drop_table("workspace_resources")
