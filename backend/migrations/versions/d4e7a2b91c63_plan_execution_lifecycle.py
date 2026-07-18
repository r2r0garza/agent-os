"""plan execution lifecycle

Revision ID: d4e7a2b91c63
Revises: 6b3e6c15068d
Create Date: 2026-07-18 03:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d4e7a2b91c63"
down_revision: Union[str, Sequence[str], None] = "6b3e6c15068d"
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
        "goal_plan_executions",
        sa.Column("planning_session_id", sa.UUID(), nullable=False),
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("graph_revision_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("total_tasks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pending_tasks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("running_tasks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_tasks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_tasks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancelled_tasks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _uuid_primary_key(),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="goal_plan_execution_status_valid",
        ),
        sa.CheckConstraint(
            "total_tasks >= 0 AND pending_tasks >= 0 AND running_tasks >= 0 "
            "AND completed_tasks >= 0 AND failed_tasks >= 0 AND cancelled_tasks >= 0",
            name="goal_plan_execution_counts_non_negative",
        ),
        sa.CheckConstraint(
            "pending_tasks + running_tasks + completed_tasks + failed_tasks "
            "+ cancelled_tasks = total_tasks",
            name="goal_plan_execution_counts_total",
        ),
        sa.ForeignKeyConstraint(
            ["planning_session_id"], ["goal_planning_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["graph_revision_id"], ["task_graph_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "planning_session_id", name="uq_goal_plan_executions_planning_session"
        ),
    )
    op.create_index(
        "ix_goal_plan_executions_goal_status",
        "goal_plan_executions",
        ["goal_id", "status", "created_at"],
    )

    op.create_table(
        "plan_task_context_packages",
        sa.Column("plan_execution_id", sa.UUID(), nullable=False),
        sa.Column("planning_session_id", sa.UUID(), nullable=False),
        sa.Column("planning_assignment_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("agent_version_id", sa.UUID(), nullable=False),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        _uuid_primary_key(),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["plan_execution_id"], ["goal_plan_executions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["planning_session_id"], ["goal_planning_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["planning_assignment_id"], ["planning_assignments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_execution_id",
            "task_id",
            name="uq_plan_task_context_packages_execution_task",
        ),
    )
    op.create_index(
        "ix_plan_task_context_packages_planning_session",
        "plan_task_context_packages",
        ["planning_session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plan_task_context_packages_planning_session",
        table_name="plan_task_context_packages",
    )
    op.drop_table("plan_task_context_packages")
    op.drop_index(
        "ix_goal_plan_executions_goal_status",
        table_name="goal_plan_executions",
    )
    op.drop_table("goal_plan_executions")
