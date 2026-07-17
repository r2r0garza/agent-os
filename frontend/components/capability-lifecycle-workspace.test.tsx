import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { CapabilityLifecycleWorkspace } from "@/components/capability-lifecycle-workspace"
import type {
  Agent,
  AgentVersion,
  McpServer,
  McpServerHealthCheck,
  McpServerTool,
  McpServerVersion,
  Skill,
  SkillVersion,
} from "@/lib/api"

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

interface RouteTable {
  [key: string]: (url: URL, init?: RequestInit) => Response | Promise<Response>
}

function installFetchMock(routes: RouteTable) {
  const calls: Array<{ method: string; path: string; body: unknown }> = []
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost")
    const method = (init?.method ?? "GET").toUpperCase()
    const path = url.pathname.replace(/^\/api\/agentic/, "")
    const body = init?.body ? JSON.parse(String(init.body)) : null
    calls.push({ method, path, body })
    const handler = routes[`${method} ${path}`]
    if (!handler) throw new Error(`Unhandled request: ${method} ${path}`)
    return handler(url, init)
  }))
  return calls
}

const skill: Skill = {
  id: "skill-1", team_id: "team-1", created_by: "user-1",
  name: "Research package", visibility: "team", created_at: "2026-07-17T00:00:00Z",
}

function makeSkillVersion(overrides: Partial<SkillVersion> = {}): SkillVersion {
  return {
    id: "skill-version-1", skill_id: skill.id, version_number: 1,
    content_ref: `sha256:${"a".repeat(64)}`, resource_metadata: {},
    manifest: { name: "research-package", resources: ["references/guide.md"] },
    instructions: "Use the guide.",
    resources: [{
      path: "references/guide.md", content: "# Guide", metadata: {},
      sha256: "b".repeat(64), size_bytes: 7,
    }],
    declared_capabilities: ["research"], provenance: { source: "authored" },
    package_hash: "a".repeat(64), validation_status: "valid",
    validation_diagnostics: [], created_at: "2026-07-17T00:00:00Z",
    ...overrides,
  }
}

const server: McpServer = {
  id: "server-1", team_id: "team-1", project_id: null, name: "Search MCP",
  visibility: "team", created_at: "2026-07-17T00:00:00Z",
}

const serverVersion: McpServerVersion = {
  id: "server-version-1", mcp_server_id: server.id, version_number: 1,
  connection_config: { url: "https://mcp.example/mcp" },
  credential_configured: true, credential_id: null, created_at: "2026-07-17T00:00:00Z",
}

const agent: Agent = {
  id: "agent-1", team_id: "team-1", created_by: "user-1",
  name: "Research agent", visibility: "private", created_at: "2026-07-17T00:00:00Z",
}

function makeHealth(overrides: Partial<McpServerHealthCheck> = {}): McpServerHealthCheck {
  return {
    id: "health-1", mcp_server_version_id: serverVersion.id, status: "healthy",
    tool_count: 1, latency_ms: 12, request_metadata: {}, diagnostics: [],
    checked_at: "2026-07-17T00:00:00Z", created_at: "2026-07-17T00:00:00Z",
    ...overrides,
  }
}

function makeTool(overrides: Partial<McpServerTool> = {}): McpServerTool {
  return {
    id: "tool-1", mcp_server_version_id: serverVersion.id, tool_name: "search",
    description: "Untrusted remote search description", input_schema: { type: "object" },
    schema_valid: true, schema_validation_errors: [], descriptor_hash: "d".repeat(64),
    credential_scope_required: true, enabled: false, timeout_ms: null,
    output_limit_bytes: null, last_discovered_at: "2026-07-17T00:00:00Z",
    created_at: "2026-07-17T00:00:00Z", updated_at: "2026-07-17T00:00:00Z",
    ...overrides,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("CapabilityLifecycleWorkspace", () => {
  it("authors, validates, inspects, and exports an immutable skill package", async () => {
    let versions = [makeSkillVersion()]
    const calls = installFetchMock({
      "GET /skills/skill-1/versions": () => jsonResponse(versions),
      "POST /skills/skill-1/versions": (_url, init) => {
        const body = JSON.parse(String(init?.body))
        const versionNumber = versions.length + 1
        const created = makeSkillVersion({
          id: `skill-version-${versionNumber}`,
          version_number: versionNumber,
          manifest: body.manifest,
          provenance: body.provenance,
        })
        versions = [...versions, created]
        return jsonResponse(created, 201)
      },
      "GET /skills/skill-1/versions/2/export": () =>
        jsonResponse({ format_version: 1, name: skill.name, ...versions[1] }),
    })

    const user = userEvent.setup()
    render(<CapabilityLifecycleWorkspace agents={[]} skills={[skill]} servers={[]} />)

    expect(await screen.findByText(/package aaaaaaaaaaaa/)).toBeInTheDocument()
    await user.type(screen.getByPlaceholderText("Manifest name"), "research-v2")
    await user.type(screen.getByPlaceholderText("Package description"), "Research workflow")
    await user.type(screen.getByPlaceholderText("Instructions"), "Follow the guide")
    await user.type(screen.getByPlaceholderText("# Guide"), "# Updated guide")
    await user.type(screen.getByPlaceholderText("research, summarize"), "research")
    await user.click(screen.getByRole("button", { name: /Validate author\/import/ }))

    expect(await screen.findByText(/Immutable skill package version/)).toBeInTheDocument()
    expect(screen.getByText("v2")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: /Inspect redacted export/ }))
    expect(await screen.findByText(/Redacted export bundle loaded/)).toBeInTheDocument()
    expect(screen.getByText(/"format_version": 1/)).toBeInTheDocument()

    await user.click(screen.getByPlaceholderText("Paste package JSON to import (optional)"))
    await user.paste(JSON.stringify({
      manifest: { name: "imported-package", resources: [] },
      instructions: "Imported instructions",
      resources: [],
      declared_capabilities: ["summarize"],
      provenance: { source: "imported" },
    }))
    await user.click(screen.getByRole("button", { name: /Validate author\/import/ }))
    expect(await screen.findByText("v3")).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "POST").at(-1)?.body).toEqual(expect.objectContaining({
      provenance: { source: "imported" },
    }))
  })

  it("discovers MCP tools, saves limits, and creates explicit agent grants", async () => {
    let checks: McpServerHealthCheck[] = []
    let tools: McpServerTool[] = []
    const calls = installFetchMock({
      "GET /skills/skill-1/versions": () => jsonResponse([makeSkillVersion()]),
      "GET /mcp-servers/server-1/versions": () => jsonResponse([serverVersion]),
      "GET /mcp-servers/server-1/versions/1/health-checks": () => jsonResponse(checks),
      "GET /mcp-servers/server-1/versions/1/discovered-tools": () => jsonResponse(tools),
      "POST /mcp-servers/server-1/versions/1/health-checks": () => {
        checks = [makeHealth()]
        tools = [makeTool()]
        return jsonResponse(checks[0], 201)
      },
      "PATCH /mcp-servers/server-1/versions/1/discovered-tools/search": (_url, init) => {
        tools = [{ ...tools[0], ...JSON.parse(String(init?.body)) }]
        return jsonResponse(tools[0])
      },
      "POST /agents/agent-1/versions": () => jsonResponse({
        id: "agent-version-2", agent_id: agent.id, version_number: 2,
        instructions: "Use only explicitly granted capabilities.", capability_manifest: {},
        model_profile_id: null, model_profile_version_id: null, default_budget_id: null,
        skill_attachments: [], mcp_server_attachments: [],
        skill_grants: [{
          version_id: "skill-version-1", skill_id: skill.id,
          resource_paths: ["references/guide.md"], declared_capabilities: ["research"],
          package_hash: "a".repeat(64), provenance: { source: "authored" },
          policy_metadata: { decision: "allow" }, granted_by: "user-1",
          granted_at: "2026-07-17T00:00:00Z",
        }],
        mcp_tool_grants: [{
          version_id: serverVersion.id, mcp_server_id: server.id,
          tools: [{ name: "search", descriptor_hash: "d".repeat(64), timeout_ms: 1500, output_limit_bytes: 4096 }],
          policy_metadata: { decision: "allow" }, credential_configured: true,
          granted_by: "user-1", granted_at: "2026-07-17T00:00:00Z",
        }],
        policy_set_version_ids: [], created_at: "2026-07-17T00:00:00Z",
      } satisfies AgentVersion, 201),
    })

    const user = userEvent.setup()
    render(<CapabilityLifecycleWorkspace agents={[agent]} skills={[skill]} servers={[server]} />)

    await screen.findByText("unprobed")
    await user.click(screen.getByRole("button", { name: /Discover & check health/ }))
    expect(await screen.findByText(/healthy · 1 tools/)).toBeInTheDocument()
    expect(screen.getByText(/Untrusted remote search description/)).toBeInTheDocument()
    expect(screen.getByText(/credential scope required/)).toBeInTheDocument()

    await user.click(screen.getByRole("checkbox", { name: /Enabled/ }))
    await user.type(screen.getByPlaceholderText("Timeout ms"), "1500")
    await user.type(screen.getByPlaceholderText("Output bytes"), "4096")
    await user.click(screen.getByRole("button", { name: /Save tool policy/ }))
    expect(await screen.findByText(/Tool search settings saved/)).toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText("Agent"), agent.id)
    await user.type(screen.getByPlaceholderText("Enabled tools"), "search")
    await user.click(screen.getByRole("button", { name: /Create granted agent version/ }))
    expect(await screen.findByText(/Agent version 2 created/)).toBeInTheDocument()
    expect(screen.getByText(/1 skill grant\(s\), 1 MCP grant\(s\)/)).toBeInTheDocument()
    expect(calls).toEqual(expect.arrayContaining([
      expect.objectContaining({ method: "PATCH", path: "/mcp-servers/server-1/versions/1/discovered-tools/search" }),
      expect.objectContaining({ method: "POST", path: "/agents/agent-1/versions" }),
    ]))
  })

  it("covers empty, unauthorized, and retry states", async () => {
    const { rerender } = render(
      <CapabilityLifecycleWorkspace agents={[]} skills={[]} servers={[]} />
    )
    expect(await screen.findByText(/no governed capability definitions are visible/i)).toBeInTheDocument()

    installFetchMock({
      "GET /skills/skill-1/versions": () =>
        jsonResponse({ detail: "capability lifecycle forbidden" }, 403),
    })
    rerender(<CapabilityLifecycleWorkspace agents={[]} skills={[skill]} servers={[]} />)

    expect(await screen.findByText("Capability access denied")).toBeInTheDocument()
    expect(screen.getByText(/capability lifecycle forbidden/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Retry/ })).toBeInTheDocument()
  })
})
