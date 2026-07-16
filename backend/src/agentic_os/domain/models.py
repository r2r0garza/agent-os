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
AgentVisibility = Enum("private", "team", "public", name="visibility", validate_strings=True)
GoalStatus = Enum(
    "draft", "active", "paused", "completed", "cancelled", "failed", name="goal_status", validate_strings=True
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
ArtifactStorageState = Enum(
    "staged", "finalized", "missing", "orphaned", name="artifact_storage_state", validate_strings=True
)


class Team(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "teams"

    name: Mapped[str] = mapped_column(Text, nullable=False)


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


class Goal(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "goals"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(GoalStatus, nullable=False, server_default="draft")


class Task(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tasks"

    goal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(TaskStatus, nullable=False, server_default="pending")
    required_capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    capability_rationale: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    expected_outputs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    resource_intent: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    policy_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
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
    default_budget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budgets.id", ondelete="SET NULL"), nullable=True
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
    __table_args__ = (UniqueConstraint("skill_id", "version_number", name="uq_skill_versions_skill_version"),)

    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_ref: Mapped[str] = mapped_column(Text, nullable=False)
    resource_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class McpServer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        CheckConstraint(
            "team_id IS NOT NULL OR project_id IS NOT NULL", name="owner_scope_required"
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


class Policy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "policies"

    scope_type: Mapped[str] = mapped_column(PolicyScopeType, nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decision: Mapped[str] = mapped_column(PolicyDecision, nullable=False)
    rule: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


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


class CostLedgerEntry(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "cost_ledger_entries"
    __table_args__ = (
        CheckConstraint("reserved_amount_minor_units >= 0", name="reserved_non_negative"),
    )

    budget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budgets.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    reserved_amount_minor_units: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    actual_amount_minor_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    is_zero_cost: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
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
    name: Mapped[str] = mapped_column(Text, nullable=False)


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


class AuditEvent(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "audit_events"

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
