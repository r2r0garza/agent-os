"""scoped MCP credential attachments

Revision ID: b81d7e4f2a90
Revises: 54b0f3ff0b78
Create Date: 2026-07-16 21:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b81d7e4f2a90"
down_revision: Union[str, Sequence[str], None] = "54b0f3ff0b78"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


visibility = postgresql.ENUM(
    "private", "team", "public", name="visibility", create_type=False
)


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column("visibility", visibility, server_default="private", nullable=False),
    )
    op.drop_constraint(
        op.f("ck_mcp_servers_owner_scope_required"), "mcp_servers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_mcp_servers_exactly_one_owner_scope"),
        "mcp_servers",
        "(team_id IS NOT NULL) <> (project_id IS NOT NULL)",
    )
    op.create_table(
        "mcp_server_attachments",
        sa.Column("mcp_server_version_id", sa.UUID(), nullable=False),
        sa.Column("credential_id", sa.UUID(), nullable=True),
        sa.Column("team_id", sa.UUID(), nullable=True),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("agent_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            "((team_id IS NOT NULL)::int + (project_id IS NOT NULL)::int + "
            "(agent_id IS NOT NULL)::int) = 1",
            name=op.f("ck_mcp_server_attachments_exactly_one_target_scope"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["credentials.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["mcp_server_version_id"],
            ["mcp_server_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Preserve existing explicit credential references as scoped grants.
    op.execute(
        """
        INSERT INTO mcp_server_attachments (
            id, mcp_server_version_id, credential_id, team_id, project_id,
            created_by, created_at
        )
        SELECT gen_random_uuid(), version.id, version.credential_id,
               server.team_id, server.project_id, server.created_by, now()
        FROM mcp_server_versions AS version
        JOIN mcp_servers AS server ON server.id = version.mcp_server_id
        WHERE version.credential_id IS NOT NULL
        """
    )
    # Convert legacy inline ciphertext into a normally scoped credential and
    # link it without ever copying secret material into definition metadata.
    op.execute(
        """
        INSERT INTO credentials (
            id, team_id, project_id, created_by, name, credential_type,
            encrypted_material, metadata, created_at, updated_at
        )
        SELECT gen_random_uuid(), server.team_id, server.project_id,
               server.created_by, server.name || ' credential', 'mcp_inline',
               version.credential_ciphertext,
               jsonb_build_object('legacy_mcp_version_id', version.id::text),
               now(), now()
        FROM mcp_server_versions AS version
        JOIN mcp_servers AS server ON server.id = version.mcp_server_id
        WHERE version.credential_ciphertext IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO mcp_server_attachments (
            id, mcp_server_version_id, credential_id, team_id, project_id,
            created_by, created_at
        )
        SELECT gen_random_uuid(), version.id, credential.id,
               server.team_id, server.project_id, server.created_by, now()
        FROM mcp_server_versions AS version
        JOIN mcp_servers AS server ON server.id = version.mcp_server_id
        JOIN credentials AS credential
          ON credential.metadata->>'legacy_mcp_version_id' = version.id::text
        WHERE version.credential_ciphertext IS NOT NULL
        """
    )
    op.execute(
        "UPDATE mcp_server_versions SET credential_ciphertext = NULL, credential_id = NULL"
    )


def downgrade() -> None:
    op.drop_table("mcp_server_attachments")
    op.drop_constraint(
        op.f("ck_mcp_servers_exactly_one_owner_scope"), "mcp_servers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_mcp_servers_owner_scope_required"),
        "mcp_servers",
        "team_id IS NOT NULL OR project_id IS NOT NULL",
    )
    op.drop_column("mcp_servers", "visibility")
