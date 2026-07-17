from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agentic_os.domain.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin

UserRole = Enum("admin", "regular_user", name="user_role", validate_strings=True)
TeamMemberRole = Enum("owner", "member", name="team_member_role", validate_strings=True)
AgentVisibility = Enum("private", "team", "public", name="visibility", validate_strings=True)
GoalStatus = Enum(
    "draft", "active", "paused", "completed", "cancelled", "failed", name="goal_status", validate_strings=True
)
GoalControlAction = Enum(
    "pause", "resume", "cancel", name="goal_control_action", validate_strings=True
)
GoalControlCommandStatus = Enum(
    "requested", "applied", "rejected",
    name="goal_control_command_status", validate_strings=True,
)
GoalSteeringRequestStatus = Enum(
    "requested", "applied", "rejected",
    name="goal_steering_request_status", validate_strings=True,
)
TaskGraphRevisionChange = Enum(
    "unchanged", "added", "revised", "superseded",
    name="task_graph_revision_change", validate_strings=True,
)
TaskStatus = Enum(
    "pending", "ready", "running", "blocked", "completed", "failed", "cancelled",
    name="task_status", validate_strings=True,
)
RunStatus = Enum(
    "queued", "running", "waiting_approval", "completed", "failed", "cancelled",
    name="run_status", validate_strings=True,
)
PolicyScopeType = Enum(
    "installation", "team", "project", "agent", "goal", "mcp_server", "tool",
    name="policy_scope_type", validate_strings=True,
)
PolicyDecision = Enum("deny", "approval_required", "allow", name="policy_decision", validate_strings=True)
BudgetEnforcementMode = Enum("warning", "hard_stop", name="budget_enforcement_mode", validate_strings=True)
LedgerEntryStatus = Enum("reserved", "reconciled", "void", name="ledger_entry_status", validate_strings=True)
ApprovalMode = Enum(
    "auto", "consequential", "every_tool_call", name="approval_mode", validate_strings=True
)
ApprovalRequestStatus = Enum(
    "pending", "approved", "denied", "expired", "cancelled",
    name="approval_request_status", validate_strings=True,
)
ApprovalDecisionType = Enum(
    "approved", "denied", "expired", "cancelled",
    name="approval_decision_type", validate_strings=True,
)
BudgetReservationStatus = Enum(
    "active", "reconciled", "released", "rejected",
    name="budget_reservation_status", validate_strings=True,
)
ArtifactStorageState = Enum(
    "staged", "finalized", "missing", "orphaned", name="artifact_storage_state", validate_strings=True
)
ArtifactKind = Enum("source", "normalized", "output", name="artifact_kind", validate_strings=True)
ArtifactIngestionStatus = Enum(
    "not_applicable", "pending", "complete", "failed", "unsupported", "needs_reconciliation",
    name="artifact_ingestion_status", validate_strings=True,
)
ObservabilityEventKind = Enum(
    "request", "goal", "task", "run", "model_call", "tool_call", "mcp_call",
    "sandbox", "approval", "budget", "artifact", "checkpoint",
    name="observability_event_kind", validate_strings=True,
)
TelemetryDeliveryStatus = Enum(
    "pending", "delivered", "dropped", "delayed", "disabled", "failed",
    name="telemetry_delivery_status", validate_strings=True,
)


class Team(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "teams"

    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(UserRole, nullable=False, server_default="regular_user")


class TeamMembership(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "team_memberships"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_memberships_team_user"),)

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(TeamMemberRole, nullable=False, server_default="member")


class Project(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "projects"

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)


class ProjectMember(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Goal(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint("control_version >= 0", name="goal_control_version_non_negative"),
        CheckConstraint(
            "active_graph_revision_number >= 0",
            name="goal_graph_revision_non_negative",
        ),
        CheckConstraint(
            "forced_termination_completed_at IS NULL "
            "OR forced_termination_requested_at IS NOT NULL",
            name="goal_forced_termination_completion_requires_request",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(GoalStatus, nullable=False, server_default="draft")
    control_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    pending_control: Mapped[str | None] = mapped_column(GoalControlAction, nullable=True)
    control_requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    control_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_grace_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    forced_termination_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    forced_termination_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active_graph_revision_number: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )


class Task(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tasks"

    goal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(TaskStatus, nullable=False, server_default="pending")
    required_capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    capability_rationale: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    expected_outputs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    resource_intent: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    policy_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    knowledge_artifact_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    budget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budgets.id", ondelete="SET NULL"), nullable=True
    )
    assigned_agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=True
    )
    assignment_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="unassigned")
    assignment_candidates: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    assignment_rationale: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    assignment_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceResource(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workspace_resources"
    __table_args__ = (
        UniqueConstraint("project_id", "resource_key", name="uq_workspace_resources_project_key"),
        CheckConstraint("revision >= 0", name="workspace_resource_revision_non_negative"),
        CheckConstraint("last_fencing_token >= 0", name="workspace_resource_fencing_non_negative"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    resource_key: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class WorkspaceResourceLease(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workspace_resource_leases"
    __table_args__ = (
        UniqueConstraint("resource_id", name="uq_workspace_resource_leases_resource"),
        CheckConstraint("fencing_token > 0", name="workspace_resource_lease_fencing_positive"),
        CheckConstraint("expected_revision >= 0", name="workspace_resource_lease_revision_non_negative"),
    )

    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_resources.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_lease_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspacePromotion(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "workspace_promotions"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_workspace_promotions_run"),
        CheckConstraint(
            "status in ('promoted', 'conflict', 'denied')",
            name="workspace_promotion_status_valid",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expected_revisions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    resulting_revisions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    conflict_details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (
        CheckConstraint("task_id <> depends_on_task_id", name="no_self_reference"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )


class GoalLifecycleCommand(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "goal_lifecycle_commands"
    __table_args__ = (
        UniqueConstraint(
            "goal_id", "idempotency_key", name="uq_goal_lifecycle_commands_goal_idempotency"
        ),
        CheckConstraint(
            "applied_at IS NULL OR applied_at >= created_at",
            name="goal_lifecycle_command_application_not_before_request",
        ),
        CheckConstraint(
            "status <> 'applied' OR applied_at IS NOT NULL",
            name="goal_lifecycle_command_applied_has_timestamp",
        ),
        CheckConstraint(
            "forced_termination_completed_at IS NULL "
            "OR forced_termination_requested_at IS NOT NULL",
            name="goal_lifecycle_command_forced_completion_requires_request",
        ),
        Index(
            "ix_goal_lifecycle_commands_goal_status",
            "goal_id",
            "status",
            "created_at",
        ),
    )

    goal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    command_type: Mapped[str] = mapped_column(GoalControlAction, nullable=False)
    status: Mapped[str] = mapped_column(
        GoalControlCommandStatus, nullable=False, server_default="requested"
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    prior_goal_status: Mapped[str | None] = mapped_column(GoalStatus, nullable=True)
    target_goal_status: Mapped[str | None] = mapped_column(GoalStatus, nullable=True)
    cancellation_grace_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    forced_termination_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    forced_termination_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class GoalSteeringRequest(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "goal_steering_requests"
    __table_args__ = (
        UniqueConstraint(
            "goal_id", "idempotency_key", name="uq_goal_steering_requests_goal_idempotency"
        ),
        CheckConstraint(
            "base_revision_number >= 0",
            name="goal_steering_request_base_revision_non_negative",
        ),
        CheckConstraint(
            "applied_revision_number IS NULL OR "
            "applied_revision_number > base_revision_number",
            name="goal_steering_request_applied_revision_advances",
        ),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= created_at",
            name="goal_steering_request_resolution_not_before_request",
        ),
        CheckConstraint(
            "status <> 'applied' OR "
            "(applied_revision_number IS NOT NULL AND resolved_at IS NOT NULL)",
            name="goal_steering_request_applied_has_resolution",
        ),
        Index(
            "ix_goal_steering_requests_goal_status",
            "goal_id",
            "status",
            "created_at",
        ),
    )

    goal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        GoalSteeringRequestStatus, nullable=False, server_default="requested"
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    base_revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    applied_revision_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class TaskGraphRevision(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "task_graph_revisions"
    __table_args__ = (
        UniqueConstraint(
            "goal_id", "revision_number", name="uq_task_graph_revisions_goal_revision"
        ),
        UniqueConstraint(
            "steering_request_id", name="uq_task_graph_revisions_steering_request"
        ),
        CheckConstraint(
            "revision_number > 0", name="task_graph_revision_number_positive"
        ),
        CheckConstraint(
            "parent_revision_number IS NULL "
            "OR (parent_revision_number >= 0 AND parent_revision_number < revision_number)",
            name="task_graph_revision_parent_precedes_revision",
        ),
        Index(
            "ix_task_graph_revisions_goal_created",
            "goal_id",
            "created_at",
        ),
    )

    goal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    steering_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("goal_steering_requests.id", ondelete="SET NULL"),
        nullable=True,
    )
    revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parent_revision_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    graph_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    assignment_evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    policy_context: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    budget_context: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class TaskGraphRevisionTask(Base, CreatedAtMixin):
    __tablename__ = "task_graph_revision_tasks"
    __table_args__ = (
        CheckConstraint(
            "(change_type = 'revised' AND supersedes_task_id IS NOT NULL) "
            "OR (change_type <> 'revised')",
            name="task_graph_revision_revised_task_has_predecessor",
        ),
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_graph_revisions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    change_type: Mapped[str] = mapped_column(
        TaskGraphRevisionChange, nullable=False, server_default="unchanged"
    )
    supersedes_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True
    )
    task_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class GoalLifecycleEvent(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "goal_lifecycle_events"
    __table_args__ = (
        Index(
            "ix_goal_lifecycle_events_goal_sequence",
            "goal_id",
            "sequence_number",
            unique=True,
        ),
    )

    sequence_number: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        nullable=False,
        unique=True,
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    lifecycle_command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("goal_lifecycle_commands.id", ondelete="SET NULL"),
        nullable=True,
    )
    steering_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("goal_steering_requests.id", ondelete="SET NULL"),
        nullable=True,
    )
    graph_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_graph_revisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    prior_goal_status: Mapped[str | None] = mapped_column(GoalStatus, nullable=True)
    resulting_goal_status: Mapped[str | None] = mapped_column(GoalStatus, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ModelProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "model_profiles"

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    model_identifier: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    capability_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    pricing_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class Credential(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "credentials"
    __table_args__ = (
        CheckConstraint(
            "(team_id IS NOT NULL) <> (project_id IS NOT NULL)",
            name="exactly_one_owner_scope",
        ),
    )

    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    credential_type: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_material: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")

    def redacted_metadata(self) -> dict:
        """Return the safe default representation without secret material."""
        return {
            "id": str(self.id),
            "team_id": str(self.team_id) if self.team_id else None,
            "project_id": str(self.project_id) if self.project_id else None,
            "name": self.name,
            "credential_type": self.credential_type,
            "metadata": dict(self.metadata_ or {}),
            "configured": bool(self.encrypted_material),
        }


class ModelProfileVersion(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "model_profile_versions"
    __table_args__ = (
        UniqueConstraint("model_profile_id", "version_number", name="uq_model_profile_versions_profile_version"),
    )

    model_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_profiles.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    model_identifier: Mapped[str] = mapped_column(Text, nullable=False)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credentials.id", ondelete="RESTRICT"), nullable=True
    )
    headers: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    capability_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    pricing_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class ModelProfileProbe(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "model_profile_probes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('completed', 'degraded', 'failed')",
            name="valid_status",
        ),
    )

    model_profile_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_profile_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    capability_evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    pricing_evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    request_metadata: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    diagnostics: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Agent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "agents"

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(AgentVisibility, nullable=False, server_default="private")


class AgentVersion(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version_number", name="uq_agent_versions_agent_version"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    capability_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_profiles.id", ondelete="RESTRICT"), nullable=True
    )
    model_profile_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_profile_versions.id", ondelete="RESTRICT"), nullable=True
    )
    default_budget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budgets.id", ondelete="SET NULL"), nullable=True
    )


class AgentInstallation(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Lineage record for an agent installed from a team-visible or public source version.

    Installing pins an immutable source version into a new, independently
    governed `Agent` row owned by the installing team; the source owner
    cannot mutate the installed copy and the installer cannot edit the source.
    """

    __tablename__ = "agent_installations"
    __table_args__ = (
        UniqueConstraint("installed_agent_id", name="uq_agent_installations_installed_agent"),
    )

    installed_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    source_agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    installed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class Skill(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "skills"

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(AgentVisibility, nullable=False, server_default="private")


class SkillVersion(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint("skill_id", "version_number", name="uq_skill_versions_skill_version"),
        CheckConstraint(
            "validation_status IN ('legacy', 'valid')",
            name="valid_validation_status",
        ),
    )

    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_ref: Mapped[str] = mapped_column(Text, nullable=False)
    resource_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    package_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    resources: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    declared_capabilities: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    package_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="legacy"
    )
    validation_diagnostics: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )


class SkillInstallation(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Lineage record for a skill installed from a team-visible or public source version.

    Mirrors `AgentInstallation`: the installed skill is a new, independently
    governed `Skill` row owned by the installing team, pinned to the source
    version at install time.
    """

    __tablename__ = "skill_installations"
    __table_args__ = (
        UniqueConstraint("installed_skill_id", name="uq_skill_installations_installed_skill"),
    )

    installed_skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    source_skill_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill_versions.id", ondelete="RESTRICT"), nullable=False
    )
    installed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class AgentVersionSkill(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "agent_version_skills"
    __table_args__ = (
        UniqueConstraint("agent_version_id", "skill_version_id", name="uq_agent_version_skills_attachment"),
    )

    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False
    )
    skill_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill_versions.id", ondelete="RESTRICT"), nullable=False
    )
    attachment_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class McpServer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        CheckConstraint(
            "(team_id IS NOT NULL) <> (project_id IS NOT NULL)", name="exactly_one_owner_scope"
        ),
    )

    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(AgentVisibility, nullable=False, server_default="private")


class McpServerVersion(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "mcp_server_versions"
    __table_args__ = (
        UniqueConstraint("mcp_server_id", "version_number", name="uq_mcp_server_versions_server_version"),
    )

    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    connection_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    credential_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credentials.id", ondelete="RESTRICT"), nullable=True
    )


class McpServerAttachment(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A revocable permission to use an MCP definition in one runtime scope.

    Definitions and their versions contain shareable, non-secret connection
    metadata.  Credential permission lives here instead, attached to exactly
    one team, project, or agent.  This prevents definition visibility from
    implicitly granting credential use.
    """

    __tablename__ = "mcp_server_attachments"
    __table_args__ = (
        CheckConstraint(
            "((team_id IS NOT NULL)::int + (project_id IS NOT NULL)::int + "
            "(agent_id IS NOT NULL)::int) = 1",
            name="exactly_one_target_scope",
        ),
    )

    mcp_server_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_server_versions.id", ondelete="CASCADE"), nullable=False
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credentials.id", ondelete="RESTRICT"), nullable=True
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentVersionMcpServer(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "agent_version_mcp_servers"
    __table_args__ = (
        UniqueConstraint(
            "agent_version_id", "mcp_server_version_id", name="uq_agent_version_mcp_servers_attachment"
        ),
    )

    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False
    )
    mcp_server_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_server_versions.id", ondelete="RESTRICT"), nullable=False
    )
    attachment_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class Policy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "policies"

    scope_type: Mapped[str] = mapped_column(PolicyScopeType, nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decision: Mapped[str] = mapped_column(PolicyDecision, nullable=False)
    rule: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class PolicySet(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "policy_sets"
    __table_args__ = (
        CheckConstraint(
            "(team_id IS NOT NULL) <> (project_id IS NOT NULL)",
            name="exactly_one_owner_scope",
        ),
    )

    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)


class PolicySetVersion(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "policy_set_versions"
    __table_args__ = (
        UniqueConstraint("policy_set_id", "version_number", name="uq_policy_set_versions_set_version"),
    )

    policy_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_sets.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    rules: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")


class AgentVersionPolicySet(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "agent_version_policy_sets"
    __table_args__ = (
        UniqueConstraint(
            "agent_version_id", "policy_set_version_id", name="uq_agent_version_policy_sets_attachment"
        ),
    )

    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False
    )
    policy_set_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_set_versions.id", ondelete="RESTRICT"), nullable=False
    )


class Budget(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "budgets"
    __table_args__ = (CheckConstraint("amount_minor_units >= 0", name="amount_non_negative"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    amount_minor_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    enforcement_mode: Mapped[str] = mapped_column(BudgetEnforcementMode, nullable=False)
    warning_threshold_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Run(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_runs_task_attempt"),
        UniqueConstraint("task_id", "idempotency_key", name="uq_runs_task_idempotency_key"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    lease_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    langgraph_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(RunStatus, nullable=False, server_default="queued")
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunConfigurationSnapshot(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "run_configuration_snapshots"
    __table_args__ = (UniqueConstraint("run_id", name="uq_run_configuration_snapshots_run"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    model_profile_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_profile_versions.id", ondelete="RESTRICT"), nullable=True
    )
    budget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budgets.id", ondelete="RESTRICT"), nullable=True
    )
    configuration: Mapped[dict] = mapped_column(JSONB, nullable=False)


class ApprovalModeConfiguration(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "approval_mode_configurations"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="version_positive"),
        CheckConstraint(
            "project_id IS NOT NULL OR goal_id IS NULL",
            name="goal_configuration_requires_project",
        ),
        Index(
            "ix_approval_mode_configurations_scope",
            "team_id",
            "project_id",
            "goal_id",
            "version_number",
        ),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=True
    )
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="RESTRICT"), nullable=True
    )
    configured_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(ApprovalMode, nullable=False)
    consequential_action_types: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class ApprovalRequest(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= created_at",
            name="resolution_not_before_creation",
        ),
        Index("ix_approval_requests_scope", "team_id", "project_id", "run_id", "status"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    configuration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_mode_configurations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    mode: Mapped[str] = mapped_column(ApprovalMode, nullable=False)
    status: Mapped[str] = mapped_column(ApprovalRequestStatus, nullable=False, server_default="pending")
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    action_preview: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    policy_version_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    policy_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalDecisionRecord(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        Index("ix_approval_decisions_request", "approval_request_id", "created_at"),
    )

    approval_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("approval_requests.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[str] = mapped_column(ApprovalDecisionType, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    evaluated_policy_version_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )


class AdminOverride(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "admin_overrides"
    __table_args__ = (
        CheckConstraint("expires_at > starts_at", name="expiry_after_start"),
        Index("ix_admin_overrides_scope", "team_id", "project_id", "scope_type", "scope_id"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=True
    )
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="RESTRICT"), nullable=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluated_policy_version_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class BudgetReservation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "budget_reservations"
    __table_args__ = (
        CheckConstraint("amount_minor_units >= 0", name="amount_non_negative"),
        CheckConstraint(
            "NOT is_unpriced OR amount_minor_units = 0",
            name="unpriced_amount_is_zero",
        ),
        Index("ix_budget_reservations_scope", "team_id", "project_id", "run_id", "status"),
    )

    budget_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budgets.id", ondelete="RESTRICT"), nullable=False
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(
        BudgetReservationStatus, nullable=False, server_default="active"
    )
    is_unpriced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    warning_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    hard_stop_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    pricing_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    policy_version_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CostLedgerEntry(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "cost_ledger_entries"
    __table_args__ = (
        CheckConstraint("reserved_amount_minor_units >= 0", name="reserved_non_negative"),
        CheckConstraint(
            "actual_amount_minor_units IS NULL OR actual_amount_minor_units >= 0",
            name="actual_non_negative",
        ),
        CheckConstraint(
            "NOT is_unpriced OR (reserved_amount_minor_units = 0 "
            "AND COALESCE(actual_amount_minor_units, 0) = 0)",
            name="unpriced_amount_is_zero",
        ),
        Index("ix_cost_ledger_entries_scope", "team_id", "project_id", "run_id", "status"),
    )

    budget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budgets.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budget_reservations.id", ondelete="SET NULL"), nullable=True
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="SET NULL"), nullable=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    reserved_amount_minor_units: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    actual_amount_minor_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    is_zero_cost: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_unpriced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    warning_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    hard_stop_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(LedgerEntryStatus, nullable=False, server_default="reserved")


class Artifact(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "artifacts"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    parent_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(ArtifactKind, nullable=False, server_default="source")
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingestion_status: Mapped[str] = mapped_column(
        ArtifactIngestionStatus, nullable=False, server_default="not_applicable"
    )
    ingestion_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    ingestion_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ArtifactBlob(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "artifact_blobs"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_artifact_blobs_content_hash"),
        CheckConstraint("size_bytes >= 0", name="artifact_blob_size_non_negative"),
    )

    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(ArtifactStorageState, nullable=False, server_default="staged")
    staged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciliation_details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class ArtifactVersion(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "artifact_versions"
    __table_args__ = (
        UniqueConstraint("artifact_id", "version_number", name="uq_artifact_versions_artifact_version"),
    )

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    blob_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifact_blobs.id", ondelete="RESTRICT"), nullable=True
    )
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    storage_state: Mapped[str] = mapped_column(
        ArtifactStorageState, nullable=False, server_default="missing"
    )
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifact_versions.id", ondelete="SET NULL"), nullable=True
    )


class ArtifactCitation(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "artifact_citations"
    __table_args__ = (
        UniqueConstraint(
            "output_artifact_id", "source_artifact_id", "run_id",
            name="uq_artifact_citations_output_source_run",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    output_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    source_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    normalized_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    normalized_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifact_versions.id", ondelete="RESTRICT"), nullable=False
    )
    citation_anchor: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class AuditEvent(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_event_type_occurred_at", "event_type", "occurred_at"),
    )

    sequence_number: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        nullable=False,
        unique=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="CASCADE"), nullable=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TelemetryExportSetting(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "telemetry_export_settings"
    __table_args__ = (
        Index("ix_telemetry_export_settings_scope", "team_id", "project_id", "enabled"),
    )

    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    exporter_type: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    endpoint_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    capture_prompts: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    capture_outputs: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    redaction_policy_evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    configuration_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class ObservabilityRecord(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "observability_records"
    __table_args__ = (
        Index("ix_observability_records_correlation", "correlation_id", "occurred_at"),
        Index("ix_observability_records_trace", "trace_id", "span_id"),
        Index("ix_observability_records_run", "run_id", "occurred_at"),
        Index("ix_observability_records_goal", "goal_id", "occurred_at"),
        Index("ix_observability_records_project", "project_id", "occurred_at"),
        Index("ix_observability_records_team", "team_id", "occurred_at"),
    )

    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_kind: Mapped[str] = mapped_column(ObservabilityEventKind, nullable=False)
    operation_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    audit_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audit_events.id", ondelete="SET NULL"), nullable=True
    )
    cost_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cost_ledger_entries.id", ondelete="SET NULL"), nullable=True
    )
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("approval_requests.id", ondelete="SET NULL"), nullable=True
    )
    approval_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("approval_decisions.id", ondelete="SET NULL"), nullable=True
    )
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True
    )
    artifact_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifact_versions.id", ondelete="SET NULL"), nullable=True
    )
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    mcp_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    sandbox_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    checkpoint_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    capture_policy_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    redaction_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class TelemetryExportAttempt(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "telemetry_export_attempts"
    __table_args__ = (
        UniqueConstraint(
            "observability_record_id", "destination", "attempt_number",
            name="uq_telemetry_export_attempts_record_destination_attempt",
        ),
        CheckConstraint("attempt_number > 0", name="attempt_number_positive"),
        Index("ix_telemetry_export_attempts_status", "status", "created_at"),
    )

    observability_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("observability_records.id", ondelete="CASCADE"), nullable=False
    )
    export_setting_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("telemetry_export_settings.id", ondelete="SET NULL"), nullable=True
    )
    destination: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        TelemetryDeliveryStatus, nullable=False, server_default="pending"
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
