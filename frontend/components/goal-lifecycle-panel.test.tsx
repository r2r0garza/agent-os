import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { GoalLifecyclePanel } from "@/components/goal-lifecycle-panel"
import type { Goal } from "@/lib/api"

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
    const handler = routes[`${method} ${path}`]
    if (!handler) {
      throw new Error(`Unhandled request: ${method} ${path}`)
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

function emptyLifecycleRoutes() {
  return {
    "GET /goals/goal-1/lifecycle-commands": () => jsonResponse([]),
    "GET /goals/goal-1/steering-requests": () => jsonResponse([]),
    "GET /goals/goal-1/graph-revisions": () => jsonResponse([]),
    "GET /goals/goal-1/lifecycle-events": () => jsonResponse([]),
  }
}

const baseProps = {
  goalId: "goal-1",
  onRefresh: vi.fn(async () => undefined),
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("GoalLifecyclePanel", () => {
  it("prompts for goal selection when no goal is selected", () => {
    render(<GoalLifecyclePanel goalId="" goal={undefined} onRefresh={baseProps.onRefresh} />)
    expect(
      screen.getByText(/Select a goal to inspect its lifecycle controls/)
    ).toBeInTheDocument()
  })

  it("shows empty states for an active goal with no history yet", async () => {
    installFetchMock(emptyLifecycleRoutes())
    render(<GoalLifecyclePanel {...baseProps} goal={makeGoal()} />)

    await waitFor(() =>
      expect(
        screen.getByText(/No steering instructions have been submitted/)
      ).toBeInTheDocument()
    )
    expect(
      screen.getByText(/No task graph revisions have been recorded/)
    ).toBeInTheDocument()
    expect(
      screen.getByText(/No lifecycle or steering events are recorded/)
    ).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /^Pause$/ })).toBeEnabled()
    expect(screen.getByRole("button", { name: /^Resume$/ })).toBeDisabled()
    expect(screen.getByRole("button", { name: /^Cancel$/ })).toBeEnabled()
  })

  it("pauses an active goal and reports the persisted command", async () => {
    const { calls } = installFetchMock({
      ...emptyLifecycleRoutes(),
      "POST /goals/goal-1/pause": () =>
        jsonResponse(
          {
            id: "command-1",
            goal_id: "goal-1",
            requested_by: "user-1",
            command_type: "pause",
            status: "applied",
            idempotency_key: "key-1",
            reason: null,
            prior_goal_status: "active",
            target_goal_status: "paused",
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
    render(<GoalLifecyclePanel {...baseProps} goal={makeGoal()} />)

    const pauseButton = await screen.findByRole("button", { name: /^Pause$/ })
    await user.click(pauseButton)

    await waitFor(() =>
      expect(
        screen.getByText(/Goal pause requested and persisted/)
      ).toBeInTheDocument()
    )
    expect(
      calls.some((call) => call.method === "POST" && call.path === "/goals/goal-1/pause")
    ).toBe(true)
  })

  it("shows a resume-only control set for a paused goal", async () => {
    installFetchMock(emptyLifecycleRoutes())
    render(
      <GoalLifecyclePanel
        {...baseProps}
        goal={makeGoal({ status: "paused" })}
      />
    )

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Resume$/ })).toBeEnabled()
    )
    expect(screen.getByRole("button", { name: /^Pause$/ })).toBeDisabled()
    expect(screen.getByRole("button", { name: /^Cancel$/ })).toBeEnabled()
  })

  it("disables all lifecycle controls and explains durable cancellation for a cancelled goal", async () => {
    installFetchMock(emptyLifecycleRoutes())
    render(
      <GoalLifecyclePanel
        {...baseProps}
        goal={makeGoal({
          status: "cancelled",
          cancellation_grace_expires_at: "2026-01-03T00:00:00Z",
        })}
      />
    )

    await waitFor(() =>
      expect(screen.getByText(/Cancellation is durable/)).toBeInTheDocument()
    )
    expect(screen.getByRole("button", { name: /^Pause$/ })).toBeDisabled()
    expect(screen.getByRole("button", { name: /^Resume$/ })).toBeDisabled()
    expect(screen.getByRole("button", { name: /^Cancel$/ })).toBeDisabled()
  })

  it("shows a pending badge while a control is being applied by workers", async () => {
    installFetchMock(emptyLifecycleRoutes())
    render(
      <GoalLifecyclePanel
        {...baseProps}
        goal={makeGoal({ pending_control: "pause" })}
      />
    )

    await waitFor(() =>
      expect(screen.getByText(/Applying pause…/)).toBeInTheDocument()
    )
    expect(screen.getByRole("button", { name: /^Pause$/ })).toBeDisabled()
    expect(screen.getByRole("button", { name: /^Resume$/ })).toBeDisabled()
    expect(screen.getByRole("button", { name: /^Cancel$/ })).toBeDisabled()
  })

  it("shows an unauthorized notice and hides lifecycle controls when access is denied", async () => {
    installFetchMock({
      "GET /goals/goal-1/lifecycle-commands": () =>
        jsonResponse({ detail: "goal not found" }, 404),
      "GET /goals/goal-1/steering-requests": () =>
        jsonResponse({ detail: "goal not found" }, 404),
      "GET /goals/goal-1/graph-revisions": () =>
        jsonResponse({ detail: "goal not found" }, 404),
      "GET /goals/goal-1/lifecycle-events": () =>
        jsonResponse({ detail: "goal not found" }, 404),
    })

    render(<GoalLifecyclePanel {...baseProps} goal={makeGoal()} />)

    await waitFor(() =>
      expect(
        screen.getByText(/You do not have access to this goal/)
      ).toBeInTheDocument()
    )
    expect(
      screen.queryByRole("button", { name: /^Pause$/ })
    ).not.toBeInTheDocument()
  })

  it("shows a degraded error banner with retry when the backend is unavailable", async () => {
    installFetchMock({
      "GET /goals/goal-1/lifecycle-commands": () =>
        jsonResponse({ error: "upstream unavailable" }, 502),
      "GET /goals/goal-1/steering-requests": () =>
        jsonResponse({ error: "upstream unavailable" }, 502),
      "GET /goals/goal-1/graph-revisions": () =>
        jsonResponse({ error: "upstream unavailable" }, 502),
      "GET /goals/goal-1/lifecycle-events": () =>
        jsonResponse({ error: "upstream unavailable" }, 502),
    })

    render(<GoalLifecyclePanel {...baseProps} goal={makeGoal()} />)

    await waitFor(() =>
      expect(screen.getByText(/upstream unavailable/)).toBeInTheDocument()
    )
    expect(screen.getByRole("button", { name: /Retry/ })).toBeInTheDocument()
  })

  it("submits a steering instruction and lists it as requested", async () => {
    installFetchMock({
      ...emptyLifecycleRoutes(),
      "POST /goals/goal-1/steer": () =>
        jsonResponse(
          {
            id: "steer-1",
            goal_id: "goal-1",
            requested_by: "user-1",
            status: "requested",
            idempotency_key: "key-2",
            instruction: "Add a review step before publishing",
            base_revision_number: 0,
            applied_revision_number: null,
            resolved_at: null,
            evidence: {},
            created_at: "2026-01-02T00:00:00Z",
          },
          201
        ),
      "GET /goals/goal-1/steering-requests-after": () => jsonResponse([]),
    })

    const user = userEvent.setup()
    render(<GoalLifecyclePanel {...baseProps} goal={makeGoal()} />)

    const instructionField = await screen.findByPlaceholderText(
      /Describe how the remaining work should change/
    )
    await user.type(instructionField, "Add a review step before publishing")
    await user.click(
      screen.getByRole("button", { name: /Submit steering instruction/ })
    )

    await waitFor(() =>
      expect(
        screen.getByText(/Steering instruction submitted and persisted/)
      ).toBeInTheDocument()
    )
  })
})
