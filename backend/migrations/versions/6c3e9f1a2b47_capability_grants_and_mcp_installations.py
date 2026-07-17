"""capability grants and MCP installations

Revision ID: 6c3e9f1a2b47
Revises: 2e0446f14592
Create Date: 2026-07-17 22:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6c3e9f1a2b47"
down_revision: Union[str, Sequence[str], None] = "2e0446f14592"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_version_skills",
        sa.Column("granted_by", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_agent_version_skills_granted_by_users"),
        "agent_version_skills",
        "users",
        ["granted_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "agent_version_mcp_servers",
        sa.Column("granted_by", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_agent_version_mcp_servers_granted_by_users"),
        "agent_version_mcp_servers",
        "users",
        ["granted_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "mcp_server_installations",
        sa.Column("installed_mcp_server_id", sa.UUID(), nullable=False),
        sa.Column("source_mcp_server_version_id", sa.UUID(), nullable=False),
        sa.Column("installed_by", sa.UUID(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["installed_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["installed_mcp_server_id"], ["mcp_servers.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_mcp_server_version_id"],
            ["mcp_server_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "installed_mcp_server_id",
            name=op.f("uq_mcp_server_installations_installed_server"),
        ),
    )


def downgrade() -> None:
    op.drop_table("mcp_server_installations")
    op.drop_constraint(
        op.f("fk_agent_version_mcp_servers_granted_by_users"),
        "agent_version_mcp_servers",
        type_="foreignkey",
    )
    op.drop_column("agent_version_mcp_servers", "granted_by")
    op.drop_constraint(
        op.f("fk_agent_version_skills_granted_by_users"),
        "agent_version_skills",
        type_="foreignkey",
    )
    op.drop_column("agent_version_skills", "granted_by")
