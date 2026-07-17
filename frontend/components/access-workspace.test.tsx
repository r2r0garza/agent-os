import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { AccessWorkspace } from "@/components/access-workspace"
import type {
  Agent,
  McpServer,
  Project,
  Skill,
  Team,
  TeamMembership,
  UserAccount,
} from "@/lib/api"

const TEAM: Team = {
  id: "team-1",
  name: "Core team",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
}

const OWNER_MEMBERSHIP: TeamMembership = {
  id: "membership-owner",
  team_id: "team-1",
  user_id: "user-owner",
  role: "owner",
  created_at: "2026-01-01T00:00:00Z",
  user_email: "owner@example.test",
  user_display_name: "Owner",
}

const TEAMMATE_MEMBERSHIP: TeamMembership = {
  id: "membership-teammate",
  team_id: "team-1",
  user_id: "user-teammate",
  role: "member",
  created_at: "2026-01-01T00:00:00Z",
  user_email: "teammate@example.test",
  user_display_name: "Teammate",
}

const PROJECT: Project = {
  id: "project-1",
  team_id: "team-1",
  created_by: "user-owner",
  name: "Shared project",
  created_at: "2026-01-01T00:00:00Z",
}

const ADMIN_USERS: UserAccount[] = [
  {
    id: "user-owner",
    email: "owner@example.test",
    display_name: "Owner",
    role: "admin",
    created_at: "2026-01-01T00:00:00Z",
  },
]

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

const baseProps = {
  projectId: PROJECT.id,
  projects: [PROJECT],
  agents: [] as Agent[],
  skills: [] as Skill[],
  servers: [] as McpServer[],
  onRefresh: vi.fn(async () => undefined),
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("AccessWorkspace", () => {
  it("renders team membership and shows an admin-required notice for a regular-user actor", async () => {
    installFetchMock({
      "GET /teams": () => jsonResponse([TEAM]),
      "GET /users": () => jsonResponse({ detail: "admin role required" }, 403),
      "GET /teams/team-1/memberships": () =>
        jsonResponse([OWNER_MEMBERSHIP, TEAMMATE_MEMBERSHIP]),
      "GET /projects/project-1/members": () => jsonResponse([]),
      "GET /credentials": () => jsonResponse([]),
    })

    render(<AccessWorkspace {...baseProps} />)

    await waitFor(() =>
      expect(screen.getByText("Teammate")).toBeInTheDocument()
    )
    expect(screen.getByText("teammate@example.test")).toBeInTheDocument()
    expect(
      screen.getByText(/Admin role required\. Team membership above remains visible/)
    ).toBeInTheDocument()
  })

  it("shows the installation-wide user directory for an admin actor", async () => {
    installFetchMock({
      "GET /teams": () => jsonResponse([TEAM]),
      "GET /users": () => jsonResponse(ADMIN_USERS),
      "GET /teams/team-1/memberships": () => jsonResponse([OWNER_MEMBERSHIP]),
      "GET /projects/project-1/members": () => jsonResponse([]),
      "GET /credentials": () => jsonResponse([]),
    })

    render(<AccessWorkspace {...baseProps} />)

    await waitFor(() =>
      expect(screen.getByText(/owner@example\.test/)).toBeInTheDocument()
    )
    expect(
      screen.queryByText(/Admin role required/)
    ).not.toBeInTheDocument()
  })

  it("degrades to a forbidden notice when granting project access is denied", async () => {
    installFetchMock({
      "GET /teams": () => jsonResponse([TEAM]),
      "GET /users": () => jsonResponse({ detail: "admin role required" }, 403),
      "GET /teams/team-1/memberships": () =>
        jsonResponse([OWNER_MEMBERSHIP, TEAMMATE_MEMBERSHIP]),
      "GET /projects/project-1/members": () => jsonResponse([]),
      "GET /credentials": () => jsonResponse([]),
      "POST /projects/project-1/members": () =>
        jsonResponse(
          { detail: "only the project creator or an admin can manage project access" },
          403
        ),
    })

    const user = userEvent.setup()
    render(<AccessWorkspace {...baseProps} />)

    const select = await screen.findByLabelText("Grant access to")
    await screen.findByRole("option", { name: /Teammate/ })
    await user.selectOptions(select, "user-teammate")
    await user.click(screen.getByRole("button", { name: /Grant project access/ }))

    await waitFor(() =>
      expect(
        screen.getByText(/Only the project creator or an admin can manage/)
      ).toBeInTheDocument()
    )
  })

  it("represents redacted MCP credential state and supports revoking an attachment", async () => {
    const server: McpServer = {
      id: "server-1",
      team_id: "team-1",
      project_id: null,
      name: "Search MCP",
      visibility: "team",
      created_at: "2026-01-01T00:00:00Z",
    }

    let revoked = false
    installFetchMock({
      "GET /teams": () => jsonResponse([TEAM]),
      "GET /users": () => jsonResponse({ detail: "admin role required" }, 403),
      "GET /teams/team-1/memberships": () => jsonResponse([OWNER_MEMBERSHIP]),
      "GET /projects/project-1/members": () => jsonResponse([]),
      "GET /credentials": () => jsonResponse([]),
      "GET /mcp-servers/server-1/versions": () =>
        jsonResponse([
          {
            id: "version-1",
            mcp_server_id: "server-1",
            version_number: 1,
            connection_config: { url: "https://mcp.example.test" },
            credential_configured: true,
            credential_id: null,
            created_at: "2026-01-01T00:00:00Z",
          },
        ]),
      "GET /mcp-servers/server-1/versions/1/attachments": () =>
        jsonResponse([
          {
            id: "attachment-1",
            mcp_server_version_id: "version-1",
            team_id: "team-1",
            project_id: null,
            agent_id: null,
            credential_configured: true,
            revoked,
            created_at: "2026-01-01T00:00:00Z",
          },
        ]),
      "DELETE /mcp-servers/server-1/versions/1/attachments/attachment-1": () => {
        revoked = true
        return jsonResponse({
          id: "attachment-1",
          mcp_server_version_id: "version-1",
          team_id: "team-1",
          project_id: null,
          agent_id: null,
          credential_configured: true,
          revoked: true,
          created_at: "2026-01-01T00:00:00Z",
        })
      },
    })

    const user = userEvent.setup()
    render(<AccessWorkspace {...baseProps} servers={[server]} />)

    const select = await screen.findByLabelText("Server")
    await user.selectOptions(select, "server-1")

    await waitFor(() =>
      expect(screen.getByText(/credential configured/)).toBeInTheDocument()
    )
    // The redacted connection config and any secret material must never reach the DOM.
    expect(screen.queryByText(/mcp\.example\.test/)).not.toBeInTheDocument()

    const revokeButton = await screen.findByRole("button", { name: /Revoke/ })
    await user.click(revokeButton)

    await waitFor(() =>
      expect(screen.getByText("revoked")).toBeInTheDocument()
    )
  })
})
