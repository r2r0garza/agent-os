import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { GovernanceWorkspace } from "@/components/governance-workspace"
import type { ModelProfile, ModelProfileProbe, ModelProfileVersion } from "@/lib/api"

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

function makeModel(overrides: Partial<ModelProfile> = {}): ModelProfile {
  return {
    id: "model-1",
    name: "Primary GPT",
    base_url: "https://api.openai.com/v1",
    model_identifier: "gpt-5-mini",
    capability_metadata: {},
    pricing_metadata: {},
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  }
}

function makeVersion(
  overrides: Partial<ModelProfileVersion> = {}
): ModelProfileVersion {
  return {
    id: "version-1",
    model_profile_id: "model-1",
    version_number: 1,
    base_url: "https://api.openai.com/v1",
    model_identifier: "gpt-5-mini",
    credential_id: "cred-1",
    headers: {},
    capability_metadata: {},
    pricing_metadata: {},
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  }
}

function makeProbe(overrides: Partial<ModelProfileProbe> = {}): ModelProfileProbe {
  return {
    id: "probe-1",
    model_profile_version_id: "version-1",
    status: "completed",
    capability_evidence: {
      streaming: { status: "supported", diagnostic: "SSE data frame returned" },
      tool_calls: { status: "supported", diagnostic: "tool call returned" },
      structured_output: { status: "supported", diagnostic: "valid JSON object returned" },
      token_usage: { status: "supported", diagnostic: "usage object returned" },
      reasoning: { status: "unknown", diagnostic: "no reasoning fields" },
      retry_timeout: { status: "supported", diagnostic: "bounded timeout policy completed" },
    },
    pricing_evidence: { status: "valid", metered: true, warnings: [], failures: [] },
    request_metadata: { endpoint: "https://api.openai.com" },
    diagnostics: [],
    started_at: "2026-01-01T00:00:00Z",
    completed_at: "2026-01-01T00:00:01Z",
    created_at: "2026-01-01T00:00:01Z",
    ...overrides,
  }
}

function baseRoutes(probesStore: () => ModelProfileProbe[]): RouteTable {
  return {
    "GET /credentials": () => jsonResponse([]),
    "GET /policy-sets": () => jsonResponse([]),
    "GET /model-profiles/model-1/versions": () => jsonResponse([makeVersion()]),
    "GET /model-profiles/model-1/versions/1/probes": () => jsonResponse(probesStore()),
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("GovernanceWorkspace model profile probing", () => {
  it("shows an unprobed state and lets an operator probe a version", async () => {
    let probes: ModelProfileProbe[] = []
    installFetchMock({
      ...baseRoutes(() => probes),
      "POST /model-profiles/model-1/versions/1/probe": () => {
        probes = [makeProbe()]
        return jsonResponse(probes[0], 201)
      },
    })

    const user = userEvent.setup()
    render(
      <GovernanceWorkspace agents={[]} models={[makeModel()]} skills={[]} servers={[]} />
    )

    expect(await screen.findByText("unprobed")).toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: /Probe/ }))

    await waitFor(() =>
      expect(screen.getByText(/probe completed/)).toBeInTheDocument()
    )
    expect(screen.getByText(/Streaming: supported/)).toBeInTheDocument()
    expect(screen.getByText(/pricing: valid/)).toBeInTheDocument()
    expect(screen.queryByText("unprobed")).not.toBeInTheDocument()
  })

  it("flags degraded and unsupported capability evidence from a prior probe", async () => {
    const probes = [
      makeProbe({
        status: "degraded",
        capability_evidence: {
          streaming: { status: "unsupported", diagnostic: "provider returned HTTP 404" },
          tool_calls: { status: "unknown", diagnostic: "request succeeded without a tool call" },
          structured_output: { status: "supported", diagnostic: "valid JSON object returned" },
          token_usage: { status: "supported", diagnostic: "usage object returned" },
          reasoning: { status: "unknown", diagnostic: "no reasoning fields" },
          retry_timeout: { status: "supported", diagnostic: "bounded timeout policy completed" },
        },
      }),
    ]
    installFetchMock(baseRoutes(() => probes))

    render(
      <GovernanceWorkspace agents={[]} models={[makeModel()]} skills={[]} servers={[]} />
    )

    await waitFor(() =>
      expect(screen.getByText(/probe degraded/)).toBeInTheDocument()
    )
    expect(screen.getByText(/Streaming: unsupported/)).toBeInTheDocument()
  })

  it("marks an old probe as stale and recommends re-probing", async () => {
    const probes = [
      makeProbe({ created_at: "2020-01-01T00:00:00Z", started_at: "2020-01-01T00:00:00Z" }),
    ]
    installFetchMock(baseRoutes(() => probes))

    render(
      <GovernanceWorkspace agents={[]} models={[makeModel()]} skills={[]} servers={[]} />
    )

    expect(
      await screen.findByText(/stale · re-probe recommended/)
    ).toBeInTheDocument()
  })

  it("shows an error message when a probe request fails", async () => {
    installFetchMock({
      ...baseRoutes(() => []),
      "POST /model-profiles/model-1/versions/1/probe": () =>
        jsonResponse({ detail: "model profile version has no credential" }, 422),
    })

    const user = userEvent.setup()
    render(
      <GovernanceWorkspace agents={[]} models={[makeModel()]} skills={[]} servers={[]} />
    )

    await screen.findByText("unprobed")
    await user.click(screen.getByRole("button", { name: /Probe/ }))

    expect(
      await screen.findByText(/model profile version has no credential/)
    ).toBeInTheDocument()
  })
})
