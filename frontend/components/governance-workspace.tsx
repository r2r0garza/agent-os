"use client"

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react"
import {
  Agent,
  AgentVersion,
  Budget,
  Credential,
  McpServer,
  McpServerVersion,
  ModelProfile,
  ModelProfileVersion,
  PolicySet,
  PolicySetVersion,
  Skill,
  SkillVersion,
  api,
  jsonBody,
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
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Boxes,
  CircleDollarSign,
  KeyRound,
  LoaderCircle,
  Puzzle,
  ServerCog,
  ShieldCheck,
  Wrench,
} from "lucide-react"

function displayDate(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}

function Field({
  label,
  children,
  hint,
}: {
  label: string
  children: React.ReactNode
  hint?: string
}) {
  return (
    <div className="grid gap-1.5">
      <Label>{label}</Label>
      {children}
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  )
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed bg-muted/20 px-4 py-6 text-center text-xs text-muted-foreground">
      {children}
    </div>
  )
}

function parseJsonRecord(raw: FormDataEntryValue | null, label: string): Record<string, unknown> {
  const text = String(raw ?? "").trim()
  if (!text) return {}
  try {
    const parsed = JSON.parse(text)
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("expected a JSON object")
    }
    return parsed as Record<string, unknown>
  } catch {
    throw new Error(`${label} must be valid JSON (an object).`)
  }
}

function parseJsonArray(raw: FormDataEntryValue | null, label: string): Record<string, unknown>[] {
  const text = String(raw ?? "").trim()
  if (!text) return []
  try {
    const parsed = JSON.parse(text)
    if (!Array.isArray(parsed)) throw new Error("expected a JSON array")
    return parsed as Record<string, unknown>[]
  } catch {
    throw new Error(`${label} must be a valid JSON array.`)
  }
}

export interface GovernanceLookups {
  skillVersionName: Record<string, string>
  mcpVersionName: Record<string, string>
  modelProfileVersionName: Record<string, string>
  policySetVersionName: Record<string, string>
}

interface GovernanceWorkspaceProps {
  agents: Agent[]
  models: ModelProfile[]
  skills: Skill[]
  servers: McpServer[]
  onLookupsChange?: (lookups: GovernanceLookups) => void
}

export function GovernanceWorkspace({
  agents,
  models,
  skills,
  servers,
  onLookupsChange,
}: GovernanceWorkspaceProps) {
  const [credentials, setCredentials] = useState<Credential[]>([])
  const [policySets, setPolicySets] = useState<PolicySet[]>([])
  const [policySetVersions, setPolicySetVersions] = useState<
    Record<string, PolicySetVersion[]>
  >({})
  const [skillVersions, setSkillVersions] = useState<
    Record<string, SkillVersion[]>
  >({})
  const [mcpVersions, setMcpVersions] = useState<
    Record<string, McpServerVersion[]>
  >({})
  const [modelVersions, setModelVersions] = useState<
    Record<string, ModelProfileVersion[]>
  >({})
  const [agentVersions, setAgentVersions] = useState<
    Record<string, AgentVersion[]>
  >({})
  const [budgets, setBudgets] = useState<Record<string, Budget[]>>({})

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [mutation, setMutation] = useState("")

  const [selectedAgentId, setSelectedAgentId] = useState("")
  const [skillAttachmentIds, setSkillAttachmentIds] = useState<string[]>([])
  const [mcpAttachmentIds, setMcpAttachmentIds] = useState<string[]>([])
  const [policyAttachmentIds, setPolicyAttachmentIds] = useState<string[]>([])

  const agentKey = useMemo(
    () => agents.map((agent) => agent.id).join(","),
    [agents]
  )
  const skillKey = useMemo(() => skills.map((skill) => skill.id).join(","), [skills])
  const serverKey = useMemo(
    () => servers.map((server) => server.id).join(","),
    [servers]
  )
  const modelKey = useMemo(() => models.map((model) => model.id).join(","), [models])

  const load = useCallback(async () => {
    setError("")
    try {
      const [credentialList, policySetList] = await Promise.all([
        api<Credential[]>("/credentials"),
        api<PolicySet[]>("/policy-sets"),
      ])
      setCredentials(credentialList)
      setPolicySets(policySetList)

      const policyVersionEntries = await Promise.all(
        policySetList.map(
          async (set) =>
            [set.id, await api<PolicySetVersion[]>(`/policy-sets/${set.id}/versions`)] as const
        )
      )
      setPolicySetVersions(Object.fromEntries(policyVersionEntries))

      const skillVersionEntries = await Promise.all(
        skills.map(
          async (skill) =>
            [skill.id, await api<SkillVersion[]>(`/skills/${skill.id}/versions`)] as const
        )
      )
      setSkillVersions(Object.fromEntries(skillVersionEntries))

      const mcpVersionEntries = await Promise.all(
        servers.map(
          async (server) =>
            [server.id, await api<McpServerVersion[]>(`/mcp-servers/${server.id}/versions`)] as const
        )
      )
      setMcpVersions(Object.fromEntries(mcpVersionEntries))

      const modelVersionEntries = await Promise.all(
        models.map(
          async (model) =>
            [model.id, await api<ModelProfileVersion[]>(`/model-profiles/${model.id}/versions`)] as const
        )
      )
      setModelVersions(Object.fromEntries(modelVersionEntries))

      const agentVersionEntries = await Promise.all(
        agents.map(
          async (agent) =>
            [agent.id, await api<AgentVersion[]>(`/agents/${agent.id}/versions`)] as const
        )
      )
      setAgentVersions(Object.fromEntries(agentVersionEntries))

      const budgetEntries = await Promise.all(
        agents.map(
          async (agent) =>
            [agent.id, await api<Budget[]>(`/agents/${agent.id}/budgets`)] as const
        )
      )
      setBudgets(Object.fromEntries(budgetEntries))

      setSelectedAgentId((current) =>
        current && agents.some((agent) => agent.id === current)
          ? current
          : (agents.at(-1)?.id ?? "")
      )
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to load governance state"
      )
    } finally {
      setLoading(false)
    }
    // Reload whenever the parent's inventory of agents/skills/servers/models changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentKey, skillKey, serverKey, modelKey])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  useEffect(() => {
    if (!onLookupsChange) return
    const skillVersionName: Record<string, string> = {}
    for (const skill of skills) {
      for (const version of skillVersions[skill.id] ?? []) {
        skillVersionName[version.id] = `${skill.name} · v${version.version_number}`
      }
    }
    const mcpVersionName: Record<string, string> = {}
    for (const server of servers) {
      for (const version of mcpVersions[server.id] ?? []) {
        mcpVersionName[version.id] = `${server.name} · v${version.version_number}`
      }
    }
    const modelProfileVersionName: Record<string, string> = {}
    for (const model of models) {
      for (const version of modelVersions[model.id] ?? []) {
        modelProfileVersionName[version.id] = `${model.name} · v${version.version_number}`
      }
    }
    const policySetVersionName: Record<string, string> = {}
    for (const set of policySets) {
      for (const version of policySetVersions[set.id] ?? []) {
        policySetVersionName[version.id] = `${set.name} · v${version.version_number}`
      }
    }
    onLookupsChange({
      skillVersionName,
      mcpVersionName,
      modelProfileVersionName,
      policySetVersionName,
    })
  }, [
    onLookupsChange,
    skills,
    servers,
    models,
    policySets,
    skillVersions,
    mcpVersions,
    modelVersions,
    policySetVersions,
  ])

  async function mutate(name: string, work: () => Promise<void>) {
    setMutation(name)
    setError("")
    setNotice("")
    try {
      await work()
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Request failed")
    } finally {
      setMutation("")
    }
  }

  function skillVersionLabel(versionId: string): string {
    for (const skill of skills) {
      const match = (skillVersions[skill.id] ?? []).find((v) => v.id === versionId)
      if (match) return `${skill.name} · v${match.version_number}`
    }
    return `unknown skill version (${versionId.slice(0, 8)})`
  }

  function mcpVersionLabel(versionId: string): string {
    for (const server of servers) {
      const match = (mcpVersions[server.id] ?? []).find((v) => v.id === versionId)
      if (match) return `${server.name} · v${match.version_number}`
    }
    return `unknown MCP version (${versionId.slice(0, 8)})`
  }

  function policyVersionLabel(versionId: string): string {
    for (const set of policySets) {
      const match = (policySetVersions[set.id] ?? []).find((v) => v.id === versionId)
      if (match) return `${set.name} · v${match.version_number}`
    }
    return `unknown policy version (${versionId.slice(0, 8)})`
  }

  function modelProfileLabel(profileId: string | null): string {
    if (!profileId) return "No model profile pinned"
    const profile = models.find((model) => model.id === profileId)
    return profile ? profile.name : `unknown model profile (${profileId.slice(0, 8)})`
  }

  function budgetLabel(agentId: string, budgetId: string | null): string {
    if (!budgetId) return "No default budget"
    const budget = (budgets[agentId] ?? []).find((entry) => entry.id === budgetId)
    if (!budget) return `unknown budget (${budgetId.slice(0, 8)})`
    const amount = (budget.amount_minor_units / 100).toFixed(2)
    return `${amount} ${budget.currency} · ${budget.enforcement_mode.replace("_", " ")}`
  }

  async function createCredential(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    await mutate("credential", async () => {
      await api<Credential>(
        "/credentials",
        jsonBody({
          name: form.get("name"),
          credential_type: form.get("credential_type"),
          material: form.get("material"),
        })
      )
      setNotice("Credential stored encrypted. Its material is never returned.")
      formElement.reset()
    })
  }

  async function createPolicySet(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    await mutate("policy-set", async () => {
      await api<PolicySet>("/policy-sets", jsonBody({ name: form.get("name") }))
      setNotice("Policy set created. Add a version with rules to enforce it.")
      formElement.reset()
    })
  }

  async function createPolicySetVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    const policySetId = String(form.get("policy_set_id") ?? "")
    if (!policySetId) {
      setError("Select a policy set before adding a version.")
      return
    }
    await mutate("policy-version", async () => {
      const rules = parseJsonArray(form.get("rules"), "Policy rules")
      await api<PolicySetVersion>(
        `/policy-sets/${policySetId}/versions`,
        jsonBody({ rules })
      )
      setNotice("Policy set version pinned and ready to attach to an agent version.")
      formElement.reset()
    })
  }

  async function createModelProfileVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    const modelProfileId = String(form.get("model_profile_id") ?? "")
    const credentialId = String(form.get("credential_id") ?? "")
    if (!modelProfileId || !credentialId) {
      setError("Select a model profile and a credential before adding a version.")
      return
    }
    await mutate("model-version", async () => {
      const headers = parseJsonRecord(form.get("headers"), "Headers")
      await api<ModelProfileVersion>(
        `/model-profiles/${modelProfileId}/versions`,
        jsonBody({
          base_url: form.get("base_url"),
          model_identifier: form.get("model_identifier"),
          credential_id: credentialId,
          headers,
        })
      )
      setNotice("Model profile version pinned.")
      formElement.reset()
    })
  }

  async function createSkillVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    const skillId = String(form.get("skill_id") ?? "")
    if (!skillId) {
      setError("Select a skill before adding a version.")
      return
    }
    await mutate("skill-version", async () => {
      const resourceMetadata = parseJsonRecord(
        form.get("resource_metadata"),
        "Resource metadata"
      )
      await api<SkillVersion>(
        `/skills/${skillId}/versions`,
        jsonBody({
          content_ref: form.get("content_ref"),
          resource_metadata: resourceMetadata,
        })
      )
      setNotice("Skill version pinned and ready to attach to an agent version.")
      formElement.reset()
    })
  }

  async function createMcpServerVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    const mcpServerId = String(form.get("mcp_server_id") ?? "")
    if (!mcpServerId) {
      setError("Select an MCP server before adding a version.")
      return
    }
    await mutate("mcp-version", async () => {
      const connectionConfig = parseJsonRecord(
        form.get("connection_config"),
        "Connection config"
      )
      await api<McpServerVersion>(
        `/mcp-servers/${mcpServerId}/versions`,
        jsonBody({ connection_config: connectionConfig })
      )
      setNotice("MCP server version pinned and ready to attach to an agent version.")
      formElement.reset()
    })
  }

  async function createBudget(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    const agentId = String(form.get("agent_id") ?? "")
    if (!agentId) {
      setError("Select an agent before assigning a budget.")
      return
    }
    await mutate("budget", async () => {
      await api<Budget>(
        `/agents/${agentId}/budgets`,
        jsonBody({
          currency: form.get("currency"),
          amount_minor_units: Number(form.get("amount_minor_units")),
          enforcement_mode: form.get("enforcement_mode"),
          warning_threshold_percent: Number(form.get("warning_threshold_percent") || 80),
        })
      )
      setNotice("Budget assigned. Pin it to an agent version to enforce it.")
      formElement.reset()
    })
  }

  async function createAgentVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    const agentId = String(form.get("agent_id") ?? "")
    if (!agentId) {
      setError("Select an agent before creating a version.")
      return
    }
    const modelProfileId = String(form.get("model_profile_id") ?? "")
    const defaultBudgetId = String(form.get("default_budget_id") ?? "")
    await mutate("agent-version", async () => {
      await api<AgentVersion>(
        `/agents/${agentId}/versions`,
        jsonBody({
          instructions: form.get("instructions"),
          model_profile_id: modelProfileId || null,
          default_budget_id: defaultBudgetId || null,
          capability_manifest: {},
          skill_attachments: skillAttachmentIds.map((versionId) => ({
            version_id: versionId,
            config: {},
          })),
          mcp_server_attachments: mcpAttachmentIds.map((versionId) => ({
            version_id: versionId,
            config: {},
          })),
          policy_set_version_ids: policyAttachmentIds,
        })
      )
      setNotice(
        "Governed agent version pinned with its skill, MCP, policy, model, and budget snapshot."
      )
      formElement.reset()
      setSkillAttachmentIds([])
      setMcpAttachmentIds([])
      setPolicyAttachmentIds([])
    })
  }

  function toggleId(list: string[], id: string, setList: (next: string[]) => void) {
    setList(list.includes(id) ? list.filter((entry) => entry !== id) : [...list, id])
  }

  if (loading) {
    return (
      <Card className="mb-6">
        <CardContent className="flex items-center gap-3 py-8 text-sm text-muted-foreground">
          <LoaderCircle className="size-4 animate-spin" />
          Loading governed configuration…
        </CardContent>
      </Card>
    )
  }

  const selectedAgentBudgets = budgets[selectedAgentId] ?? []
  const selectedAgentVersions = [...(agentVersions[selectedAgentId] ?? [])].sort(
    (a, b) => b.version_number - a.version_number
  )
  const allSkillVersions = skills.flatMap((skill) =>
    (skillVersions[skill.id] ?? []).map((version) => ({ skill, version }))
  )
  const allMcpVersions = servers.flatMap((server) =>
    (mcpVersions[server.id] ?? []).map((version) => ({ server, version }))
  )
  const allPolicyVersions = policySets.flatMap((set) =>
    (policySetVersions[set.id] ?? []).map((version) => ({ set, version }))
  )

  return (
    <section className="mb-6 grid gap-6">
      <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
        <ShieldCheck className="size-4" /> GOVERNED AGENT CONFIGURATION
      </div>

      {error ? (
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          {error}
        </div>
      ) : null}
      {notice ? (
        <div className="rounded-xl border bg-background p-4 text-sm">{notice}</div>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <KeyRound className="size-4" /> Credentials
            </CardTitle>
            <CardDescription>
              Encrypted at rest and never returned after creation. Only redacted
              metadata and a &quot;configured&quot; flag are visible here.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <form className="grid gap-3 sm:grid-cols-2" onSubmit={createCredential}>
              <Field label="Name">
                <Input name="name" placeholder="Primary OpenAI key" required />
              </Field>
              <Field label="Type">
                <select
                  name="credential_type"
                  className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                  defaultValue="api_key"
                >
                  <option value="api_key">API key</option>
                  <option value="oauth_token">OAuth token</option>
                  <option value="other">Other</option>
                </select>
              </Field>
              <div className="sm:col-span-2">
                <Field label="Secret material" hint="Stored encrypted; redacted everywhere else.">
                  <Input name="material" type="password" required />
                </Field>
              </div>
              <div className="flex justify-end sm:col-span-2">
                <Button type="submit" size="sm" disabled={mutation === "credential"}>
                  {mutation === "credential" ? (
                    <LoaderCircle className="animate-spin" />
                  ) : (
                    <KeyRound />
                  )}
                  Store credential
                </Button>
              </div>
            </form>
            <div className="grid gap-2 border-t pt-3">
              {credentials.length ? (
                credentials.map((credential) => (
                  <div
                    key={credential.id}
                    className="flex items-center justify-between gap-3 rounded-lg border bg-background p-2.5 text-xs"
                  >
                    <div>
                      <p className="font-medium">{credential.name}</p>
                      <p className="text-muted-foreground">
                        {credential.credential_type} · {displayDate(credential.created_at)}
                      </p>
                    </div>
                    <Badge variant={credential.configured ? "outline" : "destructive"}>
                      {credential.configured ? "configured" : "not configured"}
                    </Badge>
                  </div>
                ))
              ) : (
                <EmptyState>No credentials stored yet.</EmptyState>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ShieldCheck className="size-4" /> Policy sets
            </CardTitle>
            <CardDescription>
              Versioned rule sets attach to agent versions and gate model, tool,
              and side-effect actions.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <form className="flex items-end gap-3" onSubmit={createPolicySet}>
              <div className="flex-1">
                <Field label="New policy set">
                  <Input name="name" placeholder="Default guardrails" required />
                </Field>
              </div>
              <Button type="submit" size="sm" disabled={mutation === "policy-set"}>
                {mutation === "policy-set" ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <ShieldCheck />
                )}
                Create
              </Button>
            </form>
            <form className="grid gap-3" onSubmit={createPolicySetVersion}>
              <Field label="Policy set">
                <select
                  name="policy_set_id"
                  className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                  defaultValue=""
                  disabled={!policySets.length}
                >
                  <option value="" disabled>
                    {policySets.length ? "Select a policy set" : "Create a policy set first"}
                  </option>
                  {policySets.map((set) => (
                    <option key={set.id} value={set.id}>
                      {set.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label="Rules (JSON array)"
                hint='Example: [{"action": "tool_call", "decision": "allow"}]'
              >
                <Textarea
                  name="rules"
                  className="min-h-20 font-mono text-xs"
                  placeholder="[]"
                  disabled={!policySets.length}
                />
              </Field>
              <Button
                type="submit"
                size="sm"
                className="justify-self-end"
                disabled={!policySets.length || mutation === "policy-version"}
              >
                {mutation === "policy-version" ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <ShieldCheck />
                )}
                Add version
              </Button>
            </form>
            <div className="grid gap-2 border-t pt-3">
              {allPolicyVersions.length ? (
                allPolicyVersions.map(({ set, version }) => (
                  <div
                    key={version.id}
                    className="rounded-lg border bg-background p-2.5 text-xs"
                  >
                    <p className="font-medium">
                      {set.name} · v{version.version_number}
                    </p>
                    <p className="mt-1 text-muted-foreground">
                      {version.rules.length} rule{version.rules.length === 1 ? "" : "s"} ·{" "}
                      {displayDate(version.created_at)}
                    </p>
                  </div>
                ))
              ) : (
                <EmptyState>No policy set versions pinned yet.</EmptyState>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Boxes className="size-4" /> Model profile versions
            </CardTitle>
            <CardDescription>
              Pin a new base URL, identifier, or credential for an existing model
              profile without breaking already-running agent versions.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <form className="grid gap-3 sm:grid-cols-2" onSubmit={createModelProfileVersion}>
              <Field label="Model profile">
                <select
                  name="model_profile_id"
                  className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                  defaultValue=""
                  disabled={!models.length}
                >
                  <option value="" disabled>
                    {models.length ? "Select a profile" : "Create a model profile first"}
                  </option>
                  {models.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Credential">
                <select
                  name="credential_id"
                  className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                  defaultValue=""
                  disabled={!credentials.length}
                >
                  <option value="" disabled>
                    {credentials.length ? "Select a credential" : "Store a credential first"}
                  </option>
                  {credentials.map((credential) => (
                    <option key={credential.id} value={credential.id}>
                      {credential.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Base URL">
                <Input name="base_url" type="url" defaultValue="https://api.openai.com/v1" required />
              </Field>
              <Field label="Model identifier">
                <Input name="model_identifier" placeholder="gpt-5-mini" required />
              </Field>
              <div className="sm:col-span-2">
                <Field label="Headers (JSON object, optional)">
                  <Textarea name="headers" className="min-h-16 font-mono text-xs" placeholder="{}" />
                </Field>
              </div>
              <div className="flex justify-end sm:col-span-2">
                <Button
                  type="submit"
                  size="sm"
                  disabled={!models.length || !credentials.length || mutation === "model-version"}
                >
                  {mutation === "model-version" ? (
                    <LoaderCircle className="animate-spin" />
                  ) : (
                    <Boxes />
                  )}
                  Pin version
                </Button>
              </div>
            </form>
            <div className="grid gap-2 border-t pt-3">
              {models.flatMap((model) => modelVersions[model.id] ?? []).length ? (
                models.map((model) =>
                  (modelVersions[model.id] ?? []).map((version) => (
                    <div
                      key={version.id}
                      className="rounded-lg border bg-background p-2.5 text-xs"
                    >
                      <p className="font-medium">
                        {model.name} · v{version.version_number}
                      </p>
                      <p className="mt-1 text-muted-foreground">
                        {version.model_identifier} · {version.base_url} ·{" "}
                        {displayDate(version.created_at)}
                      </p>
                    </div>
                  ))
                )
              ) : (
                <EmptyState>No pinned model profile versions yet.</EmptyState>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Puzzle className="size-4" /> Skill versions
            </CardTitle>
            <CardDescription>
              Pin a new content reference for an existing skill so agent versions
              can attach an immutable snapshot.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <form className="grid gap-3" onSubmit={createSkillVersion}>
              <Field label="Skill">
                <select
                  name="skill_id"
                  className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                  defaultValue=""
                  disabled={!skills.length}
                >
                  <option value="" disabled>
                    {skills.length ? "Select a skill" : "Create a skill first"}
                  </option>
                  {skills.map((skill) => (
                    <option key={skill.id} value={skill.id}>
                      {skill.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Content reference">
                <Input name="content_ref" placeholder="skills://research/v2" required />
              </Field>
              <Field label="Resource metadata (JSON object, optional)">
                <Textarea
                  name="resource_metadata"
                  className="min-h-16 font-mono text-xs"
                  placeholder="{}"
                />
              </Field>
              <Button
                type="submit"
                size="sm"
                className="justify-self-end"
                disabled={!skills.length || mutation === "skill-version"}
              >
                {mutation === "skill-version" ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <Puzzle />
                )}
                Pin version
              </Button>
            </form>
            <div className="grid gap-2 border-t pt-3">
              {allSkillVersions.length ? (
                allSkillVersions.map(({ skill, version }) => (
                  <div
                    key={version.id}
                    className="rounded-lg border bg-background p-2.5 text-xs"
                  >
                    <p className="font-medium">
                      {skill.name} · v{version.version_number}
                    </p>
                    <p className="mt-1 text-muted-foreground">
                      {version.content_ref} · {displayDate(version.created_at)}
                    </p>
                  </div>
                ))
              ) : (
                <EmptyState>No pinned skill versions yet.</EmptyState>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ServerCog className="size-4" /> MCP server versions
            </CardTitle>
            <CardDescription>
              Pin a new connection configuration and discovered tools for an MCP
              server.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <form className="grid gap-3" onSubmit={createMcpServerVersion}>
              <Field label="MCP server">
                <select
                  name="mcp_server_id"
                  className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                  defaultValue=""
                  disabled={!servers.length}
                >
                  <option value="" disabled>
                    {servers.length ? "Select a server" : "Create an MCP server first"}
                  </option>
                  {servers.map((server) => (
                    <option key={server.id} value={server.id}>
                      {server.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label="Connection config (JSON object)"
                hint='Example: {"transport": "test", "tools": [{"name": "echo"}]}'
              >
                <Textarea
                  name="connection_config"
                  className="min-h-20 font-mono text-xs"
                  placeholder="{}"
                />
              </Field>
              <Button
                type="submit"
                size="sm"
                className="justify-self-end"
                disabled={!servers.length || mutation === "mcp-version"}
              >
                {mutation === "mcp-version" ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <ServerCog />
                )}
                Pin version
              </Button>
            </form>
            <div className="grid gap-2 border-t pt-3">
              {allMcpVersions.length ? (
                allMcpVersions.map(({ server, version }) => (
                  <div
                    key={version.id}
                    className="rounded-lg border bg-background p-2.5 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <p className="font-medium">
                        {server.name} · v{version.version_number}
                      </p>
                      <Badge variant={version.credential_configured ? "outline" : "secondary"}>
                        {version.credential_configured ? "credentialed" : "no credential"}
                      </Badge>
                    </div>
                    <p className="mt-1 text-muted-foreground">
                      {Array.isArray(version.connection_config.tools)
                        ? `${(version.connection_config.tools as unknown[]).length} discovered tool(s)`
                        : "no discovered tools recorded"}{" "}
                      · {displayDate(version.created_at)}
                    </p>
                  </div>
                ))
              ) : (
                <EmptyState>No pinned MCP server versions yet.</EmptyState>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <CircleDollarSign className="size-4" /> Budget assignments
            </CardTitle>
            <CardDescription>
              Lifetime budgets are agent-scoped; pin one as an agent version&apos;s
              default to enforce it before side effects.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <form className="grid gap-3 sm:grid-cols-2" onSubmit={createBudget}>
              <div className="sm:col-span-2">
                <Field label="Agent">
                  <select
                    name="agent_id"
                    className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                    defaultValue=""
                    disabled={!agents.length}
                  >
                    <option value="" disabled>
                      {agents.length ? "Select an agent" : "Create an agent first"}
                    </option>
                    {agents.map((agent) => (
                      <option key={agent.id} value={agent.id}>
                        {agent.name}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              <Field label="Currency">
                <Input name="currency" defaultValue="USD" required />
              </Field>
              <Field label="Amount (minor units)">
                <Input name="amount_minor_units" type="number" min="0" defaultValue="50000" required />
              </Field>
              <Field label="Enforcement">
                <select
                  name="enforcement_mode"
                  className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                  defaultValue="hard_stop"
                >
                  <option value="hard_stop">Hard stop</option>
                  <option value="warning">Warning</option>
                </select>
              </Field>
              <Field label="Warning threshold %">
                <Input
                  name="warning_threshold_percent"
                  type="number"
                  min="0"
                  max="100"
                  defaultValue="80"
                />
              </Field>
              <div className="flex justify-end sm:col-span-2">
                <Button type="submit" size="sm" disabled={!agents.length || mutation === "budget"}>
                  {mutation === "budget" ? (
                    <LoaderCircle className="animate-spin" />
                  ) : (
                    <CircleDollarSign />
                  )}
                  Assign budget
                </Button>
              </div>
            </form>
            <div className="grid gap-2 border-t pt-3">
              {agents.flatMap((agent) => budgets[agent.id] ?? []).length ? (
                agents.map((agent) =>
                  (budgets[agent.id] ?? []).map((budget) => (
                    <div
                      key={budget.id}
                      className="flex items-center justify-between gap-3 rounded-lg border bg-background p-2.5 text-xs"
                    >
                      <p className="font-medium">{agent.name}</p>
                      <Badge variant="outline">
                        {(budget.amount_minor_units / 100).toFixed(2)} {budget.currency} ·{" "}
                        {budget.enforcement_mode.replace("_", " ")}
                      </Badge>
                    </div>
                  ))
                )
              ) : (
                <EmptyState>No budgets assigned yet.</EmptyState>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Wrench className="size-4" /> Agent definitions &amp; versions
          </CardTitle>
          <CardDescription>
            Compose an immutable agent version by pinning a model profile,
            budget, skill versions, MCP server versions, and policy set
            versions. Editing later creates a new version rather than mutating
            this one.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-5">
          <Field label="Agent">
            <select
              className="h-9 w-full max-w-sm rounded-lg border bg-background px-3 text-sm"
              value={selectedAgentId}
              onChange={(event) => setSelectedAgentId(event.target.value)}
              disabled={!agents.length}
            >
              <option value="">
                {agents.length ? "Select an agent" : "No agents persisted yet"}
              </option>
              {agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name}
                </option>
              ))}
            </select>
          </Field>

          {selectedAgentId ? (
            <form className="grid gap-4" onSubmit={createAgentVersion}>
              <input type="hidden" name="agent_id" value={selectedAgentId} />
              <Field label="Instructions">
                <Textarea
                  name="instructions"
                  className="min-h-20"
                  placeholder="Use only the attached skill and MCP tools. Preserve audit evidence."
                />
              </Field>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Model profile">
                  <select
                    name="model_profile_id"
                    className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                    defaultValue=""
                  >
                    <option value="">No model profile</option>
                    {models.map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Default budget">
                  <select
                    name="default_budget_id"
                    className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                    defaultValue=""
                  >
                    <option value="">No default budget</option>
                    {selectedAgentBudgets.map((budget) => (
                      <option key={budget.id} value={budget.id}>
                        {(budget.amount_minor_units / 100).toFixed(2)} {budget.currency} ·{" "}
                        {budget.enforcement_mode.replace("_", " ")}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>

              <div className="grid gap-3 sm:grid-cols-3">
                <div className="grid gap-2 rounded-xl border p-3">
                  <p className="text-xs font-medium text-muted-foreground">
                    Skill versions
                  </p>
                  {allSkillVersions.length ? (
                    allSkillVersions.map(({ skill, version }) => (
                      <label key={version.id} className="flex items-center gap-2 text-xs">
                        <input
                          type="checkbox"
                          checked={skillAttachmentIds.includes(version.id)}
                          onChange={() =>
                            toggleId(skillAttachmentIds, version.id, setSkillAttachmentIds)
                          }
                        />
                        {skill.name} · v{version.version_number}
                      </label>
                    ))
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      No skill versions pinned yet.
                    </p>
                  )}
                </div>
                <div className="grid gap-2 rounded-xl border p-3">
                  <p className="text-xs font-medium text-muted-foreground">
                    MCP server versions
                  </p>
                  {allMcpVersions.length ? (
                    allMcpVersions.map(({ server, version }) => (
                      <label key={version.id} className="flex items-center gap-2 text-xs">
                        <input
                          type="checkbox"
                          checked={mcpAttachmentIds.includes(version.id)}
                          onChange={() =>
                            toggleId(mcpAttachmentIds, version.id, setMcpAttachmentIds)
                          }
                        />
                        {server.name} · v{version.version_number}
                      </label>
                    ))
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      No MCP server versions pinned yet.
                    </p>
                  )}
                </div>
                <div className="grid gap-2 rounded-xl border p-3">
                  <p className="text-xs font-medium text-muted-foreground">
                    Policy set versions
                  </p>
                  {allPolicyVersions.length ? (
                    allPolicyVersions.map(({ set, version }) => (
                      <label key={version.id} className="flex items-center gap-2 text-xs">
                        <input
                          type="checkbox"
                          checked={policyAttachmentIds.includes(version.id)}
                          onChange={() =>
                            toggleId(policyAttachmentIds, version.id, setPolicyAttachmentIds)
                          }
                        />
                        {set.name} · v{version.version_number}
                      </label>
                    ))
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      No policy set versions pinned yet.
                    </p>
                  )}
                </div>
              </div>

              <Button
                type="submit"
                className="justify-self-end"
                disabled={mutation === "agent-version"}
              >
                {mutation === "agent-version" ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <Wrench />
                )}
                Pin new agent version
              </Button>
            </form>
          ) : null}

          <div className="grid gap-2 border-t pt-4">
            <p className="text-xs font-medium text-muted-foreground">
              PERSISTED VERSIONS FOR THIS AGENT
            </p>
            {selectedAgentVersions.length ? (
              selectedAgentVersions.map((version) => (
                <div key={version.id} className="rounded-xl border bg-background p-3 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <p className="font-medium">Version {version.version_number}</p>
                    <Badge variant="outline">{displayDate(version.created_at)}</Badge>
                  </div>
                  {version.instructions ? (
                    <p className="mt-1 text-xs text-muted-foreground">
                      {version.instructions}
                    </p>
                  ) : null}
                  <div className="mt-2 flex flex-wrap gap-1.5 text-xs">
                    <Badge variant="outline">{modelProfileLabel(version.model_profile_id)}</Badge>
                    <Badge variant="outline">
                      {budgetLabel(version.agent_id, version.default_budget_id)}
                    </Badge>
                    {version.skill_attachments.map((attachment) => (
                      <Badge key={attachment.version_id} variant="secondary">
                        {skillVersionLabel(attachment.version_id)}
                      </Badge>
                    ))}
                    {version.mcp_server_attachments.map((attachment) => (
                      <Badge key={attachment.version_id} variant="secondary">
                        {mcpVersionLabel(attachment.version_id)}
                      </Badge>
                    ))}
                    {version.policy_set_version_ids.map((policyVersionId) => (
                      <Badge key={policyVersionId} variant="secondary">
                        {policyVersionLabel(policyVersionId)}
                      </Badge>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <EmptyState>
                {selectedAgentId
                  ? "This agent has no pinned versions yet."
                  : "Select an agent to inspect its persisted versions."}
              </EmptyState>
            )}
          </div>
        </CardContent>
      </Card>
    </section>
  )
}
