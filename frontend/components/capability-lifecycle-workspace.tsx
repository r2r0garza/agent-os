"use client"

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react"
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  LoaderCircle,
  PackageCheck,
  RefreshCw,
  ServerCog,
  ShieldCheck,
} from "lucide-react"

import {
  Agent,
  AgentVersion,
  ApiError,
  McpServer,
  McpServerHealthCheck,
  McpServerTool,
  McpServerVersion,
  Skill,
  SkillPackageExport,
  SkillVersion,
  api,
  jsonBody,
  patchBody,
} from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"

interface CapabilityLifecycleWorkspaceProps {
  agents: Agent[]
  skills: Skill[]
  servers: McpServer[]
  onInventoryRefresh?: () => Promise<void>
}

function splitList(value: FormDataEntryValue | null) {
  return String(value ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
}

function statusVariant(status: string) {
  return status === "healthy" || status === "valid"
    ? "outline"
    : status === "degraded"
      ? "secondary"
      : "destructive"
}

function shortHash(value: string | null) {
  return value ? `${value.slice(0, 12)}…` : "not recorded"
}

export function CapabilityLifecycleWorkspace({
  agents,
  skills,
  servers,
  onInventoryRefresh,
}: CapabilityLifecycleWorkspaceProps) {
  const [skillVersions, setSkillVersions] = useState<Record<string, SkillVersion[]>>({})
  const [serverVersions, setServerVersions] = useState<Record<string, McpServerVersion[]>>({})
  const [selectedSkillId, setSelectedSkillId] = useState("")
  const [selectedServerId, setSelectedServerId] = useState("")
  const [healthChecks, setHealthChecks] = useState<McpServerHealthCheck[]>([])
  const [tools, setTools] = useState<McpServerTool[]>([])
  const [exportBundle, setExportBundle] = useState<SkillPackageExport | null>(null)
  const [latestAgentVersion, setLatestAgentVersion] = useState<AgentVersion | null>(null)
  const [loading, setLoading] = useState(true)
  const [mutation, setMutation] = useState("")
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [unauthorized, setUnauthorized] = useState(false)

  const loadDefinitions = useCallback(async () => {
    setLoading(true)
    setError("")
    setUnauthorized(false)
    try {
      const [skillEntries, serverEntries] = await Promise.all([
        Promise.all(
          skills.map(async (skill) => [
            skill.id,
            await api<SkillVersion[]>(`/skills/${skill.id}/versions`),
          ] as const)
        ),
        Promise.all(
          servers.map(async (server) => [
            server.id,
            await api<McpServerVersion[]>(`/mcp-servers/${server.id}/versions`),
          ] as const)
        ),
      ])
      setSkillVersions(Object.fromEntries(skillEntries))
      setServerVersions(Object.fromEntries(serverEntries))
      const nextSkillId =
        selectedSkillId && skills.some((item) => item.id === selectedSkillId)
          ? selectedSkillId
          : skills[0]?.id ?? ""
      const nextServerId =
        selectedServerId && servers.some((item) => item.id === selectedServerId)
          ? selectedServerId
          : servers[0]?.id ?? ""
      setSelectedSkillId(nextSkillId)
      setSelectedServerId(nextServerId)
      const nextServerVersion = Object.fromEntries(serverEntries)[nextServerId]?.at(-1)
      if (nextServerId && nextServerVersion) {
        await loadMcpEvidence(nextServerId, nextServerVersion.version_number)
      } else {
        setHealthChecks([])
        setTools([])
      }
    } catch (reason) {
      setUnauthorized(reason instanceof ApiError && (reason.status === 401 || reason.status === 403))
      setError(reason instanceof Error ? reason.message : "Unable to load capability definitions")
    } finally {
      setLoading(false)
    }
  }, [selectedServerId, selectedSkillId, servers, skills])

  useEffect(() => {
    // The inventory is external API state; refresh it when the parent inventory changes.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadDefinitions()
  }, [loadDefinitions])

  const selectedSkillVersions = skillVersions[selectedSkillId] ?? []
  const selectedSkillVersion = selectedSkillVersions.at(-1)
  const selectedServerVersions = serverVersions[selectedServerId] ?? []
  const selectedServerVersion = selectedServerVersions.at(-1)

  async function mutate(label: string, action: () => Promise<void>) {
    setMutation(label)
    setError("")
    setNotice("")
    try {
      await action()
    } catch (reason) {
      setUnauthorized(reason instanceof ApiError && (reason.status === 401 || reason.status === 403))
      setError(reason instanceof Error ? reason.message : "Capability action failed")
    } finally {
      setMutation("")
    }
  }

  async function loadMcpEvidence(serverId: string, versionNumber: number) {
    try {
      const [checks, discovered] = await Promise.all([
        api<McpServerHealthCheck[]>(
          `/mcp-servers/${serverId}/versions/${versionNumber}/health-checks`
        ),
        api<McpServerTool[]>(
          `/mcp-servers/${serverId}/versions/${versionNumber}/discovered-tools`
        ),
      ])
      setHealthChecks(checks)
      setTools(discovered)
    } catch (reason) {
      setUnauthorized(reason instanceof ApiError && (reason.status === 401 || reason.status === 403))
      setError(reason instanceof Error ? reason.message : "Unable to load MCP evidence")
    }
  }

  async function authorSkillPackage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selectedSkillId) return
    const form = new FormData(event.currentTarget)
    await mutate("skill-author", async () => {
      const resourcePath = String(form.get("resource_path") ?? "references/guide.md")
      const imported = String(form.get("package_json") ?? "").trim()
      const payload = imported
        ? JSON.parse(imported)
        : {
            manifest: {
              name: form.get("manifest_name"),
              description: form.get("description"),
              resources: [resourcePath],
            },
            instructions: form.get("instructions"),
            resources: [{
              path: resourcePath,
              content: form.get("resource_content"),
              media_type: "text/markdown",
              metadata: { audience: "agent" },
            }],
            declared_capabilities: splitList(form.get("capabilities")),
            provenance: { source: form.get("provenance") || "authored" },
          }
      await api(`/skills/${selectedSkillId}/versions`, jsonBody(payload))
      await loadDefinitions()
      setNotice("Immutable skill package version validated and created.")
    })
  }

  async function exportSkill() {
    if (!selectedSkillId || !selectedSkillVersion) return
    await mutate("skill-export", async () => {
      setExportBundle(await api<SkillPackageExport>(
        `/skills/${selectedSkillId}/versions/${selectedSkillVersion.version_number}/export`
      ))
      setNotice("Redacted export bundle loaded for inspection.")
    })
  }

  async function installSkill() {
    if (!selectedSkillId || !selectedSkillVersion) return
    await mutate("skill-install", async () => {
      await api(
        `/skills/${selectedSkillId}/versions/${selectedSkillVersion.version_number}/install`,
        jsonBody({})
      )
      await onInventoryRefresh?.()
      setNotice("Accessible skill definition installed separately from agent grants.")
    })
  }

  async function createServerVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selectedServerId) return
    const form = new FormData(event.currentTarget)
    await mutate("server-version", async () => {
      await api(`/mcp-servers/${selectedServerId}/versions`, jsonBody({
        connection_config: {
          url: form.get("url"),
          credential_required: form.get("credential_required") === "on",
        },
        credential: form.get("credential") || null,
      }))
      await loadDefinitions()
      setNotice("Immutable MCP connection version created; credentials remain redacted.")
    })
  }

  async function discoverTools() {
    if (!selectedServerId || !selectedServerVersion) return
    await mutate("discover", async () => {
      const check = await api<McpServerHealthCheck>(
        `/mcp-servers/${selectedServerId}/versions/${selectedServerVersion.version_number}/health-checks`,
        jsonBody({ timeout_seconds: 2, max_attempts: 2 })
      )
      await loadMcpEvidence(selectedServerId, selectedServerVersion.version_number)
      setNotice(`MCP discovery completed with ${check.status} health.`)
    })
  }

  async function installMcpServer() {
    if (!selectedServerId || !selectedServerVersion) return
    await mutate("server-install", async () => {
      await api(
        `/mcp-servers/${selectedServerId}/versions/${selectedServerVersion.version_number}/install`,
        jsonBody({})
      )
      await onInventoryRefresh?.()
      setNotice("Accessible MCP definition installed separately from agent grants.")
    })
  }

  async function updateTool(tool: McpServerTool, form: HTMLFormElement) {
    if (!selectedServerId || !selectedServerVersion) return
    const values = new FormData(form)
    await mutate(`tool-${tool.id}`, async () => {
      await api(
        `/mcp-servers/${selectedServerId}/versions/${selectedServerVersion.version_number}/discovered-tools/${encodeURIComponent(tool.tool_name)}`,
        patchBody({
          enabled: values.get("enabled") === "on",
          timeout_ms: Number(values.get("timeout_ms")) || null,
          output_limit_bytes: Number(values.get("output_limit_bytes")) || null,
        })
      )
      await loadMcpEvidence(selectedServerId, selectedServerVersion.version_number)
      setNotice(`Tool ${tool.tool_name} settings saved.`)
    })
  }

  async function grantCapabilities(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const agentId = String(form.get("agent_id") ?? "")
    if (!agentId) return
    await mutate("grant", async () => {
      const skillResourcePaths = splitList(form.get("skill_resources"))
      const toolNames = splitList(form.get("tool_names"))
      const version = await api<AgentVersion>(`/agents/${agentId}/versions`, jsonBody({
        instructions: "Use only the explicitly granted capability resources.",
        skill_grants: selectedSkillVersion ? [{
          version_id: selectedSkillVersion.id,
          resource_paths: skillResourcePaths,
          policy_metadata: { decision: "allow" },
        }] : [],
        mcp_tool_grants: selectedServerVersion && toolNames.length ? [{
          version_id: selectedServerVersion.id,
          tool_names: toolNames,
          policy_metadata: { decision: "allow" },
        }] : [],
      }))
      setLatestAgentVersion(version)
      setNotice(`Agent version ${version.version_number} created with explicit capability grants.`)
    })
  }

  const latestHealth = healthChecks[0]
  const enabledToolNames = useMemo(
    () => tools.filter((tool) => tool.enabled && tool.schema_valid).map((tool) => tool.tool_name),
    [tools]
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <PackageCheck className="size-5" /> Governed capability packages
        </CardTitle>
        <CardDescription>
          Author or import immutable skill packages, discover untrusted MCP descriptors,
          and grant selected resources separately from definition ownership.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-5">
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircle className="size-4 animate-spin" /> Loading capability lifecycle…
          </div>
        ) : error ? (
          <Alert variant="destructive">
            <AlertTriangle className="size-4" />
            <AlertTitle>{unauthorized ? "Capability access denied" : "Capability workflow unavailable"}</AlertTitle>
            <AlertDescription className="flex items-center justify-between gap-3">
              <span>{error}</span>
              <Button variant="outline" size="sm" onClick={() => void loadDefinitions()}>
                <RefreshCw className="size-3" /> Retry
              </Button>
            </AlertDescription>
          </Alert>
        ) : null}
        {notice ? (
          <Alert>
            <CheckCircle2 className="size-4" />
            <AlertDescription>{notice}</AlertDescription>
          </Alert>
        ) : null}

        {!loading && !skills.length && !servers.length ? (
          <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
            Create a skill or MCP definition first; no governed capability definitions are visible.
          </div>
        ) : null}

        <div className="grid gap-5 xl:grid-cols-2">
          <section className="grid content-start gap-3 rounded-xl border p-4">
            <div>
              <h3 className="font-medium">Skill package lifecycle</h3>
              <p className="text-xs text-muted-foreground">
                Package hashes, provenance, validation diagnostics, and resource hashes are immutable evidence.
              </p>
            </div>
            <Label htmlFor="package-skill">Definition</Label>
            <select id="package-skill" className="h-9 rounded-md border bg-background px-3 text-sm"
              value={selectedSkillId} onChange={(event) => {
                setSelectedSkillId(event.target.value)
                setExportBundle(null)
              }}>
              <option value="">No skill selected</option>
              {skills.map((skill) => <option key={skill.id} value={skill.id}>{skill.name} · {skill.visibility}</option>)}
            </select>
            {selectedSkillVersion ? (
              <div className="rounded-lg bg-muted/30 p-3 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={statusVariant(selectedSkillVersion.validation_status)}>
                    {selectedSkillVersion.validation_status}
                  </Badge>
                  <span>v{selectedSkillVersion.version_number}</span>
                  <span>package {shortHash(selectedSkillVersion.package_hash)}</span>
                  <span>{selectedSkillVersion.resources.length} resources</span>
                </div>
                <p className="mt-2">Provenance: {JSON.stringify(selectedSkillVersion.provenance)}</p>
                {selectedSkillVersion.resources.map((resource) => (
                  <p key={resource.path} className="mt-1">
                    {resource.path} · {shortHash(resource.sha256 ?? null)}
                  </p>
                ))}
              </div>
            ) : <p className="text-xs text-muted-foreground">No package versions yet.</p>}
            <form className="grid gap-2" onSubmit={authorSkillPackage}>
              <Input name="manifest_name" placeholder="Manifest name" />
              <Input name="description" placeholder="Package description" />
              <Textarea name="instructions" placeholder="Instructions" />
              <Input name="resource_path" defaultValue="references/guide.md" required />
              <Textarea name="resource_content" placeholder="# Guide" />
              <Input name="capabilities" placeholder="research, summarize" />
              <Input name="provenance" defaultValue="authored" />
              <Textarea
                name="package_json"
                placeholder="Paste package JSON to import (optional)"
              />
              <Button type="submit" disabled={!selectedSkillId || Boolean(mutation)}>
                {mutation === "skill-author" ? <LoaderCircle className="animate-spin" /> : <PackageCheck />}
                Validate author/import
              </Button>
            </form>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" disabled={!selectedSkillVersion || Boolean(mutation)} onClick={() => void exportSkill()}>
                <Download /> Inspect redacted export
              </Button>
              <Button variant="outline" disabled={!selectedSkillVersion || Boolean(mutation)} onClick={() => void installSkill()}>
                Install accessible definition
              </Button>
            </div>
            {exportBundle ? (
              <pre className="max-h-56 overflow-auto rounded-lg bg-muted p-3 text-[11px]">
                {JSON.stringify(exportBundle, null, 2)}
              </pre>
            ) : null}
          </section>

          <section className="grid content-start gap-3 rounded-xl border p-4">
            <div>
              <h3 className="flex items-center gap-2 font-medium"><ServerCog className="size-4" /> MCP discovery & health</h3>
              <p className="text-xs text-muted-foreground">
                Remote descriptions are untrusted evidence; enable tools only after schema review.
              </p>
            </div>
            <Label htmlFor="mcp-server">Definition</Label>
            <select id="mcp-server" className="h-9 rounded-md border bg-background px-3 text-sm"
              value={selectedServerId} onChange={(event) => {
                const serverId = event.target.value
                setSelectedServerId(serverId)
                const version = serverVersions[serverId]?.at(-1)
                if (serverId && version) void loadMcpEvidence(serverId, version.version_number)
                else {
                  setHealthChecks([])
                  setTools([])
                }
              }}>
              <option value="">No MCP server selected</option>
              {servers.map((server) => <option key={server.id} value={server.id}>{server.name} · {server.visibility}</option>)}
            </select>
            <form className="grid gap-2 sm:grid-cols-2" onSubmit={createServerVersion}>
              <Input name="url" type="url" placeholder="https://mcp.example/mcp" required />
              <Input name="credential" type="password" placeholder="Optional credential" />
              <label className="flex items-center gap-2 text-xs">
                <input name="credential_required" type="checkbox" /> Credential required
              </label>
              <Button type="submit" disabled={!selectedServerId || Boolean(mutation)}>Create connection version</Button>
            </form>
            <div className="flex items-center gap-2">
              <Button variant="outline" disabled={!selectedServerVersion || Boolean(mutation)} onClick={() => void discoverTools()}>
                {mutation === "discover" ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}
                Discover & check health
              </Button>
              {latestHealth ? (
                <Badge variant={statusVariant(latestHealth.status)}>
                  {latestHealth.status} · {latestHealth.tool_count} tools
                  {latestHealth.latency_ms != null ? ` · ${latestHealth.latency_ms} ms` : ""}
                </Badge>
              ) : <Badge variant="secondary">unprobed</Badge>}
              <Button
                variant="outline"
                disabled={!selectedServerVersion || Boolean(mutation)}
                onClick={() => void installMcpServer()}
              >
                Install definition
              </Button>
            </div>
            {latestHealth?.diagnostics.length ? (
              <p className="text-xs text-destructive">
                Diagnostics: {latestHealth.diagnostics.map((item) => String(item.code ?? "unknown")).join(", ")}
              </p>
            ) : null}
            <div className="grid gap-2">
              {tools.map((tool) => (
                <form key={tool.id} className="grid gap-2 rounded-lg border p-3 text-xs"
                  onSubmit={(event) => { event.preventDefault(); void updateTool(tool, event.currentTarget) }}>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium">{tool.tool_name}</span>
                    <div className="flex gap-1">
                      <Badge variant={tool.schema_valid ? "outline" : "destructive"}>
                        schema {tool.schema_valid ? "valid" : "invalid"}
                      </Badge>
                      {tool.credential_scope_required ? <Badge variant="secondary">credential scope required</Badge> : null}
                    </div>
                  </div>
                  <p className="text-muted-foreground">{tool.description || "No remote description"}</p>
                  <p>descriptor {shortHash(tool.descriptor_hash)}</p>
                  <div className="grid grid-cols-3 gap-2">
                    <label className="flex items-center gap-2"><input name="enabled" type="checkbox" defaultChecked={tool.enabled} /> Enabled</label>
                    <Input name="timeout_ms" type="number" defaultValue={tool.timeout_ms ?? ""} placeholder="Timeout ms" />
                    <Input name="output_limit_bytes" type="number" defaultValue={tool.output_limit_bytes ?? ""} placeholder="Output bytes" />
                  </div>
                  <Button type="submit" size="sm" variant="outline" disabled={Boolean(mutation) || !tool.schema_valid}>Save tool policy</Button>
                </form>
              ))}
              {selectedServerVersion && !tools.length ? (
                <p className="text-xs text-muted-foreground">No tools discovered. Run a health check or inspect unreachable/malformed diagnostics.</p>
              ) : null}
            </div>
          </section>
        </div>

        <section className="grid gap-3 rounded-xl border p-4">
          <div>
            <h3 className="flex items-center gap-2 font-medium"><ShieldCheck className="size-4" /> Agent capability grants</h3>
            <p className="text-xs text-muted-foreground">
              Owning or installing a definition does not grant permission to use it. This creates a new agent version with only the selected resources and tools.
            </p>
          </div>
          <form className="grid gap-3 md:grid-cols-4" onSubmit={grantCapabilities}>
            <select name="agent_id" aria-label="Agent" className="h-9 rounded-md border bg-background px-3 text-sm" required>
              <option value="">Select agent</option>
              {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
            </select>
            <Input name="skill_resources" placeholder="references/guide.md" defaultValue={selectedSkillVersion?.resources.map((item) => item.path).join(", ") ?? ""} />
            <Input name="tool_names" placeholder="Enabled tools" defaultValue={enabledToolNames.join(", ")} />
            <Button type="submit" disabled={!agents.length || Boolean(mutation)}>Create granted agent version</Button>
          </form>
          {latestAgentVersion ? (
            <div className="rounded-lg bg-muted/30 p-3 text-xs">
              Agent version {latestAgentVersion.version_number}: {latestAgentVersion.skill_grants.length} skill grant(s),{" "}
              {latestAgentVersion.mcp_tool_grants.length} MCP grant(s). Grant actor and descriptor/package hashes are preserved; policy metadata is redacted.
            </div>
          ) : null}
        </section>
      </CardContent>
    </Card>
  )
}
