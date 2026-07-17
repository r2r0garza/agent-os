"""goal lifecycle controls and task graph revisions

Revision ID: c4f8a1d2e9b7
Revises: b81d7e4f2a90
Create Date: 2026-07-17 05:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c4f8a1d2e9b7"
down_revision: Union[str, Sequence[str], None] = "b81d7e4f2a90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


goal_status = postgresql.ENUM(
    "draft",
    "active",
    "paused",
    "completed",
    "cancelled",
    "failed",
    name="goal_status",
    create_type=False,
)
goal_control_action = postgresql.ENUM(
    "pause", "resume", "cancel", name="goal_control_action", create_type=False
)
goal_control_command_status = postgresql.ENUM(
    "requested",
    "applied",
    "rejected",
    name="goal_control_command_status",
    create_type=False,
)
goal_steering_request_status = postgresql.ENUM(
    "requested",
    "applied",
    "rejected",
    name="goal_steering_request_status",
    create_type=False,
)
task_graph_revision_change = postgresql.ENUM(
    "unchanged",
    "added",
    "revised",
    "superseded",
    name="task_graph_revision_change",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    goal_control_action.create(bind, checkfirst=True)
    goal_control_command_status.create(bind, checkfirst=True)
    goal_steering_request_status.create(bind, checkfirst=True)
    task_graph_revision_change.create(bind, checkfirst=True)

    op.add_column(
        "goals",
        sa.Column("control_version", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "goals",
        sa.Column("pending_control", goal_control_action, nullable=True),
    )
    op.add_column("goals", sa.Column("control_requested_by", sa.UUID(), nullable=True))
    op.add_column(
        "goals",
        sa.Column("control_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "goals",
        sa.Column(
            "cancellation_grace_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "goals",
        sa.Column(
            "forced_termination_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "goals",
        sa.Column(
            "forced_termination_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "goals",
        sa.Column(
            "active_graph_revision_number",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_foreign_key(
        op.f("fk_goals_control_requested_by_users"),
        "goals",
        "users",
        ["control_requested_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_goals_goal_control_version_non_negative"),
        "goals",
        "control_version >= 0",
    )
    op.create_check_constraint(
        op.f("ck_goals_goal_graph_revision_non_negative"),
        "goals",
        "active_graph_revision_number >= 0",
    )
    op.create_check_constraint(
        op.f("ck_goals_goal_forced_termination_completion_requires_request"),
        "goals",
        "forced_termination_completed_at IS NULL "
        "OR forced_termination_requested_at IS NOT NULL",
    )

    op.create_table(
        "goal_lifecycle_commands",
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("requested_by", sa.UUID(), nullable=True),
        sa.Column("command_type", goal_control_action, nullable=False),
        sa.Column(
            "status",
            goal_control_command_status,
            server_default="requested",
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("prior_goal_status", goal_status, nullable=True),
        sa.Column("target_goal_status", goal_status, nullable=True),
        sa.Column(
            "cancellation_grace_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "forced_termination_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "forced_termination_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "evidence", postgresql.JSONB(), server_default="{}", nullable=False
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
        sa.CheckConstraint(
            "applied_at IS NULL OR applied_at >= created_at",
            name=op.f(
                "ck_goal_lifecycle_commands_goal_lifecycle_command_application_not_before_request"
            ),
        ),
        sa.CheckConstraint(
            "status <> 'applied' OR applied_at IS NOT NULL",
            name=op.f(
                "ck_goal_lifecycle_commands_goal_lifecycle_command_applied_has_timestamp"
            ),
        ),
        sa.CheckConstraint(
            "forced_termination_completed_at IS NULL "
            "OR forced_termination_requested_at IS NOT NULL",
            name=op.f(
                "ck_goal_lifecycle_commands_goal_lifecycle_command_forced_completion_requires_request"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "goal_id",
            "idempotency_key",
            name="uq_goal_lifecycle_commands_goal_idempotency",
        ),
    )
    op.create_index(
        "ix_goal_lifecycle_commands_goal_status",
        "goal_lifecycle_commands",
        ["goal_id", "status", "created_at"],
    )

    op.create_table(
        "goal_steering_requests",
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("requested_by", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            goal_steering_request_status,
            server_default="requested",
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("base_revision_number", sa.BigInteger(), nullable=False),
        sa.Column("applied_revision_number", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "evidence", postgresql.JSONB(), server_default="{}", nullable=False
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
        sa.CheckConstraint(
            "base_revision_number >= 0",
            name=op.f(
                "ck_goal_steering_requests_goal_steering_request_base_revision_non_negative"
            ),
        ),
        sa.CheckConstraint(
            "applied_revision_number IS NULL OR "
            "applied_revision_number > base_revision_number",
            name=op.f(
                "ck_goal_steering_requests_goal_steering_request_applied_revision_advances"
            ),
        ),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= created_at",
            name=op.f(
                "ck_goal_steering_requests_goal_steering_request_resolution_not_before_request"
            ),
        ),
        sa.CheckConstraint(
            "status <> 'applied' OR "
            "(applied_revision_number IS NOT NULL AND resolved_at IS NOT NULL)",
            name=op.f(
                "ck_goal_steering_requests_goal_steering_request_applied_has_resolution"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "goal_id",
            "idempotency_key",
            name="uq_goal_steering_requests_goal_idempotency",
        ),
    )
    op.create_index(
        "ix_goal_steering_requests_goal_status",
        "goal_steering_requests",
        ["goal_id", "status", "created_at"],
    )

    op.create_table(
        "task_graph_revisions",
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("steering_request_id", sa.UUID(), nullable=True),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.Column("parent_revision_number", sa.BigInteger(), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column(
            "graph_snapshot", postgresql.JSONB(), server_default="{}", nullable=False
        ),
        sa.Column(
            "assignment_evidence",
            postgresql.JSONB(),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "policy_context", postgresql.JSONB(), server_default="{}", nullable=False
        ),
        sa.Column(
            "budget_context", postgresql.JSONB(), server_default="{}", nullable=False
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
        sa.CheckConstraint(
            "revision_number > 0",
            name=op.f(
                "ck_task_graph_revisions_task_graph_revision_number_positive"
            ),
        ),
        sa.CheckConstraint(
            "parent_revision_number IS NULL "
            "OR (parent_revision_number >= 0 "
            "AND parent_revision_number < revision_number)",
            name=op.f(
                "ck_task_graph_revisions_task_graph_revision_parent_precedes_revision"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["steering_request_id"],
            ["goal_steering_requests.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "goal_id",
            "revision_number",
            name="uq_task_graph_revisions_goal_revision",
        ),
        sa.UniqueConstraint(
            "steering_request_id",
            name="uq_task_graph_revisions_steering_request",
        ),
    )
    op.create_index(
        "ix_task_graph_revisions_goal_created",
        "task_graph_revisions",
        ["goal_id", "created_at"],
    )

    op.create_table(
        "task_graph_revision_tasks",
        sa.Column("revision_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column(
            "change_type",
            task_graph_revision_change,
            server_default="unchanged",
            nullable=False,
        ),
        sa.Column("supersedes_task_id", sa.UUID(), nullable=True),
        sa.Column(
            "task_snapshot", postgresql.JSONB(), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(change_type = 'revised' AND supersedes_task_id IS NOT NULL) "
            "OR (change_type <> 'revised')",
            name=op.f(
                "ck_task_graph_revision_tasks_task_graph_revision_revised_task_has_predecessor"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["task_graph_revisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_task_id"], ["tasks.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("revision_id", "task_id"),
    )

    op.create_table(
        "goal_lifecycle_events",
        sa.Column(
            "sequence_number",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("lifecycle_command_id", sa.UUID(), nullable=True),
        sa.Column("steering_request_id", sa.UUID(), nullable=True),
        sa.Column("graph_revision_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("prior_goal_status", goal_status, nullable=True),
        sa.Column("resulting_goal_status", goal_status, nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(), server_default="{}", nullable=False
        ),
        sa.Column(
            "occurred_at",
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
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["graph_revision_id"],
            ["task_graph_revisions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["lifecycle_command_id"],
            ["goal_lifecycle_commands.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["steering_request_id"],
            ["goal_steering_requests.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sequence_number"),
    )
    op.create_index(
        "ix_goal_lifecycle_events_goal_sequence",
        "goal_lifecycle_events",
        ["goal_id", "sequence_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_goal_lifecycle_events_goal_sequence",
        table_name="goal_lifecycle_events",
    )
    op.drop_table("goal_lifecycle_events")
    op.drop_table("task_graph_revision_tasks")
    op.drop_index(
        "ix_task_graph_revisions_goal_created",
        table_name="task_graph_revisions",
    )
    op.drop_table("task_graph_revisions")
    op.drop_index(
        "ix_goal_steering_requests_goal_status",
        table_name="goal_steering_requests",
    )
    op.drop_table("goal_steering_requests")
    op.drop_index(
        "ix_goal_lifecycle_commands_goal_status",
        table_name="goal_lifecycle_commands",
    )
    op.drop_table("goal_lifecycle_commands")

    op.drop_constraint(
        op.f("ck_goals_goal_forced_termination_completion_requires_request"),
        "goals",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_goals_goal_graph_revision_non_negative"),
        "goals",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_goals_goal_control_version_non_negative"),
        "goals",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_goals_control_requested_by_users"),
        "goals",
        type_="foreignkey",
    )
    op.drop_column("goals", "active_graph_revision_number")
    op.drop_column("goals", "forced_termination_completed_at")
    op.drop_column("goals", "forced_termination_requested_at")
    op.drop_column("goals", "cancellation_grace_expires_at")
    op.drop_column("goals", "control_requested_at")
    op.drop_column("goals", "control_requested_by")
    op.drop_column("goals", "pending_control")
    op.drop_column("goals", "control_version")

    bind = op.get_bind()
    task_graph_revision_change.drop(bind, checkfirst=True)
    goal_steering_request_status.drop(bind, checkfirst=True)
    goal_control_command_status.drop(bind, checkfirst=True)
    goal_control_action.drop(bind, checkfirst=True)
