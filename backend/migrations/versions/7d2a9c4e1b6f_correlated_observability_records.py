"""correlated observability and telemetry records

Revision ID: 7d2a9c4e1b6f
Revises: 9c7b3e1a4d2f
Create Date: 2026-07-16 08:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "7d2a9c4e1b6f"
down_revision: Union[str, Sequence[str], None] = "9c7b3e1a4d2f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


observability_event_kind = postgresql.ENUM(
    "request", "goal", "task", "run", "model_call", "tool_call", "mcp_call",
    "sandbox", "approval", "budget", "artifact", "checkpoint",
    name="observability_event_kind", create_type=False,
)
telemetry_delivery_status = postgresql.ENUM(
    "pending", "delivered", "dropped", "delayed", "disabled", "failed",
    name="telemetry_delivery_status", create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    observability_event_kind.create(bind, checkfirst=True)
    telemetry_delivery_status.create(bind, checkfirst=True)

    op.create_table(
        "telemetry_export_settings",
        sa.Column("team_id", sa.UUID(), nullable=True),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("exporter_type", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("endpoint_reference", sa.Text(), nullable=True),
        sa.Column("capture_prompts", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("capture_outputs", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("redaction_policy_evidence", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("configuration_evidence", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_telemetry_export_settings_scope", "telemetry_export_settings",
        ["team_id", "project_id", "enabled"],
    )

    op.create_table(
        "observability_records",
        sa.Column("correlation_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("span_id", sa.String(length=32), nullable=True),
        sa.Column("parent_span_id", sa.String(length=32), nullable=True),
        sa.Column("event_kind", observability_event_kind, nullable=False),
        sa.Column("operation_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("team_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("goal_id", sa.UUID(), nullable=True),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("audit_event_id", sa.UUID(), nullable=True),
        sa.Column("cost_ledger_entry_id", sa.UUID(), nullable=True),
        sa.Column("approval_request_id", sa.UUID(), nullable=True),
        sa.Column("approval_decision_id", sa.UUID(), nullable=True),
        sa.Column("artifact_id", sa.UUID(), nullable=True),
        sa.Column("artifact_version_id", sa.UUID(), nullable=True),
        sa.Column("model_call_id", sa.UUID(), nullable=True),
        sa.Column("tool_call_id", sa.UUID(), nullable=True),
        sa.Column("mcp_call_id", sa.UUID(), nullable=True),
        sa.Column("sandbox_id", sa.UUID(), nullable=True),
        sa.Column("checkpoint_id", sa.UUID(), nullable=True),
        sa.Column("attributes", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("capture_policy_evidence", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("redaction_evidence", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["audit_event_id"], ["audit_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cost_ledger_entry_id"], ["cost_ledger_entries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approval_request_id"], ["approval_requests.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approval_decision_id"], ["approval_decisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["artifact_version_id"], ["artifact_versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for suffix, columns in (
        ("correlation", ["correlation_id", "occurred_at"]),
        ("trace", ["trace_id", "span_id"]),
        ("run", ["run_id", "occurred_at"]),
        ("goal", ["goal_id", "occurred_at"]),
        ("project", ["project_id", "occurred_at"]),
        ("team", ["team_id", "occurred_at"]),
    ):
        op.create_index(f"ix_observability_records_{suffix}", "observability_records", columns)

    op.create_table(
        "telemetry_export_attempts",
        sa.Column("observability_record_id", sa.UUID(), nullable=False),
        sa.Column("export_setting_id", sa.UUID(), nullable=True),
        sa.Column("destination", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", telemetry_delivery_status, server_default="pending", nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("delivery_evidence", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["observability_record_id"], ["observability_records.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["export_setting_id"], ["telemetry_export_settings.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "observability_record_id", "destination", "attempt_number",
            name="uq_telemetry_export_attempts_record_destination_attempt",
        ),
        sa.CheckConstraint("attempt_number > 0", name="attempt_number_positive"),
    )
    op.create_index(
        "ix_telemetry_export_attempts_status", "telemetry_export_attempts", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_telemetry_export_attempts_status", table_name="telemetry_export_attempts")
    op.drop_table("telemetry_export_attempts")
    for suffix in ("team", "project", "goal", "run", "trace", "correlation"):
        op.drop_index(f"ix_observability_records_{suffix}", table_name="observability_records")
    op.drop_table("observability_records")
    op.drop_index("ix_telemetry_export_settings_scope", table_name="telemetry_export_settings")
    op.drop_table("telemetry_export_settings")

    bind = op.get_bind()
    telemetry_delivery_status.drop(bind, checkfirst=True)
    observability_event_kind.drop(bind, checkfirst=True)
