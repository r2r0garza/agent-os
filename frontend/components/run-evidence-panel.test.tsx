import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { RunEvidencePanel } from "@/components/run-evidence-panel"
import type { GovernanceLookups } from "@/components/governance-workspace"
import type { AuditEvent, GovernanceEvidence, Run } from "@/lib/api"

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

function installFetchMock(evidence: GovernanceEvidence) {
  const fetchMock = vi.fn(async () => jsonResponse(evidence))
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    id: "run-1",
    task_id: "task-1",
    attempt_number: 1,
    idempotency_key: "idem-1",
    agent_version_id: "agent-version-1",
    langgraph_thread_id: "agentic-os-task-task-1",
    status: "completed",
    snapshot: {
      agent_version_number: 1,
      model_profile_version_id: "model-version-1",
      enabled_tools: [],
    },
    started_at: "2026-01-01T00:00:00Z",
    completed_at: "2026-01-01T00:00:05Z",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:05Z",
    ...overrides,
  }
}

function makeAuditEvent(overrides: Partial<AuditEvent>): AuditEvent {
  return {
    id: `event-${Math.random()}`,
    sequence_number: 1,
    project_id: "project-1",
    goal_id: "goal-1",
    task_id: "task-1",
    run_id: "run-1",
    event_type: "harness.invocation_started",
    payload: {},
    occurred_at: "2026-01-01T00:00:01Z",
    ...overrides,
  }
}

function emptyEvidence(overrides: Partial<GovernanceEvidence> = {}): GovernanceEvidence {
  return {
    approval_requests: [],
    approval_decisions: [],
    admin_overrides: [],
    budget_reservations: [],
    cost_ledger_entries: [],
    audit_events: [],
    ...overrides,
  }
}

const lookups: GovernanceLookups = {
  skillVersionName: {},
  mcpVersionName: {},
  modelProfileVersionName: { "model-version-1": "Primary GPT · v1" },
  policySetVersionName: {},
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("RunEvidencePanel model invocation evidence", () => {
  it("shows completed model call evidence with token usage and tool rounds", async () => {
    installFetchMock(
      emptyEvidence({
        audit_events: [
          makeAuditEvent({
            event_type: "harness.invocation_started",
            payload: {
              model_identifier: "gpt-5-mini",
              endpoint: "https://api.openai.com",
              thread_id: "agentic-os-task-task-1",
            },
          }),
          makeAuditEvent({
            event_type: "harness.invocation_completed",
            payload: {
              attempts: 1,
              tool_rounds: 1,
              finish_reason: "stop",
              usage: { prompt_tokens: 12, completion_tokens: 8, total_tokens: 20 },
            },
          }),
        ],
      })
    )

    const user = userEvent.setup()
    render(<RunEvidencePanel run={makeRun()} agents={[]} lookups={lookups} />)

    await user.click(
      screen.getByRole("button", { name: /View pinned snapshot & evidence/ })
    )

    await waitFor(() =>
      expect(screen.getByText(/Model invocation evidence/)).toBeInTheDocument()
    )
    expect(screen.getByText(/model call started · gpt-5-mini/)).toBeInTheDocument()
    expect(
      screen.getByText(/model call completed · 1 attempt\(s\) · 1 tool round\(s\)/)
    ).toBeInTheDocument()
    expect(screen.getByText(/prompt tokens: 12/)).toBeInTheDocument()
  })

  it("surfaces a failed model invocation with its diagnostic", async () => {
    installFetchMock(
      emptyEvidence({
        audit_events: [
          makeAuditEvent({
            event_type: "harness.invocation_failed",
            payload: { attempts: 2, diagnostic: "timeout" },
          }),
        ],
      })
    )

    const user = userEvent.setup()
    render(<RunEvidencePanel run={makeRun()} agents={[]} lookups={lookups} />)

    await user.click(
      screen.getByRole("button", { name: /View pinned snapshot & evidence/ })
    )

    expect(
      await screen.findByText(/model call failed · timeout/)
    ).toBeInTheDocument()
    expect(screen.getByText(/2 attempt\(s\)/)).toBeInTheDocument()
  })

  it("surfaces a capability check failure blocking invocation", async () => {
    installFetchMock(
      emptyEvidence({
        audit_events: [
          makeAuditEvent({
            event_type: "harness.capability_check_failed",
            payload: {
              failures: [
                {
                  capability: "tool_calls",
                  status: "unsupported",
                  diagnostic: "provider returned HTTP 404",
                },
              ],
            },
          }),
        ],
      })
    )

    const user = userEvent.setup()
    render(<RunEvidencePanel run={makeRun()} agents={[]} lookups={lookups} />)

    await user.click(
      screen.getByRole("button", { name: /View pinned snapshot & evidence/ })
    )

    expect(
      await screen.findByText(/blocked before invocation/)
    ).toBeInTheDocument()
    expect(screen.getByText(/tool_calls: unsupported/)).toBeInTheDocument()
  })

  it("shows an empty state when a model profile is pinned but has no invocation evidence yet", async () => {
    installFetchMock(emptyEvidence())

    const user = userEvent.setup()
    render(<RunEvidencePanel run={makeRun()} agents={[]} lookups={lookups} />)

    await user.click(
      screen.getByRole("button", { name: /View pinned snapshot & evidence/ })
    )

    expect(
      await screen.findByText(
        /No model invocation evidence recorded for this run yet/
      )
    ).toBeInTheDocument()
  })

  it("omits the model invocation section when no model profile is pinned", async () => {
    installFetchMock(emptyEvidence())

    const user = userEvent.setup()
    render(
      <RunEvidencePanel
        run={makeRun({ snapshot: { agent_version_number: 1, enabled_tools: [] } })}
        agents={[]}
        lookups={lookups}
      />
    )

    await user.click(
      screen.getByRole("button", { name: /View pinned snapshot & evidence/ })
    )

    await waitFor(() =>
      expect(
        screen.getByText(/No cost ledger entries recorded for this run yet/)
      ).toBeInTheDocument()
    )
    expect(screen.queryByText(/Model invocation evidence/)).not.toBeInTheDocument()
  })
})
