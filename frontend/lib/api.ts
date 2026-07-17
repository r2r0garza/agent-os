export type Identifier = string

export interface ModelProfile {
  id: Identifier
  name: string
  base_url: string
  model_identifier: string
  capability_metadata: Record<string, unknown>
  pricing_metadata: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface ModelProfileVersion {
  id: Identifier
  model_profile_id: Identifier
  version_number: number
  base_url: string
  model_identifier: string
  credential_id: Identifier | null
  headers: Record<string, unknown>
  capability_metadata: Record<string, unknown>
  pricing_metadata: Record<string, unknown>
  created_at: string
}

export interface Credential {
  id: Identifier
  team_id: Identifier | null
  project_id: Identifier | null
  name: string
  credential_type: string
  metadata: Record<string, unknown>
  configured: boolean
  created_at: string
  updated_at: string
}

export interface PolicySet {
  id: Identifier
  team_id: Identifier | null
  project_id: Identifier | null
  name: string
  created_at: string
  updated_at: string
}

export interface PolicySetVersion {
  id: Identifier
  policy_set_id: Identifier
  version_number: number
  rules: Record<string, unknown>[]
  created_at: string
}

export interface Budget {
  id: Identifier
  agent_id: Identifier
  currency: string
  amount_minor_units: number
  enforcement_mode: string
  warning_threshold_percent: number | null
  created_at: string
  updated_at: string
}

export interface Project {
  id: Identifier
  team_id: Identifier
  created_by: Identifier
  name: string
  created_at: string
  updated_at?: string
}

export interface ProjectMember {
  id: Identifier
  project_id: Identifier
  user_id: Identifier
  granted_by: Identifier | null
  created_at: string
  user_email: string
  user_display_name: string
}

export interface Team {
  id: Identifier
  name: string
  created_at: string
  updated_at: string
}

export interface TeamMembership {
  id: Identifier
  team_id: Identifier
  user_id: Identifier
  role: "owner" | "member"
  created_at: string
  user_email: string
  user_display_name: string
}

export interface UserAccount {
  id: Identifier
  email: string
  display_name: string
  role: "admin" | "regular_user"
  created_at: string
}

export interface Goal {
  id: Identifier
  project_id: Identifier
  created_by: Identifier
  title: string
  description: string | null
  status: string
  control_version: number
  pending_control: string | null
  control_requested_by: Identifier | null
  control_requested_at: string | null
  cancellation_grace_expires_at: string | null
  forced_termination_requested_at: string | null
  forced_termination_completed_at: string | null
  active_graph_revision_number: number
  created_at: string
  updated_at: string
}

export interface GoalLifecycleCommand {
  id: Identifier
  goal_id: Identifier
  requested_by: Identifier | null
  command_type: string
  status: string
  idempotency_key: string
  reason: string | null
  prior_goal_status: string | null
  target_goal_status: string | null
  cancellation_grace_expires_at: string | null
  forced_termination_requested_at: string | null
  forced_termination_completed_at: string | null
  applied_at: string | null
  evidence: Record<string, unknown>
  created_at: string
}

export interface GoalSteeringRequest {
  id: Identifier
  goal_id: Identifier
  requested_by: Identifier | null
  status: string
  idempotency_key: string
  instruction: string
  base_revision_number: number
  applied_revision_number: number | null
  resolved_at: string | null
  evidence: Record<string, unknown>
  created_at: string
}

export interface TaskGraphRevisionTaskEntry {
  revision_id: Identifier
  task_id: Identifier
  change_type: "unchanged" | "added" | "revised" | "superseded"
  supersedes_task_id: Identifier | null
  task_snapshot: Record<string, unknown>
}

export interface TaskGraphRevision {
  id: Identifier
  goal_id: Identifier
  created_by: Identifier | null
  steering_request_id: Identifier | null
  revision_number: number
  parent_revision_number: number | null
  change_summary: string | null
  graph_snapshot: Record<string, unknown>
  assignment_evidence: Record<string, unknown>
  policy_context: Record<string, unknown>
  budget_context: Record<string, unknown>
  created_at: string
}

export interface TaskGraphRevisionDetail extends TaskGraphRevision {
  tasks: TaskGraphRevisionTaskEntry[]
}

export interface GoalLifecycleEvent {
  id: Identifier
  sequence_number: number
  goal_id: Identifier
  actor_id: Identifier | null
  lifecycle_command_id: Identifier | null
  steering_request_id: Identifier | null
  graph_revision_id: Identifier | null
  event_type: string
  prior_goal_status: string | null
  resulting_goal_status: string | null
  payload: Record<string, unknown>
  occurred_at: string
}

export interface SteeringTaskSpecInput {
  client_id: string
  title: string
  description?: string | null
  required_capabilities?: Record<string, unknown>
  capability_rationale?: Record<string, unknown>
  expected_outputs?: unknown[]
  resource_intent?: unknown[]
  policy_ids?: string[] | null
  budget_id?: string | null
  depends_on?: string[] | null
}

export interface SteeringTaskChangeInput {
  change_type: "added" | "revised" | "superseded"
  task_id?: string | null
  task?: SteeringTaskSpecInput | null
}

export interface Agent {
  id: Identifier
  team_id: Identifier
  created_by: Identifier
  name: string
  visibility: "private" | "team" | "public"
  created_at: string
  updated_at?: string
}

export interface AgentInstallation {
  id: Identifier
  installed_agent_id: Identifier
  source_agent_version_id: Identifier
  installed_by: Identifier
  created_at: string
}

export interface VersionAttachment {
  version_id: Identifier
  config: Record<string, unknown>
}

export interface AgentVersion {
  id: Identifier
  agent_id: Identifier
  version_number: number
  instructions: string | null
  capability_manifest: Record<string, unknown>
  model_profile_id: Identifier | null
  model_profile_version_id: Identifier | null
  default_budget_id: Identifier | null
  skill_attachments: VersionAttachment[]
  mcp_server_attachments: VersionAttachment[]
  policy_set_version_ids: Identifier[]
  created_at: string
}

export interface Skill {
  id: Identifier
  team_id: Identifier
  created_by: Identifier
  name: string
  visibility: "private" | "team" | "public"
  created_at: string
  updated_at?: string
}

export interface SkillInstallation {
  id: Identifier
  installed_skill_id: Identifier
  source_skill_version_id: Identifier
  installed_by: Identifier
  created_at: string
}

export interface SkillVersion {
  id: Identifier
  skill_id: Identifier
  version_number: number
  content_ref: string
  resource_metadata: Record<string, unknown>
  created_at: string
}

export interface McpServer {
  id: Identifier
  team_id: Identifier | null
  project_id: Identifier | null
  name: string
  visibility: "private" | "team" | "public"
  created_at: string
  updated_at?: string
}

export interface McpServerVersion {
  id: Identifier
  mcp_server_id: Identifier
  version_number: number
  connection_config: Record<string, unknown>
  credential_configured: boolean
  credential_id: Identifier | null
  created_at: string
}

export interface McpServerAttachment {
  id: Identifier
  mcp_server_version_id: Identifier
  team_id: Identifier | null
  project_id: Identifier | null
  agent_id: Identifier | null
  credential_configured: boolean
  revoked: boolean
  created_at: string
}

export interface AssignmentCandidate {
  agent_id: Identifier
  agent_version_id: Identifier
  agent_version_number: number
  eligible: boolean
  matched_capabilities: string[]
  missing_capabilities: string[]
  policy_decision: string
  budget_id: Identifier | null
  rejection_reasons: string[]
}

export interface Task {
  id: Identifier
  goal_id: Identifier
  title: string
  description: string | null
  status: string
  required_capabilities: Record<string, unknown>
  capability_rationale: Record<string, unknown>
  expected_outputs: unknown[]
  resource_intent: { resource_key: string; intent: string }[]
  policy_ids: string[]
  budget_id: Identifier | null
  assigned_agent_version_id: Identifier | null
  assignment_status: string
  assignment_candidates: AssignmentCandidate[]
  assignment_rationale: Record<string, unknown>
  assignment_updated_at: string | null
  lease_owner: string | null
  lease_token: number
  lease_expires_at: string | null
  created_at: string
  updated_at: string
}

export interface TaskDependency {
  task_id: Identifier
  depends_on_task_id: Identifier
}

export interface TaskGraph {
  tasks: Task[]
  dependencies: TaskDependency[]
}

export interface RunSnapshot {
  configuration_snapshot_id?: Identifier
  agent_id?: Identifier
  agent_version_id?: Identifier
  agent_version_number?: number
  model_profile_version_id?: Identifier | null
  default_budget_id?: Identifier | null
  skill_version_ids?: Identifier[]
  skill_version_id?: Identifier | null
  mcp_server_version_ids?: Identifier[]
  mcp_server_version_id?: Identifier | null
  enabled_tools?: string[]
  policy_decision?: string
  policy_evaluations?: Record<string, unknown>[]
  assignment_rationale?: Record<string, unknown>
  capability_manifest?: Record<string, unknown>
}

export interface Run {
  id: Identifier
  task_id: Identifier
  attempt_number: number
  idempotency_key: string
  agent_version_id: Identifier
  langgraph_thread_id: string | null
  status: string
  snapshot: RunSnapshot
  started_at: string | null
  completed_at: string | null
  created_at: string
  updated_at: string
}

export interface Artifact {
  id: Identifier
  project_id: Identifier
  goal_id: Identifier | null
  task_id: Identifier | null
  run_id: Identifier | null
  created_by: Identifier | null
  parent_artifact_id: Identifier | null
  name: string
  kind: "source" | "normalized" | "output"
  content_type: string | null
  ingestion_status: string
  ingestion_metadata: Record<string, unknown>
  ingestion_error: string | null
  created_at: string
  latest_version: ArtifactVersion | null
}

export interface ArtifactVersion {
  id: Identifier
  artifact_id: Identifier
  version_number: number
  content_hash: string
  size_bytes: number
  storage_state: string
  previous_version_id: Identifier | null
  created_at: string
}

export interface ArtifactLineage {
  artifact: Artifact
  parent: Artifact | null
  children: Artifact[]
}

export interface ArtifactCitation {
  source_artifact_id: Identifier
  normalized_artifact_id: Identifier
  citation_anchor: Record<string, unknown>
}

export interface AuditEvent {
  id: Identifier
  sequence_number: number
  project_id: Identifier | null
  goal_id: Identifier | null
  task_id: Identifier | null
  run_id: Identifier | null
  event_type: string
  payload: Record<string, unknown>
  occurred_at: string
}

export interface CostLedgerEntry {
  id: Identifier
  run_id: Identifier | null
  action_type: string
  reserved_amount_minor_units: number
  actual_amount_minor_units: number | null
  currency: string
  is_zero_cost: boolean
  is_unpriced?: boolean
  warning_triggered?: boolean
  hard_stop_triggered?: boolean
  evidence?: Record<string, unknown>
  status: string
  created_at: string
}

export interface ApprovalRequest {
  id: Identifier
  project_id: Identifier
  goal_id: Identifier
  task_id: Identifier
  run_id: Identifier
  agent_version_id: Identifier
  configuration_id: Identifier | null
  requested_by: Identifier | null
  mode: string
  status: "pending" | "approved" | "denied" | "expired" | "cancelled"
  action_type: string
  action_preview: Record<string, unknown>
  policy_version_ids: Identifier[]
  policy_evidence: Record<string, unknown>
  expires_at: string | null
  resolved_at: string | null
  created_at: string
  updated_at: string
}

export interface ApprovalDecision {
  id: Identifier
  approval_request_id: Identifier
  decision: string
  actor_id: Identifier | null
  reason: string | null
  context: Record<string, unknown>
  evaluated_policy_version_ids: Identifier[]
  created_at: string
}

export interface AdminOverride {
  id: Identifier
  project_id: Identifier | null
  goal_id: Identifier | null
  task_id: Identifier | null
  run_id: Identifier | null
  created_by: Identifier
  scope_type: "project" | "goal" | "task" | "run"
  scope_id: Identifier
  reason: string
  starts_at: string
  expires_at: string
  evaluated_policy_version_ids: Identifier[]
  context: Record<string, unknown>
  created_at: string
}

export interface BudgetReservation {
  id: Identifier
  budget_id: Identifier
  project_id: Identifier
  goal_id: Identifier
  task_id: Identifier
  run_id: Identifier
  action_type: string
  amount_minor_units: number
  currency: string
  status: string
  is_unpriced: boolean
  warning_triggered: boolean
  hard_stop_triggered: boolean
  pricing_evidence: Record<string, unknown>
  policy_version_ids: Identifier[]
  reconciled_at: string | null
  created_at: string
  updated_at: string
}

export interface GovernanceEvidence {
  approval_requests: ApprovalRequest[]
  approval_decisions: ApprovalDecision[]
  admin_overrides: AdminOverride[]
  budget_reservations: BudgetReservation[]
  cost_ledger_entries: CostLedgerEntry[]
  audit_events: AuditEvent[]
}

export interface TelemetryAttempt {
  id: Identifier
  observability_record_id: Identifier
  destination: string
  attempt_number: number
  status: string
  last_attempted_at: string | null
  delivered_at: string | null
  retry_after: string | null
  failure_code: string | null
  failure_message: string | null
  delivery_evidence: Record<string, unknown>
  created_at: string
}

export interface ObservabilityRecord {
  id: Identifier
  correlation_id: Identifier
  request_id: Identifier | null
  trace_id: string | null
  span_id: string | null
  parent_span_id: string | null
  event_kind: string
  operation_name: string
  status: string | null
  occurred_at: string
  team_id: Identifier | null
  user_id: Identifier | null
  project_id: Identifier | null
  goal_id: Identifier | null
  task_id: Identifier | null
  run_id: Identifier | null
  audit_event_id: Identifier | null
  cost_ledger_entry_id: Identifier | null
  approval_request_id: Identifier | null
  approval_decision_id: Identifier | null
  artifact_id: Identifier | null
  artifact_version_id: Identifier | null
  model_call_id: Identifier | null
  tool_call_id: Identifier | null
  mcp_call_id: Identifier | null
  sandbox_id: Identifier | null
  checkpoint_id: Identifier | null
  attributes: Record<string, unknown>
  capture_policy_evidence: Record<string, unknown>
  redaction_evidence: Record<string, unknown>
  telemetry_attempts: TelemetryAttempt[]
}

export interface ObservabilityHealth {
  status: string
  checked_at: string
  deployment: {
    status: string
    checks: Record<string, { status: string; detail: string }>
  }
  maintenance: {
    events: Array<{
      id: Identifier
      event_type: string
      occurred_at: string
      evidence: Record<string, unknown>
    }>
    commands: {
      setup_check: string
      migration_status: string
      backup: string
      restore: string
      upgrade_preflight: string
    }
  }
  database: { status: string; latency_ms: number }
  queues: {
    status: string
    depth: number
    tasks_by_status: Record<string, number>
  }
  workers: {
    status: string
    active: number
    stale: number
    stale_worker_ids: string[]
    stale_task_ids: Identifier[]
    lease_count: number
    retry_count: number
    failure_count: number
  }
  sandbox: {
    status: string
    runtimes: Record<string, { status: string; reason: string | null }>
  }
  event_stream: {
    status: string
    latest_record_at: string | null
    latest_record_age_seconds: number | null
    latest_correlation_id: Identifier | null
    deliveries_by_status: Record<string, number>
    oldest_queued_delivery_at: string | null
    delivery_delay_seconds: number | null
  }
  telemetry: {
    status: string
    deliveries_by_status: Record<string, number>
    exporters: Array<{
      id: Identifier
      exporter_type: string
      enabled: boolean
      configured: boolean
      capture_prompts: boolean
      capture_outputs: boolean
      redaction_policy_evidence: Record<string, unknown>
    }>
  }
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message)
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/agentic${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const body = (await response.json()) as {
        detail?: string
        error?: string
      }
      message = body.detail ?? body.error ?? message
    } catch {
      // Preserve the status-based fallback for non-JSON upstream responses.
    }
    throw new ApiError(message, response.status)
  }

  if (response.status === 204) {
    return undefined as T
  }

  return (await response.json()) as T
}

export async function apiText(path: string): Promise<string> {
  const response = await fetch(`/api/agentic${path}`, {
    cache: "no-store",
    headers: { accept: "text/plain, application/json" },
  })

  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const body = (await response.json()) as {
        detail?: string
        error?: string
      }
      message = body.detail ?? body.error ?? message
    } catch {
      // Preserve the status-based fallback for non-JSON upstream responses.
    }
    throw new ApiError(message, response.status)
  }

  return response.text()
}

export function jsonBody(value: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(value) }
}

export function patchBody(value: unknown): RequestInit {
  return { method: "PATCH", body: JSON.stringify(value) }
}

export function deleteInit(): RequestInit {
  return { method: "DELETE" }
}
