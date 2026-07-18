import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { GoalPlanningPanel } from "@/components/goal-planning-panel"
import type {
  Agent,
  GoalPlanningAcceptance,
  GoalPlanningSession,
  Task,
} from "@/lib/api"

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

interface RouteTable {
  [key: string]: (init?: RequestInit) => Response | Promise<Response>
}

function installFetchMock(routes: RouteTable) {
  const calls: Array<{ method: string; path: string; body: unknown }> = []
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost")
      const method = (init?.method ?? "GET").toUpperCase()
      const path = url.pathname.replace(/^\/api\/agentic/, "")
      const body = init?.body ? JSON.parse(String(init.body)) : null
      calls.push({ method, path, body })
      const handler = routes[`${method} ${path}`]
      if (!handler) throw new Error(`Unhandled request: ${method} ${path}`)
      return handler(init)
    })
  )
  return calls
}

const agents: Agent[] = [
  {
    id: "agent-1",
    team_id: "team-1",
    created_by: "user-1",
    name: "Research agent",
    visibility: "team",
    created_at: "2026-07-17T00:00:00Z",
  },
  {
    id: "agent-2",
    team_id: "team-1",
    created_by: "user-1",
    name: "Review agent",
    visibility: "team",
    created_at: "2026-07-17T00:00:00Z",
  },
  {
    id: "agent-3",
    team_id: "team-1",
    created_by: "user-1",
    name: "Rejected agent",
    visibility: "team",
    created_at: "2026-07-17T00:00:00Z",
  },
]

const task: Task = {
  id: "11111111-1111-4111-8111-111111111111",
  goal_id: "goal-1",
  title: "Research the topic",
  description: null,
  status: "pending",
  required_capabilities: { research: true },
  capability_rationale: { research: "Sources required" },
  expected_outputs: [],
  resource_intent: [],
  policy_ids: [],
  budget_id: null,
  assigned_agent_version_id: null,
  assignment_status: "pending",
  assignment_candidates: [],
  assignment_rationale: {},
  assignment_updated_at: null,
  lease_owner: null,
  lease_token: 0,
  lease_expires_at: null,
  created_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:00Z",
}

function makeSession(
  overrides: Partial<GoalPlanningSession> = {}
): GoalPlanningSession {
  return {
    id: "plan-1",
    goal_id: "goal-1",
    revision_number: 1,
    status: "previewed",
    validation_status: "valid",
    constraints_snapshot: {
      required_model_capabilities: ["tool_calling"],
      budget_status: "within_limit",
    },
    requirements: [
      {
        id: "requirement-1",
        capability_key: "research",
        required: true,
        rationale: "Goal needs sources",
        source_evidence: { source: "task" },
      },
    ],
    candidates: [
      {
        id: "candidate-1",
        agent_id: "agent-1",
        agent_version_id: "version-1",
        eligible: true,
        matched_capabilities: ["research"],
        missing_capabilities: [],
        rejection_reasons: [],
        evidence: { selection_rank: 1 },
        constraints_snapshot: {
          policy_decision: "allow",
          budget_id: "[REDACTED]",
          enabled_tools: ["search"],
        },
      },
      {
        id: "candidate-2",
        agent_id: "agent-2",
        agent_version_id: "version-2",
        eligible: true,
        matched_capabilities: ["research"],
        missing_capabilities: [],
        rejection_reasons: [],
        evidence: { selection_rank: 2 },
        constraints_snapshot: { policy_decision: "allow" },
      },
      {
        id: "candidate-3",
        agent_id: "agent-3",
        agent_version_id: "version-3",
        eligible: false,
        matched_capabilities: [],
        missing_capabilities: ["research"],
        rejection_reasons: ["missing_capability:research"],
        evidence: { selection_rank: null },
        constraints_snapshot: { policy_decision: "allow" },
      },
    ],
    assignments: [
      {
        id: "assignment-1",
        assignment_key: task.id,
        requirement_id: "requirement-1",
        candidate_id: "candidate-1",
        selected_by: "user-1",
        rationale: "Best capability coverage",
        validation_status: "valid",
        validation_evidence: { matched_capabilities: ["research"] },
      },
    ],
    overrides: [],
    ...overrides,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("GoalPlanningPanel", () => {
  it("previews candidates, applies an eligible override, and accepts the task graph", async () => {
    const preview = makeSession()
    const overridden = makeSession({
      assignments: [{ ...preview.assignments[0], candidate_id: "candidate-2" }],
      overrides: [
        {
          id: "override-1",
          assignment_id: "assignment-1",
          actor_id: "user-1",
          requested_candidate_id: "candidate-2",
          reason: "Use the reviewer",
          prior_candidate_evidence: {},
          validation_status: "valid",
          validation_evidence: {},
        },
      ],
    })
    const accepted: GoalPlanningAcceptance = {
      ...overridden,
      status: "accepted",
      materialized_tasks: [{ task_id: task.id, assignment_key: task.id }],
      graph_revision_id: "revision-1",
      graph_revision_number: 1,
    }
    const calls = installFetchMock({
      "GET /goals/goal-1/planning-sessions": () => jsonResponse([]),
      "POST /goals/goal-1/planning-sessions": () => jsonResponse(preview, 201),
      "POST /goals/goal-1/planning-sessions/plan-1/overrides": () =>
        jsonResponse(overridden, 201),
      "POST /goals/goal-1/planning-sessions/plan-1/accept": () =>
        jsonResponse(accepted),
    })
    const onAccepted = vi.fn(async () => undefined)
    const user = userEvent.setup()

    render(
      <GoalPlanningPanel
        goalId="goal-1"
        tasks={[task]}
        agents={agents}
        onAccepted={onAccepted}
      />
    )

    await user.click(
      await screen.findByRole("button", { name: /Preview team and plan/ })
    )
    expect(
      (await screen.findAllByText("Research agent")).length
    ).toBeGreaterThan(0)
    expect(screen.getAllByText("Review agent").length).toBeGreaterThan(0)
    expect(screen.getAllByText("Rejected agent").length).toBeGreaterThan(0)
    expect(screen.getByText(/missing capability:research/)).toBeInTheDocument()
    expect(
      screen.getByText(/required model capabilities: tool_calling/)
    ).toBeInTheDocument()

    await user.selectOptions(
      screen.getByLabelText("Override agent"),
      "version-2"
    )
    await user.type(
      screen.getByLabelText("Override reason"),
      "Use the reviewer"
    )
    await user.click(screen.getByRole("button", { name: /^Apply$/ }))
    expect(
      await screen.findByText(/Assignment for Research the topic was updated/)
    ).toBeInTheDocument()

    await user.click(
      screen.getByRole("button", { name: /Accept and schedule/ })
    )
    expect(
      await screen.findByText(/task graph revision 1 is scheduled/)
    ).toBeInTheDocument()
    expect(onAccepted).toHaveBeenCalledOnce()
    expect(
      calls.find((call) => call.path.endsWith("/overrides"))?.body
    ).toEqual({
      assignment_key: task.id,
      agent_version_id: "version-2",
      reason: "Use the reviewer",
    })
  })

  it("shows no-eligible-agent evidence and keeps acceptance disabled", async () => {
    const pending = makeSession({
      validation_status: "pending",
      candidates: [
        {
          ...makeSession().candidates[2],
          id: "candidate-only",
        },
      ],
      assignments: [
        {
          ...makeSession().assignments[0],
          candidate_id: null,
          validation_status: "pending",
        },
      ],
    })
    installFetchMock({
      "GET /goals/goal-1/planning-sessions": () => jsonResponse([pending]),
    })

    render(
      <GoalPlanningPanel
        goalId="goal-1"
        tasks={[task]}
        agents={agents}
        onAccepted={vi.fn(async () => undefined)}
      />
    )

    expect(await screen.findByText("0")).toBeInTheDocument()
    expect(screen.getByText("Rejected agent")).toBeInTheDocument()
    expect(
      screen.getByRole("button", { name: /Accept and schedule/ })
    ).toBeDisabled()
    expect(
      screen.getByText(/Resolve every assignment to an eligible candidate/)
    ).toBeInTheDocument()
  })

  it("renders access denial without exposing planning controls", async () => {
    installFetchMock({
      "GET /goals/goal-1/planning-sessions": () =>
        jsonResponse({ detail: "goal not found" }, 404),
    })
    render(
      <GoalPlanningPanel
        goalId="goal-1"
        tasks={[task]}
        agents={agents}
        onAccepted={vi.fn(async () => undefined)}
      />
    )

    expect(
      await screen.findByText(/You do not have access to planning records/)
    ).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: /Preview team and plan/ })
    ).not.toBeInTheDocument()
  })

  it("shows a retry action when planning history is temporarily unavailable", async () => {
    installFetchMock({
      "GET /goals/goal-1/planning-sessions": () =>
        jsonResponse({ error: "planning service unavailable" }, 502),
    })
    render(
      <GoalPlanningPanel
        goalId="goal-1"
        tasks={[task]}
        agents={agents}
        onAccepted={vi.fn(async () => undefined)}
      />
    )

    expect(
      await screen.findByText(/planning service unavailable/)
    ).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Retry/ })).toBeInTheDocument()
  })
})
