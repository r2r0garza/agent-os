export type Identifier = string

export interface ModelProfile {
  id: Identifier
  name: string
  base_url: string
  model_identifier: string
  created_at: string
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

export interface Skill {
  id: Identifier
  name: string
  visibility: string
  created_at: string
}

export interface McpServer {
  id: Identifier
  name: string
  project_id: Identifier | null
  created_at: string
}

export interface Task {
  id: Identifier
  goal_id: Identifier
  title: string
  description: string | null
  status: string
  created_at: string
  updated_at: string
}

export interface Run {
  id: Identifier
  task_id: Identifier
  attempt_number: number
  status: string
  snapshot: Record<string, unknown>
  started_at: string | null
  completed_at: string | null
  created_at: string
}

export interface Artifact {
  id: Identifier
  goal_id: Identifier | null
  run_id: Identifier | null
  name: string
  created_at: string
}

export interface AuditEvent {
  id: Identifier
  sequence_number: number
  goal_id: Identifier | null
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

export function jsonBody(value: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(value) }
}
