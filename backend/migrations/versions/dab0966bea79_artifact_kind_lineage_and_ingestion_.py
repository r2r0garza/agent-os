"""artifact kind lineage and ingestion status

Revision ID: dab0966bea79
Revises: c31db6e4a92f
Create Date: 2026-07-15 19:12:36.276391

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'dab0966bea79'
down_revision: Union[str, Sequence[str], None] = 'c31db6e4a92f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

artifact_kind = postgresql.ENUM("source", "normalized", "output", name="artifact_kind", create_type=False)
artifact_ingestion_status = postgresql.ENUM(
    "not_applicable", "pending", "complete", "failed", "unsupported", "needs_reconciliation",
    name="artifact_ingestion_status", create_type=False,
)


def upgrade() -> None:
    artifact_kind.create(op.get_bind(), checkfirst=True)
    artifact_ingestion_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "artifacts",
        sa.Column("parent_artifact_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "artifacts",
        sa.Column("kind", artifact_kind, server_default="source", nullable=False),
    )
    op.add_column("artifacts", sa.Column("content_type", sa.Text(), nullable=True))
    op.add_column(
        "artifacts",
        sa.Column(
            "ingestion_status", artifact_ingestion_status, server_default="not_applicable", nullable=False
        ),
    )
    op.create_foreign_key(
        "fk_artifacts_parent_artifact_id_artifacts",
        "artifacts",
        "artifacts",
        ["parent_artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_artifacts_parent_artifact_id_artifacts", "artifacts", type_="foreignkey")
    op.drop_column("artifacts", "ingestion_status")
    op.drop_column("artifacts", "content_type")
    op.drop_column("artifacts", "kind")
    op.drop_column("artifacts", "parent_artifact_id")
    artifact_ingestion_status.drop(op.get_bind(), checkfirst=True)
    artifact_kind.drop(op.get_bind(), checkfirst=True)
