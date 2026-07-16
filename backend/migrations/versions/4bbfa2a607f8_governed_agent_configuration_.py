"""governed agent configuration credentials and snapshots

Revision ID: 4bbfa2a607f8
Revises: f6a1c02b7d4e
Create Date: 2026-07-15 22:09:48.281473

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '4bbfa2a607f8'
down_revision: Union[str, Sequence[str], None] = 'f6a1c02b7d4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "credentials",
        sa.Column("team_id", sa.UUID(), nullable=True),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("credential_type", sa.Text(), nullable=False),
        sa.Column("encrypted_material", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("(team_id IS NOT NULL) <> (project_id IS NOT NULL)", name="exactly_one_owner_scope"),
    )

    op.create_table(
        "model_profile_versions",
        sa.Column("model_profile_id", sa.UUID(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model_identifier", sa.Text(), nullable=False),
        sa.Column("credential_id", sa.UUID(), nullable=True),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("capability_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("pricing_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["model_profile_id"], ["model_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["credential_id"], ["credentials.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_profile_id", "version_number", name="uq_model_profile_versions_profile_version"),
    )

    op.add_column(
        "agent_versions",
        sa.Column("model_profile_version_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "agent_versions_model_profile_version_id_fkey",
        "agent_versions",
        "model_profile_versions",
        ["model_profile_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "agent_version_skills",
        sa.Column("agent_version_id", sa.UUID(), nullable=False),
        sa.Column("skill_version_id", sa.UUID(), nullable=False),
        sa.Column("attachment_config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_version_id", "skill_version_id", name="uq_agent_version_skills_attachment"),
    )

    op.add_column(
        "mcp_server_versions",
        sa.Column("credential_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "mcp_server_versions_credential_id_fkey",
        "mcp_server_versions",
        "credentials",
        ["credential_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "agent_version_mcp_servers",
        sa.Column("agent_version_id", sa.UUID(), nullable=False),
        sa.Column("mcp_server_version_id", sa.UUID(), nullable=False),
        sa.Column("attachment_config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["mcp_server_version_id"], ["mcp_server_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_version_id", "mcp_server_version_id", name="uq_agent_version_mcp_servers_attachment"
        ),
    )

    op.create_table(
        "policy_sets",
        sa.Column("team_id", sa.UUID(), nullable=True),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("(team_id IS NOT NULL) <> (project_id IS NOT NULL)", name="exactly_one_owner_scope"),
    )

    op.create_table(
        "policy_set_versions",
        sa.Column("policy_set_id", sa.UUID(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["policy_set_id"], ["policy_sets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("policy_set_id", "version_number", name="uq_policy_set_versions_set_version"),
    )

    op.create_table(
        "agent_version_policy_sets",
        sa.Column("agent_version_id", sa.UUID(), nullable=False),
        sa.Column("policy_set_version_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["policy_set_version_id"], ["policy_set_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_version_id", "policy_set_version_id", name="uq_agent_version_policy_sets_attachment"
        ),
    )

    op.create_table(
        "run_configuration_snapshots",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("team_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("agent_version_id", sa.UUID(), nullable=False),
        sa.Column("model_profile_version_id", sa.UUID(), nullable=True),
        sa.Column("budget_id", sa.UUID(), nullable=True),
        sa.Column("configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["model_profile_version_id"], ["model_profile_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["budget_id"], ["budgets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_run_configuration_snapshots_run"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("run_configuration_snapshots")
    op.drop_table("agent_version_policy_sets")
    op.drop_table("policy_set_versions")
    op.drop_table("policy_sets")
    op.drop_table("agent_version_mcp_servers")
    op.drop_constraint("mcp_server_versions_credential_id_fkey", "mcp_server_versions", type_="foreignkey")
    op.drop_column("mcp_server_versions", "credential_id")
    op.drop_table("agent_version_skills")
    op.drop_constraint("agent_versions_model_profile_version_id_fkey", "agent_versions", type_="foreignkey")
    op.drop_column("agent_versions", "model_profile_version_id")
    op.drop_table("model_profile_versions")
    op.drop_table("credentials")
