"""artifact blob storage state and reconciliation metadata

Revision ID: c31db6e4a92f
Revises: 517db784fde1
Create Date: 2026-07-15 18:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c31db6e4a92f"
down_revision: Union[str, Sequence[str], None] = "517db784fde1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

artifact_storage_state = postgresql.ENUM(
    "staged", "finalized", "missing", "orphaned", name="artifact_storage_state", create_type=False
)


def upgrade() -> None:
    artifact_storage_state.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "artifact_blobs",
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_ref", sa.Text(), nullable=True),
        sa.Column("state", artifact_storage_state, server_default="staged", nullable=False),
        sa.Column("staged_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reconciliation_details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("size_bytes >= 0", name="artifact_blob_size_non_negative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash", name="uq_artifact_blobs_content_hash"),
    )
    op.add_column("artifact_versions", sa.Column("blob_id", sa.UUID(), nullable=True))
    op.add_column(
        "artifact_versions", sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column(
        "artifact_versions",
        sa.Column("storage_state", artifact_storage_state, server_default="missing", nullable=False),
    )
    op.create_foreign_key(
        "fk_artifact_versions_blob_id_artifact_blobs",
        "artifact_versions",
        "artifact_blobs",
        ["blob_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_artifact_versions_blob_id_artifact_blobs", "artifact_versions", type_="foreignkey")
    op.drop_column("artifact_versions", "storage_state")
    op.drop_column("artifact_versions", "size_bytes")
    op.drop_column("artifact_versions", "blob_id")
    op.drop_table("artifact_blobs")
    artifact_storage_state.drop(op.get_bind(), checkfirst=True)
