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
  name: string
  created_at: string
}

export interface Goal {
  id: Identifier
  project_id: Identifier
  title: string
  description: string | null
  status: string
  created_at: string
  updated_at: string
}

export interface Agent {
  id: Identifier
  name: string
  visibility: string
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
  name: string
  visibility: string
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
  name: string
  project_id: Identifier | null
  created_at: string
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
  status: string
  created_at: string
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
