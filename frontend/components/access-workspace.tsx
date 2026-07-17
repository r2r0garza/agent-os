"use client"

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react"
import {
  Globe,
  KeyRound,
  Lock,
  PackageCheck,
  RefreshCw,
  ServerCog,
  ShieldAlert,
  Trash2,
  Users,
  UserPlus,
} from "lucide-react"

import {
  Agent,
  AgentInstallation,
  ApiError,
  Credential,
  Identifier,
  McpServer,
  McpServerAttachment,
  McpServerVersion,
  Project,
  ProjectMember,
  Skill,
  SkillInstallation,
  Team,
  TeamMembership,
  UserAccount,
  api,
  deleteInit,
  jsonBody,
  patchBody,
} from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed bg-muted/20 px-4 py-6 text-center text-xs text-muted-foreground">
      {children}
    </div>
  )
}

function ForbiddenNotice({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex gap-3 rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-sm">
      <ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-600" />
      <span>{children}</span>
    </div>
  )
}

function visibilityBadge(visibility: string) {
  const icon =
    visibility === "public" ? (
      <Globe className="size-3" />
    ) : visibility === "team" ? (
      <Users className="size-3" />
    ) : (
      <Lock className="size-3" />
    )
  return (
    <Badge variant="outline" className="flex items-center gap-1">
      {icon}
      {visibility}
    </Badge>
  )
}

function shortId(id: string) {
  return id.slice(0, 8)
}

interface SharedDefinition {
  id: Identifier
  team_id: Identifier
  visibility: "private" | "team" | "public"
  name: string
}

function installationSourceVersionId(
  installation: AgentInstallation | SkillInstallation
): string {
  return (
    (installation as AgentInstallation).source_agent_version_id ??
    (installation as SkillInstallation).source_skill_version_id
  )
}

interface AccessWorkspaceProps {
  projectId: string
  projects: Project[]
  agents: Agent[]
  skills: Skill[]
  servers: McpServer[]
  onRefresh: () => Promise<unknown>
}

export function AccessWorkspace({
  projectId,
  projects,
  agents,
  skills,
  servers,
  onRefresh,
}: AccessWorkspaceProps) {
  const [teams, setTeams] = useState<Team[]>([])
  const [selectedTeamId, setSelectedTeamId] = useState("")
  const [memberships, setMemberships] = useState<TeamMembership[]>([])
  const [users, setUsers] = useState<UserAccount[] | null>(null)
  const [usersForbidden, setUsersForbidden] = useState(false)
  const [teamLoading, setTeamLoading] = useState(false)

  const [projectMembers, setProjectMembers] = useState<ProjectMember[]>([])
  const [projectMembersLoading, setProjectMembersLoading] = useState(false)
  const [grantForbidden, setGrantForbidden] = useState(false)
  const [grantUserId, setGrantUserId] = useState("")

  const [installations, setInstallations] = useState<
    Record<string, AgentInstallation | SkillInstallation | null>
  >({})

  const [credentials, setCredentials] = useState<Credential[]>([])
  const [selectedServerId, setSelectedServerId] = useState("")
  const [selectedVersion, setSelectedVersion] = useState<McpServerVersion | null>(
    null
  )
  const [serverVersions, setServerVersions] = useState<McpServerVersion[]>([])
  const [attachments, setAttachments] = useState<McpServerAttachment[]>([])
  const [attachmentForbidden, setAttachmentForbidden] = useState(false)
  const [mcpLoading, setMcpLoading] = useState(false)

  const [mutation, setMutation] = useState("")
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")

  const project = useMemo(
    () => projects.find((item) => item.id === projectId) ?? null,
    [projectId, projects]
  )

  const loadTeamsAndUsers = useCallback(async () => {
    setTeamLoading(true)
    setError("")
    try {
      const teamList = await api<Team[]>("/teams")
      setTeams(teamList)
      setSelectedTeamId((current) => {
        if (current && teamList.some((team) => team.id === current)) return current
        return teamList[0]?.id ?? ""
      })
      try {
        setUsers(await api<UserAccount[]>("/users"))
        setUsersForbidden(false)
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 403) {
          setUsers(null)
          setUsersForbidden(true)
        } else {
          throw reason
        }
      }
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to load teams"
      )
    } finally {
      setTeamLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => void loadTeamsAndUsers(), 0)
    return () => window.clearTimeout(timer)
  }, [loadTeamsAndUsers])

  const loadMemberships = useCallback(async (teamId: string) => {
    if (!teamId) {
      setMemberships([])
      return
    }
    setTeamLoading(true)
    try {
      setMemberships(await api<TeamMembership[]>(`/teams/${teamId}/memberships`))
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to load team membership"
      )
    } finally {
      setTeamLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => void loadMemberships(selectedTeamId), 0)
    return () => window.clearTimeout(timer)
  }, [loadMemberships, selectedTeamId])

  const loadProjectMembers = useCallback(async (id: string) => {
    if (!id) {
      setProjectMembers([])
      return
    }
    setProjectMembersLoading(true)
    setError("")
    try {
      setProjectMembers(await api<ProjectMember[]>(`/projects/${id}/members`))
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to load project access"
      )
    } finally {
      setProjectMembersLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadProjectMembers(projectId)
      setGrantForbidden(false)
    }, 0)
    return () => window.clearTimeout(timer)
  }, [loadProjectMembers, projectId])

  const projectTeamMemberships = useMemo(() => {
    if (!project) return []
    if (project.team_id === selectedTeamId) return memberships
    return []
  }, [memberships, project, selectedTeamId])

  useEffect(() => {
    // Keep the team panel following the selected project's owning team so the
    // grant form can see its candidate members without a second fetch.
    const timer = window.setTimeout(() => {
      if (project && project.team_id !== selectedTeamId) {
        setSelectedTeamId(project.team_id)
      }
    }, 0)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project])

  const grantCandidates = useMemo(
    () =>
      projectTeamMemberships.filter(
        (membership) =>
          !projectMembers.some((member) => member.user_id === membership.user_id) &&
          membership.user_id !== project?.created_by
      ),
    [project, projectMembers, projectTeamMemberships]
  )

  async function grantAccess(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!projectId || !grantUserId) return
    setMutation("grant")
    setError("")
    setNotice("")
    try {
      await api(`/projects/${projectId}/members`, jsonBody({ user_id: grantUserId }))
      setNotice("Project access granted.")
      setGrantUserId("")
      await loadProjectMembers(projectId)
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 403) {
        setGrantForbidden(true)
      }
      setError(
        reason instanceof Error ? reason.message : "Unable to grant project access"
      )
    } finally {
      setMutation("")
    }
  }

  async function revokeAccess(userId: string) {
    if (!projectId) return
    setMutation(`revoke-${userId}`)
    setError("")
    setNotice("")
    try {
      await api(`/projects/${projectId}/members/${userId}`, deleteInit())
      setNotice("Project access revoked.")
      await loadProjectMembers(projectId)
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 403) {
        setGrantForbidden(true)
      }
      setError(
        reason instanceof Error ? reason.message : "Unable to revoke project access"
      )
    } finally {
      setMutation("")
    }
  }

  const homeTeamIds = useMemo(
    () => new Set(teams.map((team) => team.id)),
    [teams]
  )

  const sharedAgents: SharedDefinition[] = agents
  const sharedSkills: SharedDefinition[] = skills

  function ownedByHomeTeam(item: SharedDefinition) {
    return homeTeamIds.has(item.team_id)
  }

  async function changeVisibility(
    kind: "agents" | "skills",
    id: string,
    visibility: string
  ) {
    setMutation(`visibility-${id}`)
    setError("")
    setNotice("")
    try {
      await api(`/${kind}/${id}`, patchBody({ visibility }))
      setNotice("Visibility updated.")
      await onRefresh()
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to change visibility"
      )
    } finally {
      setMutation("")
    }
  }

  async function installDefinition(kind: "agents" | "skills", id: string) {
    setMutation(`install-${id}`)
    setError("")
    setNotice("")
    try {
      const versions = await api<{ version_number: number }[]>(
        `/${kind}/${id}/versions`
      )
      const latest = versions.at(-1)
      if (!latest) {
        setError("This definition has no published versions to install yet.")
        return
      }
      await api(`/${kind}/${id}/versions/${latest.version_number}/install`, jsonBody({}))
      setNotice("Installed a version-pinned copy into your team.")
      await onRefresh()
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to install this definition"
      )
    } finally {
      setMutation("")
    }
  }

  async function loadInstallation(kind: "agents" | "skills", id: string) {
    try {
      const installation = await api<AgentInstallation | SkillInstallation>(
        `/${kind}/${id}/installation`
      )
      setInstallations((current) => ({ ...current, [id]: installation }))
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) {
        setInstallations((current) => ({ ...current, [id]: null }))
        return
      }
      setError(
        reason instanceof Error ? reason.message : "Unable to load installation lineage"
      )
    }
  }

  const loadServerVersions = useCallback(async (serverId: string) => {
    if (!serverId) {
      setServerVersions([])
      setSelectedVersion(null)
      setAttachments([])
      return
    }
    setMcpLoading(true)
    setError("")
    try {
      const versions = await api<McpServerVersion[]>(
        `/mcp-servers/${serverId}/versions`
      )
      setServerVersions(versions)
      const latest = versions.at(-1) ?? null
      setSelectedVersion(latest)
      if (latest) {
        setAttachmentForbidden(false)
        try {
          setAttachments(
            await api<McpServerAttachment[]>(
              `/mcp-servers/${serverId}/versions/${latest.version_number}/attachments`
            )
          )
        } catch (reason) {
          if (reason instanceof ApiError && reason.status === 403) {
            setAttachments([])
            setAttachmentForbidden(true)
          } else {
            throw reason
          }
        }
      } else {
        setAttachments([])
      }
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to load MCP server versions"
      )
    } finally {
      setMcpLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(
      () => void loadServerVersions(selectedServerId),
      0
    )
    return () => window.clearTimeout(timer)
  }, [loadServerVersions, selectedServerId])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void api<Credential[]>("/credentials").then(setCredentials).catch(() => {
        // Credential visibility is scope-limited; an empty list is a valid state.
      })
    }, 0)
    return () => window.clearTimeout(timer)
  }, [])

  async function revokeAttachment(attachmentId: string) {
    if (!selectedServerId || !selectedVersion) return
    setMutation(`attachment-${attachmentId}`)
    setError("")
    setNotice("")
    try {
      await api(
        `/mcp-servers/${selectedServerId}/versions/${selectedVersion.version_number}/attachments/${attachmentId}`,
        deleteInit()
      )
      setNotice("Attachment revoked.")
      await loadServerVersions(selectedServerId)
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 403) {
        setAttachmentForbidden(true)
      }
      setError(
        reason instanceof Error ? reason.message : "Unable to revoke attachment"
      )
    } finally {
      setMutation("")
    }
  }

  async function createAttachment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selectedServerId || !selectedVersion) return
    const form = new FormData(event.currentTarget)
    const scopeType = String(form.get("scope_type") ?? "team")
    const scopeId = String(form.get("scope_id") ?? "").trim()
    const credentialId = String(form.get("credential_id") ?? "").trim()
    if (!scopeId) {
      setError("Provide a target id for the attachment scope.")
      return
    }
    setMutation("attach")
    setError("")
    setNotice("")
    try {
      await api(
        `/mcp-servers/${selectedServerId}/versions/${selectedVersion.version_number}/attachments`,
        jsonBody({
          [`${scopeType}_id`]: scopeId,
          credential_id: credentialId || null,
        })
      )
      setNotice("Attachment created.")
      event.currentTarget.reset()
      await loadServerVersions(selectedServerId)
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 403) {
        setAttachmentForbidden(true)
      }
      setError(
        reason instanceof Error ? reason.message : "Unable to create attachment"
      )
    } finally {
      setMutation("")
    }
  }

  return (
    <section className="mb-6 grid gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <Users className="size-4" /> TEAM ACCESS & SHARING
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            void loadTeamsAndUsers()
            void loadMemberships(selectedTeamId)
            void loadProjectMembers(projectId)
          }}
          disabled={teamLoading}
        >
          <RefreshCw className={teamLoading ? "animate-spin" : ""} /> Refresh access
          state
        </Button>
      </div>

      {error ? (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          <span>{error}</span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              void loadTeamsAndUsers()
              void loadProjectMembers(projectId)
            }}
          >
            Retry
          </Button>
        </div>
      ) : null}
      {notice ? (
        <div className="rounded-xl border bg-background p-4 text-sm">
          {notice}
        </div>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Users className="size-4" /> Team membership
            </CardTitle>
            <CardDescription>
              Installation-wide team roster and admin user directory, read from
              current backend state.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            {teams.length ? (
              <div className="grid gap-1.5">
                <Label htmlFor="access-team-select">Team</Label>
                <select
                  id="access-team-select"
                  className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                  value={selectedTeamId}
                  onChange={(event) => setSelectedTeamId(event.target.value)}
                >
                  {teams.map((team) => (
                    <option key={team.id} value={team.id}>
                      {team.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : teamLoading ? (
              <p className="text-sm text-muted-foreground">Loading teams…</p>
            ) : (
              <EmptyState>No team membership visible for this actor.</EmptyState>
            )}
            <div className="grid gap-2">
              {memberships.length ? (
                memberships.map((membership) => (
                  <div
                    key={membership.id}
                    className="flex items-center justify-between gap-2 rounded-lg border p-3 text-sm"
                  >
                    <div>
                      <p className="font-medium">{membership.user_display_name}</p>
                      <p className="text-xs text-muted-foreground">
                        {membership.user_email}
                      </p>
                    </div>
                    <Badge variant={membership.role === "owner" ? "default" : "outline"}>
                      {membership.role}
                    </Badge>
                  </div>
                ))
              ) : (
                <EmptyState>No members found for this team.</EmptyState>
              )}
            </div>
            <div className="border-t pt-3">
              <p className="mb-2 text-xs font-medium text-muted-foreground">
                INSTALLATION USER DIRECTORY (ADMIN)
              </p>
              {usersForbidden ? (
                <ForbiddenNotice>
                  Admin role required. Team membership above remains visible for
                  your own teams.
                </ForbiddenNotice>
              ) : users && users.length ? (
                <div className="grid gap-1.5 text-xs text-muted-foreground">
                  {users.map((user) => (
                    <div key={user.id} className="flex items-center justify-between gap-2">
                      <span>
                        {user.display_name} · {user.email}
                      </span>
                      <Badge variant="outline">{user.role}</Badge>
                    </div>
                  ))}
                </div>
              ) : (
                <EmptyState>No installation-wide user directory loaded yet.</EmptyState>
              )}
              <p className="mt-3 text-xs text-muted-foreground">
                Adding or removing team members is not yet exposed through a
                backend API; this panel reflects current state only.
              </p>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <UserPlus className="size-4" /> Project access grants
            </CardTitle>
            <CardDescription>
              {project
                ? `Members with explicit access to "${project.name}", inherited from the owning team.`
                : "Select a project to manage its access grants."}
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            {!projectId ? (
              <EmptyState>Select a project above to manage access.</EmptyState>
            ) : (
              <>
                <div className="grid gap-2">
                  {projectMembersLoading && !projectMembers.length ? (
                    <p className="text-sm text-muted-foreground">
                      Loading project access…
                    </p>
                  ) : projectMembers.length ? (
                    projectMembers.map((member) => (
                      <div
                        key={member.id}
                        className="flex items-center justify-between gap-2 rounded-lg border p-3 text-sm"
                      >
                        <div>
                          <p className="font-medium">{member.user_display_name}</p>
                          <p className="text-xs text-muted-foreground">
                            {member.user_email}
                          </p>
                        </div>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={Boolean(mutation)}
                          onClick={() => void revokeAccess(member.user_id)}
                        >
                          <Trash2 className="size-3.5" /> Revoke
                        </Button>
                      </div>
                    ))
                  ) : (
                    <EmptyState>
                      No explicit project members. The project creator always has
                      access.
                    </EmptyState>
                  )}
                </div>
                {grantForbidden ? (
                  <ForbiddenNotice>
                    Only the project creator or an admin can manage this
                    project&apos;s access grants.
                  </ForbiddenNotice>
                ) : (
                  <form className="grid gap-2" onSubmit={grantAccess}>
                    <Label htmlFor="grant-user-select">Grant access to</Label>
                    <select
                      id="grant-user-select"
                      className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                      value={grantUserId}
                      onChange={(event) => setGrantUserId(event.target.value)}
                      required
                    >
                      <option value="">Select a team member…</option>
                      {grantCandidates.map((membership) => (
                        <option key={membership.user_id} value={membership.user_id}>
                          {membership.user_display_name} ({membership.user_email})
                        </option>
                      ))}
                    </select>
                    <Button
                      type="submit"
                      size="sm"
                      disabled={!grantUserId || mutation === "grant"}
                    >
                      <UserPlus className="size-3.5" /> Grant project access
                    </Button>
                    {!grantCandidates.length ? (
                      <p className="text-xs text-muted-foreground">
                        Every team member already has access, or the owning team
                        has no other members.
                      </p>
                    ) : null}
                  </form>
                )}
              </>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <PackageCheck className="size-4" /> Agent & skill visibility
            </CardTitle>
            <CardDescription>
              Private definitions stay home-team only; team and public
              definitions can be installed as version-pinned copies.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            {[
              { kind: "agents" as const, label: "Agents", items: sharedAgents },
              { kind: "skills" as const, label: "Skills", items: sharedSkills },
            ].map(({ kind, label, items }) => (
              <div key={kind} className="grid gap-2">
                <p className="text-xs font-medium text-muted-foreground">
                  {label.toUpperCase()}
                </p>
                {items.length ? (
                  items.map((item) => {
                    const owned = ownedByHomeTeam(item)
                    const installation = installations[item.id]
                    return (
                      <div key={item.id} className="rounded-lg border p-3 text-sm">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="font-medium">{item.name}</span>
                          {visibilityBadge(item.visibility)}
                        </div>
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          {owned ? (
                            <select
                              className="h-8 rounded-lg border bg-background px-2 text-xs"
                              value={item.visibility}
                              disabled={mutation === `visibility-${item.id}`}
                              onChange={(event) =>
                                void changeVisibility(kind, item.id, event.target.value)
                              }
                            >
                              <option value="private">private</option>
                              <option value="team">team</option>
                              <option value="public">public</option>
                            </select>
                          ) : (
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={mutation === `install-${item.id}`}
                              onClick={() => void installDefinition(kind, item.id)}
                            >
                              Install a pinned copy
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => void loadInstallation(kind, item.id)}
                          >
                            {installation === undefined
                              ? "Check lineage"
                              : installation
                                ? `Installed from ${shortId(installationSourceVersionId(installation))}`
                                : "Source definition"}
                          </Button>
                        </div>
                      </div>
                    )
                  })
                ) : (
                  <EmptyState>No {label.toLowerCase()} visible yet.</EmptyState>
                )}
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ServerCog className="size-4" /> MCP server credential scoping
            </CardTitle>
            <CardDescription>
              Credential state is redacted and actor-relative; revoked
              attachments remain visible for audit history.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            {servers.length ? (
              <div className="grid gap-1.5">
                <Label htmlFor="mcp-server-select">Server</Label>
                <select
                  id="mcp-server-select"
                  className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                  value={selectedServerId}
                  onChange={(event) => setSelectedServerId(event.target.value)}
                >
                  <option value="">Select an MCP server…</option>
                  {servers.map((server) => (
                    <option key={server.id} value={server.id}>
                      {server.name} ({server.visibility})
                    </option>
                  ))}
                </select>
              </div>
            ) : (
              <EmptyState>No MCP servers visible yet.</EmptyState>
            )}

            {mcpLoading ? (
              <p className="text-sm text-muted-foreground">Loading versions…</p>
            ) : selectedServerId && selectedVersion ? (
              <>
                <div className="flex items-center justify-between gap-2 rounded-lg border p-3 text-sm">
                  <span>
                    Version {selectedVersion.version_number} ·{" "}
                    {serverVersions.length} version
                    {serverVersions.length === 1 ? "" : "s"}
                  </span>
                  <Badge variant={selectedVersion.credential_configured ? "default" : "outline"}>
                    <KeyRound className="size-3" />{" "}
                    {selectedVersion.credential_configured
                      ? "credential configured"
                      : "no credential visible"}
                  </Badge>
                </div>

                <div className="grid gap-2">
                  {attachments.length ? (
                    attachments.map((attachment) => (
                      <div
                        key={attachment.id}
                        className="flex items-center justify-between gap-2 rounded-lg border p-3 text-xs"
                      >
                        <div>
                          <p className="font-medium">
                            {attachment.team_id
                              ? `team:${shortId(attachment.team_id)}`
                              : attachment.project_id
                                ? `project:${shortId(attachment.project_id)}`
                                : `agent:${shortId(attachment.agent_id ?? "")}`}
                          </p>
                          <p className="text-muted-foreground">
                            {attachment.credential_configured
                              ? "credential attached"
                              : "no credential"}
                          </p>
                        </div>
                        {attachment.revoked ? (
                          <Badge variant="secondary">revoked</Badge>
                        ) : (
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={Boolean(mutation)}
                            onClick={() => void revokeAttachment(attachment.id)}
                          >
                            <Trash2 className="size-3.5" /> Revoke
                          </Button>
                        )}
                      </div>
                    ))
                  ) : attachmentForbidden ? (
                    <ForbiddenNotice>
                      You do not have access to this server&apos;s attachment
                      scopes.
                    </ForbiddenNotice>
                  ) : (
                    <EmptyState>No attachments granted for this version.</EmptyState>
                  )}
                </div>

                {!attachmentForbidden ? (
                  <form className="grid gap-2 border-t pt-3" onSubmit={createAttachment}>
                    <p className="text-xs font-medium text-muted-foreground">
                      GRANT ATTACHMENT
                    </p>
                    <div className="flex gap-2">
                      <select
                        name="scope_type"
                        className="h-9 rounded-lg border bg-background px-2 text-sm"
                      >
                        <option value="team">team</option>
                        <option value="project">project</option>
                        <option value="agent">agent</option>
                      </select>
                      <input
                        name="scope_id"
                        placeholder="Target scope id"
                        className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                      />
                    </div>
                    <select
                      name="credential_id"
                      className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                      defaultValue=""
                    >
                      <option value="">No credential</option>
                      {credentials.map((credential) => (
                        <option key={credential.id} value={credential.id}>
                          {credential.name}
                        </option>
                      ))}
                    </select>
                    <Button type="submit" size="sm" disabled={mutation === "attach"}>
                      Grant attachment
                    </Button>
                  </form>
                ) : null}
              </>
            ) : selectedServerId ? (
              <EmptyState>This server has no published versions yet.</EmptyState>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </section>
  )
}
