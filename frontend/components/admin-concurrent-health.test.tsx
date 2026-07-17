import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { AdminConcurrentHealth } from "@/components/admin-concurrent-health"
import type { Project } from "@/lib/api"

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

function installFetchMock(
  routes: Record<string, () => Response | Promise<Response>>
) {
  const calls: string[] = []
  const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost")
    const method = (init?.method ?? "GET").toUpperCase()
    const path = url.pathname.replace(/^\/api\/agentic/, "")
    const key = `${method} ${path}`
    calls.push(key)
    const handler = routes[key]
    if (!handler) throw new Error(`Unhandled request: ${key}`)
    return handler()
  })
  vi.stubGlobal("fetch", fetchMock)
  return calls
}

const projects: Project[] = [
  {
    id: "project-1",
    team_id: "team-1",
    created_by: "user-1",
    name: "Alpha",
    created_at: "2026-01-01T00:00:00Z",
  },
]

const healthyRoutes = {
  "GET /admin/workspace/leases": () =>
    jsonResponse([
      {
        project_id: "project-1",
        task_id: "task-1",
        run_id: "run-12345678",
        resource_key: "docs/report",
        owner: "worker-a",
        fencing_token: 3,
        fencing_status: "current",
        expected_revision: 2,
        current_revision: 2,
        expires_at: "2026-01-01T01:00:00Z",
        state: "active",
      },
    ]),
  "GET /admin/workspace/conflicts": () => jsonResponse([]),
  "GET /admin/workspace/promotions": () =>
    jsonResponse([
      {
        project_id: "project-1",
        task_id: "task-1",
        run_id: "run-12345678",
        status: "promoted",
        occurred_at: "2026-01-01T00:30:00Z",
        resource_deltas: [
          {
            resource_key: "docs/report",
            previous_revision: 1,
            resulting_revision: 2,
            revision_increment: 1,
          },
        ],
      },
    ]),
  "GET /audit-events": () =>
    jsonResponse([
      {
        id: "event-1",
        sequence_number: 1,
        project_id: "project-1",
        goal_id: "goal-1",
        task_id: "task-1",
        run_id: null,
        event_type: "workspace.lease_acquired",
        payload: {
          worker_id: "worker-a",
          resource_key: "docs/report",
          fencing_token: 3,
        },
        occurred_at: "2026-01-01T00:20:00Z",
      },
    ]),
  "GET /projects/project-1/goals": () =>
    jsonResponse([
      {
        id: "goal-1",
        project_id: "project-1",
        created_by: "user-1",
        title: "First",
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
      },
      {
        id: "goal-2",
        project_id: "project-1",
        created_by: "user-1",
        title: "Second",
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
      },
    ]),
  "GET /goals/goal-1/task-graph": () =>
    jsonResponse({
      tasks: [
        { resource_intent: [{ resource_key: "docs/report", intent: "write" }] },
      ],
      dependencies: [],
    }),
  "GET /goals/goal-2/task-graph": () =>
    jsonResponse({
      tasks: [
        { resource_intent: [{ resource_key: "docs/report", intent: "write" }] },
      ],
      dependencies: [],
    }),
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("AdminConcurrentHealth", () => {
  it("shows healthy workers, fence evidence, and cross-goal overlap risk", async () => {
    installFetchMock(healthyRoutes)

    render(<AdminConcurrentHealth projects={projects} />)

    await waitFor(() =>
      expect(screen.getByText(/Workspace health is clear/)).toBeInTheDocument()
    )
    expect(screen.getByText("worker-a")).toBeInTheDocument()
    expect(screen.getByText(/Fence token 3/)).toBeInTheDocument()
    expect(screen.getByText(/Overlap risk: docs\/report/)).toBeInTheDocument()
    expect(screen.getByText(/Promotion promoted/)).toBeInTheDocument()
  })

  it("shows degraded lease and conflict evidence with affected run drill-down", async () => {
    installFetchMock({
      ...healthyRoutes,
      "GET /admin/workspace/leases": () =>
        jsonResponse([
          {
            project_id: "project-1",
            task_id: "task-1",
            run_id: "run-12345678",
            resource_key: "docs/report",
            owner: "worker-stale",
            fencing_token: 2,
            fencing_status: "superseded",
            expected_revision: 1,
            current_revision: 2,
            expires_at: "2026-01-01T00:00:00Z",
            state: "fenced",
          },
        ]),
      "GET /admin/workspace/conflicts": () =>
        jsonResponse([
          {
            project_id: "project-1",
            task_id: "task-1",
            run_id: "run-12345678",
            occurred_at: "2026-01-01T00:30:00Z",
            resources: [
              {
                resource_key: "docs/report",
                expected_revision: 1,
                actual_revision: 2,
              },
            ],
          },
        ]),
    })

    render(<AdminConcurrentHealth projects={projects} />)

    await waitFor(() =>
      expect(screen.getByText(/Degraded workspace health/)).toBeInTheDocument()
    )
    expect(screen.getByText("worker-stale")).toBeInTheDocument()
    expect(screen.getByText("1 fenced")).toBeInTheDocument()
    expect(screen.getAllByText(/run run-1234/)).not.toHaveLength(0)
    expect(screen.getByText(/docs\/report 1→2/)).toBeInTheDocument()
  })

  it("renders no cross-project view for a regular user", async () => {
    const calls = installFetchMock({
      "GET /admin/workspace/leases": () =>
        jsonResponse({ detail: "admin role required" }, 403),
    })

    const { container } = render(<AdminConcurrentHealth projects={projects} />)

    await waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(calls).toEqual(["GET /admin/workspace/leases"])
  })

  it("shows a retryable error state when workspace health is unavailable", async () => {
    let requests = 0
    installFetchMock({
      ...healthyRoutes,
      "GET /admin/workspace/leases": () => {
        requests += 1
        return requests === 1
          ? jsonResponse({ error: "upstream unavailable" }, 502)
          : healthyRoutes["GET /admin/workspace/leases"]()
      },
    })
    const user = userEvent.setup()

    render(<AdminConcurrentHealth projects={projects} />)

    const retry = await screen.findByRole("button", { name: "Retry" })
    expect(screen.getByText(/upstream unavailable/)).toBeInTheDocument()
    await user.click(retry)
    await waitFor(() =>
      expect(screen.getByText(/Workspace health is clear/)).toBeInTheDocument()
    )
  })
})
