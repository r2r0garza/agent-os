"""durable approval and budget governance records

Revision ID: 9c7b3e1a4d2f
Revises: 4bbfa2a607f8
Create Date: 2026-07-16 02:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "9c7b3e1a4d2f"
down_revision: Union[str, Sequence[str], None] = "4bbfa2a607f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


approval_mode = postgresql.ENUM(
    "auto", "consequential", "every_tool_call", name="approval_mode", create_type=False
)
approval_request_status = postgresql.ENUM(
    "pending", "approved", "denied", "expired", "cancelled",
    name="approval_request_status", create_type=False,
)
approval_decision_type = postgresql.ENUM(
    "approved", "denied", "expired", "cancelled",
    name="approval_decision_type", create_type=False,
)
budget_reservation_status = postgresql.ENUM(
    "active", "reconciled", "released", "rejected",
    name="budget_reservation_status", create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    approval_mode.create(bind, checkfirst=True)
    approval_request_status.create(bind, checkfirst=True)
    approval_decision_type.create(bind, checkfirst=True)
    budget_reservation_status.create(bind, checkfirst=True)

    op.create_table(
        "approval_mode_configurations",
        sa.Column("team_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("goal_id", sa.UUID(), nullable=True),
        sa.Column("configured_by", sa.UUID(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("mode", approval_mode, nullable=False),
        sa.Column(
            "consequential_action_types",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "context", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["configured_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("version_number > 0", name="version_positive"),
        sa.CheckConstraint(
            "project_id IS NOT NULL OR goal_id IS NULL",
            name="goal_configuration_requires_project",
        ),
    )
    op.create_index(
        "ix_approval_mode_configurations_scope",
        "approval_mode_configurations",
        ["team_id", "project_id", "goal_id", "version_number"],
    )

    op.create_table(
        "approval_requests",
        sa.Column("team_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("agent_version_id", sa.UUID(), nullable=False),
        sa.Column("configuration_id", sa.UUID(), nullable=True),
        sa.Column("requested_by", sa.UUID(), nullable=True),
        sa.Column("mode", approval_mode, nullable=False),
        sa.Column("status", approval_request_status, server_default="pending", nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column(
            "action_preview", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "policy_version_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "policy_evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["configuration_id"], ["approval_mode_configurations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= created_at",
            name="resolution_not_before_creation",
        ),
    )
    op.create_index(
        "ix_approval_requests_scope", "approval_requests", ["team_id", "project_id", "run_id", "status"]
    )

    op.create_table(
        "approval_decisions",
        sa.Column("approval_request_id", sa.UUID(), nullable=False),
        sa.Column("decision", approval_decision_type, nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "context", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "evaluated_policy_version_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["approval_request_id"], ["approval_requests.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_approval_decisions_request", "approval_decisions", ["approval_request_id", "created_at"]
    )

    op.create_table(
        "admin_overrides",
        sa.Column("team_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("goal_id", sa.UUID(), nullable=True),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "evaluated_policy_version_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "context", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("expires_at > starts_at", name="expiry_after_start"),
    )
    op.create_index(
        "ix_admin_overrides_scope", "admin_overrides", ["team_id", "project_id", "scope_type", "scope_id"]
    )

    op.create_table(
        "budget_reservations",
        sa.Column("budget_id", sa.UUID(), nullable=False),
        sa.Column("team_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("agent_version_id", sa.UUID(), nullable=False),
        sa.Column("requested_by", sa.UUID(), nullable=True),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("amount_minor_units", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("status", budget_reservation_status, server_default="active", nullable=False),
        sa.Column("is_unpriced", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("warning_triggered", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("hard_stop_triggered", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "pricing_evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "policy_version_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["budget_id"], ["budgets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("amount_minor_units >= 0", name="amount_non_negative"),
        sa.CheckConstraint("NOT is_unpriced OR amount_minor_units = 0", name="unpriced_amount_is_zero"),
    )
    op.create_index(
        "ix_budget_reservations_scope",
        "budget_reservations",
        ["team_id", "project_id", "run_id", "status"],
    )

    op.add_column("cost_ledger_entries", sa.Column("reservation_id", sa.UUID(), nullable=True))
    op.add_column("cost_ledger_entries", sa.Column("team_id", sa.UUID(), nullable=True))
    op.add_column("cost_ledger_entries", sa.Column("project_id", sa.UUID(), nullable=True))
    op.add_column("cost_ledger_entries", sa.Column("goal_id", sa.UUID(), nullable=True))
    op.add_column("cost_ledger_entries", sa.Column("task_id", sa.UUID(), nullable=True))
    op.add_column("cost_ledger_entries", sa.Column("agent_version_id", sa.UUID(), nullable=True))
    op.add_column("cost_ledger_entries", sa.Column("actor_id", sa.UUID(), nullable=True))
    op.add_column(
        "cost_ledger_entries", sa.Column("is_unpriced", sa.Boolean(), server_default="false", nullable=False)
    )
    op.add_column(
        "cost_ledger_entries",
        sa.Column("warning_triggered", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "cost_ledger_entries",
        sa.Column("hard_stop_triggered", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "cost_ledger_entries",
        sa.Column(
            "evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
    )
    op.create_foreign_key(
        "cost_ledger_entries_reservation_id_fkey",
        "cost_ledger_entries", "budget_reservations", ["reservation_id"], ["id"], ondelete="SET NULL",
    )
    for column, target in (
        ("team_id", "teams"),
        ("project_id", "projects"),
        ("goal_id", "goals"),
        ("task_id", "tasks"),
        ("agent_version_id", "agent_versions"),
        ("actor_id", "users"),
    ):
        op.create_foreign_key(
            f"cost_ledger_entries_{column}_fkey",
            "cost_ledger_entries", target, [column], ["id"], ondelete="SET NULL",
        )
    op.create_check_constraint(
        "actual_non_negative", "cost_ledger_entries", "actual_amount_minor_units IS NULL OR actual_amount_minor_units >= 0"
    )
    op.create_check_constraint(
        "unpriced_amount_is_zero",
        "cost_ledger_entries",
        "NOT is_unpriced OR (reserved_amount_minor_units = 0 AND COALESCE(actual_amount_minor_units, 0) = 0)",
    )
    op.create_index(
        "ix_cost_ledger_entries_scope",
        "cost_ledger_entries",
        ["team_id", "project_id", "run_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_cost_ledger_entries_scope", table_name="cost_ledger_entries")
    op.drop_constraint("unpriced_amount_is_zero", "cost_ledger_entries", type_="check")
    op.drop_constraint("actual_non_negative", "cost_ledger_entries", type_="check")
    for column in ("actor_id", "agent_version_id", "task_id", "goal_id", "project_id", "team_id"):
        op.drop_constraint(f"cost_ledger_entries_{column}_fkey", "cost_ledger_entries", type_="foreignkey")
    op.drop_constraint("cost_ledger_entries_reservation_id_fkey", "cost_ledger_entries", type_="foreignkey")
    for column in (
        "evidence", "hard_stop_triggered", "warning_triggered", "is_unpriced", "actor_id",
        "agent_version_id", "task_id", "goal_id", "project_id", "team_id", "reservation_id",
    ):
        op.drop_column("cost_ledger_entries", column)

    op.drop_index("ix_budget_reservations_scope", table_name="budget_reservations")
    op.drop_table("budget_reservations")
    op.drop_index("ix_admin_overrides_scope", table_name="admin_overrides")
    op.drop_table("admin_overrides")
    op.drop_index("ix_approval_decisions_request", table_name="approval_decisions")
    op.drop_table("approval_decisions")
    op.drop_index("ix_approval_requests_scope", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_index("ix_approval_mode_configurations_scope", table_name="approval_mode_configurations")
    op.drop_table("approval_mode_configurations")

    bind = op.get_bind()
    budget_reservation_status.drop(bind, checkfirst=True)
    approval_decision_type.drop(bind, checkfirst=True)
    approval_request_status.drop(bind, checkfirst=True)
    approval_mode.drop(bind, checkfirst=True)
