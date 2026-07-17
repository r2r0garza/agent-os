"""MCP discovery and health evidence

Revision ID: 2e0446f14592
Revises: eb42c1a79d03
Create Date: 2026-07-17 21:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "2e0446f14592"
down_revision: Union[str, Sequence[str], None] = "eb42c1a79d03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_server_tools",
        sa.Column("mcp_server_version_id", sa.UUID(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "input_schema", postgresql.JSONB(), server_default="{}", nullable=False
        ),
        sa.Column(
            "schema_valid", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.Column(
            "schema_validation_errors",
            postgresql.JSONB(),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("descriptor_hash", sa.Text(), nullable=False),
        sa.Column(
            "credential_scope_required",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("timeout_ms", sa.Integer(), nullable=True),
        sa.Column("output_limit_bytes", sa.Integer(), nullable=True),
        sa.Column(
            "last_discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["mcp_server_version_id"],
            ["mcp_server_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mcp_server_version_id",
            "tool_name",
            name=op.f("uq_mcp_server_tools_version_tool"),
        ),
    )

    op.create_table(
        "mcp_server_health_checks",
        sa.Column("mcp_server_version_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tool_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "request_metadata", postgresql.JSONB(), server_default="{}", nullable=False
        ),
        sa.Column(
            "diagnostics", postgresql.JSONB(), server_default="[]", nullable=False
        ),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("triggered_by", sa.UUID(), nullable=False),
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
            "status IN ('healthy', 'degraded', 'unreachable', 'malformed')",
            name=op.f("ck_mcp_server_health_checks_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["mcp_server_version_id"],
            ["mcp_server_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["triggered_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mcp_server_health_checks_mcp_server_version_id"),
        "mcp_server_health_checks",
        ["mcp_server_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_mcp_server_health_checks_mcp_server_version_id"),
        table_name="mcp_server_health_checks",
    )
    op.drop_table("mcp_server_health_checks")
    op.drop_table("mcp_server_tools")
