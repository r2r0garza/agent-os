"""goal planning persistence

Revision ID: 8f1d2c3b4a5e
Revises: 6c3e9f1a2b47
Create Date: 2026-07-17 23:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "8f1d2c3b4a5e"
down_revision: Union[str, Sequence[str], None] = "6c3e9f1a2b47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid_primary_key() -> sa.Column:
    return sa.Column(
        "id",
        sa.UUID(),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "goal_planning_sessions",
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("validation_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column(
            "constraints_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        _uuid_primary_key(),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("revision_number > 0", name="goal_planning_revision_positive"),
        sa.CheckConstraint(
            "status IN ('draft', 'previewed', 'accepted', 'rejected')",
            name="goal_planning_status_valid",
        ),
        sa.CheckConstraint(
            "validation_status IN ('pending', 'valid', 'invalid')",
            name="goal_planning_validation_status_valid",
        ),
        sa.CheckConstraint(
            "status <> 'accepted' OR accepted_at IS NOT NULL",
            name="goal_planning_accepted_has_timestamp",
        ),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "goal_id",
            "revision_number",
            name="uq_goal_planning_sessions_goal_revision",
        ),
    )
    op.create_index(
        "ix_goal_planning_sessions_goal_status",
        "goal_planning_sessions",
        ["goal_id", "status", "created_at"],
    )

    op.create_table(
        "planning_capability_requirements",
        sa.Column("planning_session_id", sa.UUID(), nullable=False),
        sa.Column("capability_key", sa.Text(), nullable=False),
        sa.Column("required", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "source_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        _uuid_primary_key(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["planning_session_id"],
            ["goal_planning_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "planning_session_id",
            "capability_key",
            name="uq_planning_requirements_session_capability",
        ),
    )

    op.create_table(
        "planning_candidates",
        sa.Column("planning_session_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("agent_version_id", sa.UUID(), nullable=False),
        sa.Column("eligible", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "matched_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "missing_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "rejection_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "constraints_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        _uuid_primary_key(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["planning_session_id"],
            ["goal_planning_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "planning_session_id",
            "agent_version_id",
            name="uq_planning_candidates_session_agent_version",
        ),
    )
    op.create_index(
        "ix_planning_candidates_session_eligible",
        "planning_candidates",
        ["planning_session_id", "eligible"],
    )

    op.create_table(
        "planning_assignments",
        sa.Column("planning_session_id", sa.UUID(), nullable=False),
        sa.Column("assignment_key", sa.Text(), nullable=False),
        sa.Column("requirement_id", sa.UUID(), nullable=True),
        sa.Column("candidate_id", sa.UUID(), nullable=True),
        sa.Column("selected_by", sa.UUID(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("validation_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column(
            "validation_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        _uuid_primary_key(),
        _created_at(),
        sa.CheckConstraint(
            "validation_status IN ('pending', 'valid', 'invalid')",
            name="planning_assignment_validation_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["planning_session_id"],
            ["goal_planning_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id"],
            ["planning_capability_requirements.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["planning_candidates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["selected_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "planning_session_id",
            "assignment_key",
            name="uq_planning_assignments_session_key",
        ),
    )

    op.create_table(
        "planning_overrides",
        sa.Column("planning_session_id", sa.UUID(), nullable=False),
        sa.Column("assignment_id", sa.UUID(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("requested_candidate_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "prior_candidate_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("validation_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column(
            "validation_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        _uuid_primary_key(),
        _created_at(),
        sa.CheckConstraint(
            "validation_status IN ('pending', 'valid', 'invalid')",
            name="planning_override_validation_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["planning_session_id"],
            ["goal_planning_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["planning_assignments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["requested_candidate_id"],
            ["planning_candidates.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("planning_overrides")
    op.drop_table("planning_assignments")
    op.drop_index(
        "ix_planning_candidates_session_eligible",
        table_name="planning_candidates",
    )
    op.drop_table("planning_candidates")
    op.drop_table("planning_capability_requirements")
    op.drop_index(
        "ix_goal_planning_sessions_goal_status",
        table_name="goal_planning_sessions",
    )
    op.drop_table("goal_planning_sessions")
