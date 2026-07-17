import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ConcurrentWorkspacePanel } from "@/components/concurrent-workspace-panel"
import type { Goal, Run, Task, WorkspaceConflict } from "@/lib/api"

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

interface RouteTable {
  [key: string]: (url: URL) => Response | Promise<Response>
}

function installFetchMock(routes: RouteTable) {
  const calls: { method: string; path: string }[] = []
  const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost")
    const method = (init?.method ?? "GET").toUpperCase()
    const path = url.pathname.replace(/^\/api\/agentic/, "")
    calls.push({ method, path })
    const key = `${method} ${path}`
    const handler = routes[key]
    if (!handler) {
      throw new Error(`Unhandled request: ${key}`)
    }
    return handler(url)
  })
  vi.stubGlobal("fetch", fetchMock)
  return { fetchMock, calls }
}

function makeGoal(overrides: Partial<Goal> = {}): Goal {
  return {
    id: "goal-1",
    project_id: "project-1",
    created_by: "user-1",
    title: "Ship the report",
    description: null,
    status: "active",
    control_version: 0,
    pending_control: null,
    control_requested_by: null,
    control_requested_at: null,
    cancellation_grace_expires_at: null,
    forced_termination_requested_at: null,
    forced_termination_completed_at: null,
    active_graph_revision_number: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  }
}

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    goal_id: "goal-1",
    title: "Draft outline",
    description: null,
    status: "running",
    required_capabilities: {},
    capability_rationale: {},
    expected_outputs: [],
    resource_intent: [{ resource_key: "doc/outline", intent: "write" }],
    policy_ids: [],
    budget_id: null,
    assigned_agent_version_id: null,
    assignment_status: "assigned",
    assignment_candidates: [],
    assignment_rationale: {},
    assignment_updated_at: null,
    lease_owner: "worker-1",
    lease_token: 1,
    lease_expires_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  }
}

function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    id: "run-1",
    task_id: "task-1",
    attempt_number: 1,
    idempotency_key: "key-1",
    agent_version_id: "agent-version-1",
    langgraph_thread_id: null,
    status: "running",
    snapshot: {},
    started_at: "2026-01-01T00:00:00Z",
    completed_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  }
}

function makeConflict(overrides: Partial<WorkspaceConflict> = {}): WorkspaceConflict {
  return {
    project_id: "project-1",
    task_id: "task-1",
    run_id: "run-1",
    occurred_at: "2026-01-01T01:00:00Z",
    resources: [
      { resource_key: "doc/outline", expected_revision: 1, actual_revision: 2 },
    ],
    ...overrides,
  }
}

const baseProps = {
  projectId: "project-1",
  onRefresh: vi.fn(async () => undefined),
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("ConcurrentWorkspacePanel", () => {
  it("shows a single-goal empty state and does not fetch admin lease data", async () => {
    const { calls } = installFetchMock({
      "GET /goals/goal-1/task-graph": () =>
        jsonResponse({ tasks: [], dependencies: [] }),
      "GET /projects/project-1/workspace/conflicts": () => jsonResponse([]),
    })

    render(
      <ConcurrentWorkspacePanel
        {...baseProps}
        goals={[makeGoal()]}
      />
    )

    await waitFor(() =>
      expect(
        screen.getByText(/Only one goal is currently active/)
      ).toBeInTheDocument()
    )
    expect(
      screen.getByText(/No workspace conflicts have been detected/)
    ).toBeInTheDocument()
    expect(
      calls.some((call) => call.path.includes("/admin/workspace"))
    ).toBe(false)
    expect(
      calls.some((call) => call.path.includes("/workspace/leases"))
    ).toBe(false)
  })

  it("shows goals side by side with resource intent when multiple goals are active", async () => {
    installFetchMock({
      "GET /goals/goal-1/task-graph": () =>
        jsonResponse({ tasks: [makeTask()], dependencies: [] }),
      "GET /goals/goal-2/task-graph": () =>
        jsonResponse({
          tasks: [
            makeTask({
              id: "task-2",
              goal_id: "goal-2",
              resource_intent: [{ resource_key: "doc/summary", intent: "read" }],
            }),
          ],
          dependencies: [],
        }),
      "GET /tasks/task-1/runs": () => jsonResponse([makeRun()]),
      "GET /tasks/task-2/runs": () =>
        jsonResponse([makeRun({ id: "run-2", task_id: "task-2", status: "completed" })]),
      "GET /projects/project-1/workspace/conflicts": () => jsonResponse([]),
    })

    render(
      <ConcurrentWorkspacePanel
        {...baseProps}
        goals={[makeGoal(), makeGoal({ id: "goal-2", title: "Second goal" })]}
      />
    )

    await waitFor(() =>
      expect(screen.getByText("Ship the report")).toBeInTheDocument()
    )
    expect(screen.getByText("Second goal")).toBeInTheDocument()
    expect(screen.getByText("doc/outline (write)")).toBeInTheDocument()
    expect(screen.getByText("doc/summary (read)")).toBeInTheDocument()
  })

  it("shows a conflict notification with resolution options", async () => {
    installFetchMock({
      "GET /goals/goal-1/task-graph": () =>
        jsonResponse({ tasks: [makeTask()], dependencies: [] }),
      "GET /tasks/task-1/runs": () => jsonResponse([makeRun()]),
      "GET /projects/project-1/workspace/conflicts": () =>
        jsonResponse([makeConflict()]),
    })

    render(<ConcurrentWorkspacePanel {...baseProps} goals={[makeGoal()]} />)

    await waitFor(() =>
      expect(screen.getByText(/Workspace conflict detected/)).toBeInTheDocument()
    )
    expect(screen.getByText(/expected 1 → actual 2/)).toBeInTheDocument()
    expect(
      screen.getByRole("button", { name: /Discard conflicting run/ })
    ).toBeEnabled()
    expect(
      screen.getByRole("button", { name: /Retry from safe revision/ })
    ).toBeDisabled()
    expect(screen.getByRole("button", { name: /Manual resolution/ })).toBeDisabled()
  })

  it("discards a conflicting run and refreshes state without a page reload", async () => {
    const onRefresh = vi.fn(async () => undefined)
    const { calls } = installFetchMock({
      "GET /goals/goal-1/task-graph": () =>
        jsonResponse({ tasks: [makeTask()], dependencies: [] }),
      "GET /tasks/task-1/runs": () => jsonResponse([makeRun()]),
      "GET /projects/project-1/workspace/conflicts": () =>
        jsonResponse([makeConflict()]),
      "POST /goals/goal-1/cancel": () =>
        jsonResponse(
          {
            id: "command-1",
            goal_id: "goal-1",
            requested_by: "user-1",
            command_type: "cancel",
            status: "applied",
            idempotency_key: "key-1",
            reason: null,
            prior_goal_status: "active",
            target_goal_status: "cancelled",
            cancellation_grace_expires_at: null,
            forced_termination_requested_at: null,
            forced_termination_completed_at: null,
            applied_at: "2026-01-02T00:00:00Z",
            evidence: {},
            created_at: "2026-01-02T00:00:00Z",
          },
          201
        ),
    })

    const user = userEvent.setup()
    render(
      <ConcurrentWorkspacePanel
        projectId="project-1"
        goals={[makeGoal()]}
        onRefresh={onRefresh}
      />
    )

    const discardButton = await screen.findByRole("button", {
      name: /Discard conflicting run/,
    })
    await user.click(discardButton)

    await waitFor(() =>
      expect(screen.getByText(/Conflicting run discarded/)).toBeInTheDocument()
    )
    expect(
      calls.some((call) => call.method === "POST" && call.path === "/goals/goal-1/cancel")
    ).toBe(true)
    expect(onRefresh).toHaveBeenCalled()
  })

  it("retries from a safe revision once the goal is paused", async () => {
    installFetchMock({
      "GET /goals/goal-1/task-graph": () =>
        jsonResponse({ tasks: [makeTask()], dependencies: [] }),
      "GET /tasks/task-1/runs": () => jsonResponse([makeRun()]),
      "GET /projects/project-1/workspace/conflicts": () =>
        jsonResponse([makeConflict()]),
      "POST /goals/goal-1/resume": () =>
        jsonResponse(
          {
            id: "command-2",
            goal_id: "goal-1",
            requested_by: "user-1",
            command_type: "resume",
            status: "applied",
            idempotency_key: "key-2",
            reason: null,
            prior_goal_status: "paused",
            target_goal_status: "active",
            cancellation_grace_expires_at: null,
            forced_termination_requested_at: null,
            forced_termination_completed_at: null,
            applied_at: "2026-01-02T00:00:00Z",
            evidence: {},
            created_at: "2026-01-02T00:00:00Z",
          },
          201
        ),
    })

    const user = userEvent.setup()
    render(
      <ConcurrentWorkspacePanel
        {...baseProps}
        goals={[makeGoal({ status: "paused" })]}
      />
    )

    const retryButton = await screen.findByRole("button", {
      name: /Retry from safe revision/,
    })
    expect(retryButton).toBeEnabled()
    await user.click(retryButton)

    await waitFor(() =>
      expect(screen.getByText(/Goal resumed/)).toBeInTheDocument()
    )
  })

  it("shows a degraded error banner with retry when the backend is unavailable", async () => {
    installFetchMock({
      "GET /projects/project-1/workspace/conflicts": () =>
        jsonResponse({ error: "upstream unavailable" }, 502),
    })

    render(<ConcurrentWorkspacePanel {...baseProps} goals={[]} />)

    await waitFor(() =>
      expect(screen.getByText(/upstream unavailable/)).toBeInTheDocument()
    )
    expect(screen.getByRole("button", { name: /Retry/ })).toBeInTheDocument()
  })
})
