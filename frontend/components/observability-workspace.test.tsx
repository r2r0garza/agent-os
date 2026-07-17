import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ObservabilityWorkspace } from "@/components/observability-workspace"
import type { ObservabilityHealth } from "@/lib/api"

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

function makeHealth(overrides: Partial<ObservabilityHealth> = {}): ObservabilityHealth {
  return {
    status: "healthy",
    checked_at: "2026-01-01T00:00:00Z",
    deployment: {
      status: "healthy",
      checks: {
        database: { status: "healthy", detail: "connected in 1ms" },
        migrations: { status: "healthy", detail: "database is at head" },
      },
    },
    maintenance: {
      events: [],
      commands: {
        setup_check: "./agentic-os operations setup-check",
        migration_status: "./agentic-os operations migrations status",
        backup: "./agentic-os operations backup --output <backup.tar.gz>",
        restore: "./agentic-os operations restore <backup.tar.gz> ...",
        upgrade_preflight: "./agentic-os operations upgrade-preflight",
      },
    },
    database: { status: "healthy", latency_ms: 1.2 },
    queues: { status: "healthy", depth: 0, tasks_by_status: {} },
    workers: {
      status: "healthy",
      active: 2,
      stale: 0,
      stale_worker_ids: [],
      stale_task_ids: [],
      lease_count: 2,
      retry_count: 0,
      failure_count: 0,
      capacity: 3,
      live_worker_ids: ["worker-a", "worker-b"],
      missing_worker_ids: [],
    },
    sandbox: {
      status: "healthy",
      runtimes: {
        docker: { status: "available", reason: null },
        podman: { status: "unavailable", reason: "not installed" },
      },
    },
    event_stream: {
      status: "healthy",
      latest_record_at: "2026-01-01T00:00:00Z",
      latest_record_age_seconds: 5,
      latest_correlation_id: "corr-1",
      deliveries_by_status: {},
      oldest_queued_delivery_at: null,
      delivery_delay_seconds: null,
    },
    telemetry: {
      status: "healthy",
      deliveries_by_status: {},
      exporters: [],
    },
    ...overrides,
  }
}

function emptyTimelineRoutes(goalId: string) {
  return {
    [`GET /goals/${goalId}/observability-timeline`]: () => jsonResponse([]),
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("ObservabilityWorkspace admin health", () => {
  it("shows a loading state before health evidence arrives", async () => {
    let resolveHealth: (() => void) | undefined
    installFetchMock({
      ...emptyTimelineRoutes("goal-1"),
      "GET /admin/observability/health": () =>
        new Promise((resolve) => {
          resolveHealth = () => resolve(jsonResponse(makeHealth()))
        }),
    })

    render(<ObservabilityWorkspace goalId="goal-1" runs={[]} />)

    expect(
      await screen.findByText(/Loading installation health/)
    ).toBeInTheDocument()

    resolveHealth?.()
    await waitFor(() =>
      expect(screen.getByText(/Installation healthy/)).toBeInTheDocument()
    )
  })

  it("renders healthy status, worker capacity, and deployment mode evidence", async () => {
    installFetchMock({
      ...emptyTimelineRoutes("goal-1"),
      "GET /admin/observability/health": () =>
        jsonResponse(
          makeHealth({
            maintenance: {
              events: [
                {
                  id: "event-1",
                  event_type: "operations.upgrade_preflight",
                  occurred_at: "2026-01-01T00:00:00Z",
                  evidence: {
                    ready: true,
                    deployment_mode: "team",
                    configuration: ["[OK] public_origin: configured over TLS"],
                    migrations: "database is at the migration head",
                    rollback: "Create and verify a backup before upgrade.",
                  },
                },
              ],
              commands: makeHealth().maintenance.commands,
            },
          })
        ),
    })

    render(<ObservabilityWorkspace goalId="goal-1" runs={[]} />)

    await waitFor(() =>
      expect(screen.getByText(/Installation healthy/)).toBeInTheDocument()
    )
    expect(
      screen.getByText(/2 active \/ 3 capacity · 0 stale · 0 missing heartbeat/)
    ).toBeInTheDocument()
    expect(
      screen.getByText(/team \(from latest upgrade preflight evidence\)/)
    ).toBeInTheDocument()
    expect(
      screen.getByText(/team upgrade preflight ready/)
    ).toBeInTheDocument()
  })

  it("surfaces degraded worker recovery state and highlights failed preflight checks", async () => {
    installFetchMock({
      ...emptyTimelineRoutes("goal-1"),
      "GET /admin/observability/health": () =>
        jsonResponse(
          makeHealth({
            status: "recovering",
            workers: {
              status: "recovering",
              active: 1,
              stale: 1,
              stale_worker_ids: ["worker-a"],
              stale_task_ids: ["task-1"],
              lease_count: 2,
              retry_count: 3,
              failure_count: 1,
              capacity: 2,
              live_worker_ids: ["worker-b"],
              missing_worker_ids: [],
            },
            maintenance: {
              events: [
                {
                  id: "event-2",
                  event_type: "operations.setup_check",
                  occurred_at: "2026-01-01T00:00:00Z",
                  evidence: {
                    ok: false,
                    report: [
                      "[OK] database: connected",
                      "[FAIL] public_origin: AGENTIC_OS_PUBLIC_ORIGIN is required for team deployment",
                    ],
                  },
                },
              ],
              commands: makeHealth().maintenance.commands,
            },
          })
        ),
    })

    render(<ObservabilityWorkspace goalId="goal-1" runs={[]} />)

    await waitFor(() =>
      expect(screen.getByText(/Installation recovering/)).toBeInTheDocument()
    )
    expect(
      screen.getByText(/1 active \/ 2 capacity · 1 stale · 0 missing heartbeat/)
    ).toBeInTheDocument()
    expect(screen.getByText(/Preflight failed \(1 check\)/)).toBeInTheDocument()
    expect(
      screen.getByText(
        /\[FAIL\] public_origin: AGENTIC_OS_PUBLIC_ORIGIN is required for team deployment/
      )
    ).toBeInTheDocument()
  })

  it("expands raw evidence for a maintenance event on request", async () => {
    installFetchMock({
      ...emptyTimelineRoutes("goal-1"),
      "GET /admin/observability/health": () =>
        jsonResponse(
          makeHealth({
            maintenance: {
              events: [
                {
                  id: "event-3",
                  event_type: "operations.backup_created",
                  occurred_at: "2026-01-01T00:00:00Z",
                  evidence: {
                    backup: "/backups/agentic-os-20260101T000000Z.tar.gz",
                    manifest: { artifacts: [{ path: "a", sha256: "x" }] },
                  },
                },
              ],
              commands: makeHealth().maintenance.commands,
            },
          })
        ),
    })

    const user = userEvent.setup()
    render(<ObservabilityWorkspace goalId="goal-1" runs={[]} />)

    await waitFor(() =>
      expect(
        screen.getByText(/Backup created: \/backups\/agentic-os-20260101T000000Z.tar.gz/)
      ).toBeInTheDocument()
    )
    expect(screen.queryByText(/"artifacts"/)).not.toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: /Show raw evidence/ }))

    expect(screen.getByText(/"artifacts"/)).toBeInTheDocument()
  })

  it("shows the empty maintenance evidence state when no operations have been recorded", async () => {
    installFetchMock({
      ...emptyTimelineRoutes("goal-1"),
      "GET /admin/observability/health": () => jsonResponse(makeHealth()),
    })

    render(<ObservabilityWorkspace goalId="goal-1" runs={[]} />)

    await waitFor(() =>
      expect(
        screen.getByText(/No maintenance command evidence has been recorded yet/)
      ).toBeInTheDocument()
    )
  })

  it("shows an admin-required notice and hides installation health when access is denied", async () => {
    installFetchMock({
      ...emptyTimelineRoutes("goal-1"),
      "GET /admin/observability/health": () =>
        jsonResponse({ detail: "admin role required" }, 403),
    })

    render(<ObservabilityWorkspace goalId="goal-1" runs={[]} />)

    await waitFor(() =>
      expect(screen.getByText(/Admin role required/)).toBeInTheDocument()
    )
    expect(screen.queryByText(/Installation healthy/)).not.toBeInTheDocument()
  })

  it("shows a health API error banner when the request fails unexpectedly", async () => {
    installFetchMock({
      ...emptyTimelineRoutes("goal-1"),
      "GET /admin/observability/health": () =>
        jsonResponse({ error: "upstream unavailable" }, 502),
    })

    render(<ObservabilityWorkspace goalId="goal-1" runs={[]} />)

    await waitFor(() =>
      expect(screen.getByText(/upstream unavailable/)).toBeInTheDocument()
    )
    expect(screen.getByText(/Health API unavailable/)).toBeInTheDocument()
  })
})
