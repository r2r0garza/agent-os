"use client"

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react"
import {
  Activity,
  Bot,
  Box,
  CheckCircle2,
  CircleDollarSign,
  CloudCog,
  Database,
  History,
  KeyRound,
  LoaderCircle,
  Play,
  RefreshCw,
  RotateCcw,
  ServerCog,
  ShieldCheck,
  Sparkles,
  Workflow,
  Wrench,
} from "lucide-react"

import {
  Agent,
  Artifact,
  AuditEvent,
  CostLedgerEntry,
  Goal,
  McpServer,
  ModelProfile,
  Project,
  Run,
  Skill,
  Task,
  TaskDependency,
  api,
  jsonBody,
} from "@/lib/api"
import { AccessWorkspace } from "@/components/access-workspace"
import { AdminConcurrentHealth } from "@/components/admin-concurrent-health"
import { ArtifactWorkspace } from "@/components/artifact-workspace"
import { ConcurrentWorkspacePanel } from "@/components/concurrent-workspace-panel"
import { GoalLifecyclePanel } from "@/components/goal-lifecycle-panel"
import { GovernanceOperations } from "@/components/governance-operations"
import {
  GovernanceLookups,
  GovernanceWorkspace,
} from "@/components/governance-workspace"
import { ObservabilityWorkspace } from "@/components/observability-workspace"
import { TaskGraphPanel } from "@/components/task-graph-panel"
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

const PROJECT_STORAGE_KEY = "agentic-os.project"
const GOAL_STORAGE_KEY = "agentic-os.goal"

interface Inventory {
  models: ModelProfile[]
  projects: Project[]
  agents: Agent[]
  skills: Skill[]
  servers: McpServer[]
}

const emptyInventory: Inventory = {
  models: [],
  projects: [],
  agents: [],
  skills: [],
  servers: [],
}

function displayDate(value: string | null) {
  if (!value) return "Not yet"
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed bg-muted/20 px-4 py-7 text-center text-sm text-muted-foreground">
      {children}
    </div>
  )
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

function Metric({
  label,
  value,
  icon,
}: {
  label: string
  value: string | number
  icon: React.ReactNode
}) {
  return (
    <div className="rounded-xl border bg-background p-3">
      <div className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
        {icon}
        {label}
      </div>
      <p className="text-xl font-semibold tracking-tight">{value}</p>
    </div>
  )
}

export function OperatorWorkspace() {
  const [inventory, setInventory] = useState<Inventory>(emptyInventory)
  const [selectedProjectId, setSelectedProjectId] = useState("")
  const [selectedGoalId, setSelectedGoalId] = useState("")
  const [goals, setGoals] = useState<Goal[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [dependencies, setDependencies] = useState<TaskDependency[]>([])
  const [runs, setRuns] = useState<Run[]>([])
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [ledger, setLedger] = useState<CostLedgerEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [goalLoading, setGoalLoading] = useState(false)
  const [goalStateError, setGoalStateError] = useState("")
  const [mutation, setMutation] = useState("")
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [governanceLookups, setGovernanceLookups] = useState<GovernanceLookups>(
    {
      skillVersionName: {},
      mcpVersionName: {},
      modelProfileVersionName: {},
      policySetVersionName: {},
    }
  )

  const loadInventory = useCallback(async () => {
    const [models, projects, agents, skills, servers] = await Promise.all([
      api<ModelProfile[]>("/model-profiles"),
      api<Project[]>("/projects"),
      api<Agent[]>("/agents"),
      api<Skill[]>("/skills"),
      api<McpServer[]>("/mcp-servers"),
    ])
    setInventory({ models, projects, agents, skills, servers })
    setSelectedProjectId((current) => {
      if (current && projects.some((project) => project.id === current))
        return current
      const remembered = window.localStorage.getItem(PROJECT_STORAGE_KEY)
      if (remembered && projects.some((project) => project.id === remembered)) {
        return remembered
      }
      return projects.at(-1)?.id ?? ""
    })
  }, [])

  const loadProjectState = useCallback(async (projectId: string) => {
    if (!projectId) {
      setGoals([])
      setSelectedGoalId("")
      setTasks([])
      setDependencies([])
      setRuns([])
      setArtifacts([])
      setEvents([])
      setLedger([])
      return
    }

    const [projectGoals, projectArtifacts, projectEvents] = await Promise.all([
      api<Goal[]>(`/projects/${projectId}/goals`),
      api<Artifact[]>(`/projects/${projectId}/artifacts`),
      api<AuditEvent[]>(`/audit-events?project_id=${projectId}&limit=250`),
    ])
    setGoals(projectGoals)
    setArtifacts(projectArtifacts)
    setEvents(projectEvents)
    setSelectedGoalId((current) => {
      if (current && projectGoals.some((goal) => goal.id === current))
        return current
      const remembered = window.localStorage.getItem(GOAL_STORAGE_KEY)
      if (remembered && projectGoals.some((goal) => goal.id === remembered)) {
        return remembered
      }
      return projectGoals.at(-1)?.id ?? ""
    })
  }, [])

  const loadGoalState = useCallback(async (goalId: string) => {
    if (!goalId) {
      setTasks([])
      setDependencies([])
      setRuns([])
      setLedger([])
      return
    }

    const graph = await api<{ tasks: Task[]; dependencies: TaskDependency[] }>(
      `/goals/${goalId}/task-graph`
    )
    const taskRuns = (
      await Promise.all(
        graph.tasks.map((task) => api<Run[]>(`/tasks/${task.id}/runs`))
      )
    ).flat()
    const runLedger = (
      await Promise.all(
        taskRuns.map((run) =>
          api<CostLedgerEntry[]>(`/cost-ledger-entries?run_id=${run.id}`)
        )
      )
    ).flat()
    setTasks(graph.tasks)
    setDependencies(graph.dependencies)
    setRuns(taskRuns)
    setLedger(runLedger)
  }, [])

  const refreshAll = useCallback(
    async (showSpinner = true) => {
      if (showSpinner) setRefreshing(true)
      setError("")
      try {
        await loadInventory()
        if (selectedProjectId) await loadProjectState(selectedProjectId)
        if (selectedGoalId) {
          setGoalStateError("")
          await loadGoalState(selectedGoalId)
        }
      } catch (reason) {
        setError(
          reason instanceof Error ? reason.message : "Unable to load workspace"
        )
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [
      loadGoalState,
      loadInventory,
      loadProjectState,
      selectedGoalId,
      selectedProjectId,
    ]
  )

  const retryGoalState = useCallback(() => {
    if (!selectedGoalId) return
    setGoalStateError("")
    setGoalLoading(true)
    void loadGoalState(selectedGoalId)
      .catch((reason: unknown) =>
        setGoalStateError(
          reason instanceof Error
            ? reason.message
            : "Unable to load the task graph"
        )
      )
      .finally(() => setGoalLoading(false))
  }, [loadGoalState, selectedGoalId])

  useEffect(() => {
    const timer = window.setTimeout(() => void refreshAll(false), 0)
    return () => window.clearTimeout(timer)
    // Initial inventory load intentionally schedules once; selection effects fetch detail.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!selectedProjectId) return
    window.localStorage.setItem(PROJECT_STORAGE_KEY, selectedProjectId)
    const timer = window.setTimeout(() => {
      void loadProjectState(selectedProjectId).catch((reason: unknown) =>
        setError(
          reason instanceof Error ? reason.message : "Unable to load project"
        )
      )
    }, 0)
    return () => window.clearTimeout(timer)
  }, [loadProjectState, selectedProjectId])

  useEffect(() => {
    if (!selectedGoalId) return
    window.localStorage.setItem(GOAL_STORAGE_KEY, selectedGoalId)
    const timer = window.setTimeout(() => {
      setGoalStateError("")
      setGoalLoading(true)
      void loadGoalState(selectedGoalId)
        .catch((reason: unknown) =>
          setGoalStateError(
            reason instanceof Error
              ? reason.message
              : "Unable to load the task graph"
          )
        )
        .finally(() => setGoalLoading(false))
    }, 0)
    return () => window.clearTimeout(timer)
  }, [loadGoalState, selectedGoalId])

  useEffect(() => {
    if (!selectedProjectId) return
    const timer = window.setInterval(() => {
      void Promise.all([
        loadProjectState(selectedProjectId),
        selectedGoalId ? loadGoalState(selectedGoalId) : Promise.resolve(),
      ]).catch(() => undefined)
    }, 5_000)
    return () => window.clearInterval(timer)
  }, [loadGoalState, loadProjectState, selectedGoalId, selectedProjectId])

  async function mutate(name: string, work: () => Promise<void>) {
    setMutation(name)
    setError("")
    setNotice("")
    try {
      await work()
      await loadInventory()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Request failed")
    } finally {
      setMutation("")
    }
  }

  async function createModel(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    await mutate("model", async () => {
      await api<ModelProfile>(
        "/model-profiles",
        jsonBody({
          name: form.get("name"),
          base_url: form.get("base_url"),
          model_identifier: form.get("model_identifier"),
          api_key: form.get("api_key"),
          capability_metadata: { tool_calling: true },
          pricing_metadata: {},
        })
      )
      setNotice(
        "Model profile saved. Its API key is encrypted and never returned."
      )
      formElement.reset()
    })
  }

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    await mutate("project", async () => {
      const project = await api<Project>(
        "/projects",
        jsonBody({ name: form.get("name") })
      )
      setSelectedProjectId(project.id)
      setNotice("Project created and selected for this workflow.")
      formElement.reset()
    })
  }

  async function createGoal(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selectedProjectId) return
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    await mutate("goal", async () => {
      const goal = await api<Goal>(
        `/projects/${selectedProjectId}/goals`,
        jsonBody({
          title: form.get("title"),
          description: form.get("description"),
        })
      )
      await loadProjectState(selectedProjectId)
      setSelectedGoalId(goal.id)
      setNotice(
        "Goal submitted and persisted. Waiting for task decomposition or worker activity."
      )
      formElement.reset()
    })
  }

  async function decomposeGoal() {
    if (!selectedGoalId) return
    await mutate("decompose", async () => {
      await api(
        `/goals/${selectedGoalId}/task-graph/decompose`,
        jsonBody({ workflow: "research_brief" })
      )
      await loadGoalState(selectedGoalId)
      setNotice("Goal decomposed into an inspectable task graph.")
    })
  }

  async function provisionAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    const modelId = String(form.get("model_profile_id") ?? "")
    if (!modelId) {
      setError(
        "Create and select a model profile before provisioning an agent."
      )
      return
    }

    await mutate("agent", async () => {
      const skill = await api<Skill>(
        "/skills",
        jsonBody({ name: form.get("skill_name"), visibility: "private" })
      )
      const skillVersion = await api<{ id: string }>(
        `/skills/${skill.id}/versions`,
        jsonBody({
          content_ref: form.get("skill_ref"),
          resource_metadata: { purpose: "foundation workflow" },
        })
      )
      const server = await api<McpServer>(
        "/mcp-servers",
        jsonBody({
          name: form.get("mcp_name"),
          project_id: selectedProjectId || null,
        })
      )
      const mcpVersion = await api<{ id: string }>(
        `/mcp-servers/${server.id}/versions`,
        jsonBody({
          connection_config: {
            transport: "test",
            tools: [
              {
                name: "echo",
                description:
                  "Echo a governed task payload for the foundation workflow.",
              },
            ],
          },
        })
      )
      const agent = await api<Agent>(
        "/agents",
        jsonBody({ name: form.get("agent_name"), visibility: "private" })
      )
      const budget = await api<{ id: string }>(
        `/agents/${agent.id}/budgets`,
        jsonBody({
          currency: "USD",
          amount_minor_units: Number(form.get("budget_minor_units")),
          enforcement_mode: form.get("enforcement_mode"),
          warning_threshold_percent: 80,
        })
      )
      await api(
        `/agents/${agent.id}/versions`,
        jsonBody({
          instructions: form.get("instructions"),
          model_profile_id: modelId,
          default_budget_id: budget.id,
          capability_manifest: {
            skill_version_id: skillVersion.id,
            mcp_server_version_id: mcpVersion.id,
            enabled_tools: ["echo"],
          },
        })
      )
      setNotice(
        "Versioned skill, MCP server, budget, and governed agent are ready."
      )
      formElement.reset()
    })
  }

  const selectedProject = inventory.projects.find(
    (project) => project.id === selectedProjectId
  )
  const selectedGoal = goals.find((goal) => goal.id === selectedGoalId)
  const goalEvents = events.filter(
    (event) => !selectedGoalId || event.goal_id === selectedGoalId
  )
  const visibleEvents = goalEvents.slice(-12).reverse()
  const actualCost = ledger.reduce(
    (total, entry) => total + (entry.actual_amount_minor_units ?? 0),
    0
  )
  const currency = ledger[0]?.currency ?? "USD"
  const costLabel = useMemo(
    () =>
      new Intl.NumberFormat(undefined, {
        style: "currency",
        currency,
      }).format(actualCost / 100),
    [actualCost, currency]
  )
  const activeRun = runs.find((run) => run.status === "running")
  const recoverable = runs.find((run) =>
    ["failed", "cancelled"].includes(run.status)
  )

  if (loading) {
    return (
      <main className="grid min-h-svh place-items-center bg-muted/20">
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <LoaderCircle className="size-5 animate-spin" />
          Reconnecting to persisted Agentic OS state…
        </div>
      </main>
    )
  }

  return (
    <main className="min-h-svh bg-[radial-gradient(circle_at_top_left,var(--color-muted),transparent_38%)]">
      <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        <header className="mb-6 flex flex-col gap-4 rounded-2xl border bg-background/90 p-5 shadow-sm backdrop-blur sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-3">
            <div className="grid size-10 place-items-center rounded-xl bg-foreground text-background">
              <Sparkles className="size-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-xl font-semibold tracking-tight">
                  Agentic OS
                </h1>
                <Badge variant="outline">Foundation console</Badge>
              </div>
              <p className="text-sm text-muted-foreground">
                Configure governed work, then inspect its durable execution
                trail.
              </p>
            </div>
          </div>
          <Button
            variant="outline"
            onClick={() => void refreshAll()}
            disabled={refreshing}
          >
            <RefreshCw className={refreshing ? "animate-spin" : ""} />
            Refresh state
          </Button>
        </header>

        {error ? (
          <div className="mb-5 flex items-start justify-between gap-4 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
            <span>{error}</span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void refreshAll()}
            >
              Retry
            </Button>
          </div>
        ) : null}
        {notice ? (
          <div className="mb-5 flex items-center gap-2 rounded-xl border bg-background p-4 text-sm">
            <CheckCircle2 className="size-4 text-emerald-600" /> {notice}
          </div>
        ) : null}

        <section className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Metric
            label="Model profiles"
            value={inventory.models.length}
            icon={<CloudCog className="size-4" />}
          />
          <Metric
            label="Projects"
            value={inventory.projects.length}
            icon={<Database className="size-4" />}
          />
          <Metric
            label="Agents"
            value={inventory.agents.length}
            icon={<Bot className="size-4" />}
          />
          <Metric
            label="Run attempts"
            value={runs.length}
            icon={<Activity className="size-4" />}
          />
          <Metric
            label="Actual cost"
            value={costLabel}
            icon={<CircleDollarSign className="size-4" />}
          />
        </section>

        <Card className="mb-6">
          <CardHeader>
            <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
              <Activity className="size-4" /> TASK GRAPH & MULTI-AGENT PROGRESS
            </div>
            <CardTitle>{selectedGoal?.title ?? "Run progress"}</CardTitle>
            <CardDescription>
              {selectedProject
                ? `${selectedProject.name} · polled every 5 seconds`
                : "Select a project to inspect persisted work."}
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div className="max-w-md flex-1">
                <Field label="Goal">
                  <select
                    className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                    value={selectedGoalId}
                    onChange={(event) => setSelectedGoalId(event.target.value)}
                    disabled={!goals.length}
                  >
                    <option value="">
                      {goals.length
                        ? "Select a goal"
                        : "No goals in this project"}
                    </option>
                    {goals.map((goal) => (
                      <option key={goal.id} value={goal.id}>
                        {goal.title}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              {selectedGoalId && !goalLoading && tasks.length === 0 ? (
                <Button
                  variant="outline"
                  onClick={() => void decomposeGoal()}
                  disabled={mutation === "decompose"}
                >
                  {mutation === "decompose" ? (
                    <LoaderCircle className="animate-spin" />
                  ) : (
                    <Workflow />
                  )}
                  Decompose into task graph
                </Button>
              ) : null}
            </div>

            {activeRun ? (
              <div className="flex items-center gap-3 rounded-xl border border-blue-500/20 bg-blue-500/5 p-3 text-sm">
                <LoaderCircle className="size-4 animate-spin text-blue-600" />
                Run attempt {activeRun.attempt_number} is active. Progress will
                reconnect automatically.
              </div>
            ) : null}
            {recoverable ? (
              <div className="flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
                <RotateCcw className="mt-0.5 size-4 text-amber-600" />
                <span>
                  Execution stopped in a recoverable state. Restart or resume
                  the worker, then refresh; persisted history remains visible.
                </span>
              </div>
            ) : null}

            {!selectedGoalId ? (
              <EmptyState>
                Submit or select a goal to inspect its task graph and run
                attempts.
              </EmptyState>
            ) : (
              <TaskGraphPanel
                loading={goalLoading && tasks.length === 0}
                error={goalStateError}
                onRetry={retryGoalState}
                tasks={tasks}
                dependencies={dependencies}
                runs={runs}
                ledger={ledger}
                events={goalEvents}
                agents={inventory.agents}
                governanceLookups={governanceLookups}
              />
            )}
          </CardContent>
        </Card>

        <ConcurrentWorkspacePanel
          projectId={selectedProjectId}
          goals={goals}
          onRefresh={async () => {
            await loadProjectState(selectedProjectId)
            if (selectedGoalId) await loadGoalState(selectedGoalId)
          }}
        />

        <AdminConcurrentHealth projects={inventory.projects} />

        <GoalLifecyclePanel
          goalId={selectedGoalId}
          goal={selectedGoal}
          onRefresh={async () => {
            await loadProjectState(selectedProjectId)
            if (selectedGoalId) await loadGoalState(selectedGoalId)
          }}
        />

        <ObservabilityWorkspace goalId={selectedGoalId} runs={runs} />

        <GovernanceWorkspace
          agents={inventory.agents}
          models={inventory.models}
          skills={inventory.skills}
          servers={inventory.servers}
          onLookupsChange={setGovernanceLookups}
        />

        <GovernanceOperations
          projectId={selectedProjectId}
          tasks={tasks}
          runs={runs}
          onRefresh={async () => {
            await loadProjectState(selectedProjectId)
            if (selectedGoalId) await loadGoalState(selectedGoalId)
          }}
        />

        <AccessWorkspace
          projectId={selectedProjectId}
          projects={inventory.projects}
          agents={inventory.agents}
          skills={inventory.skills}
          servers={inventory.servers}
          onRefresh={loadInventory}
        />

        <ArtifactWorkspace
          projectId={selectedProjectId}
          goalId={selectedGoalId}
          artifacts={artifacts}
          onRefresh={() => loadProjectState(selectedProjectId)}
        />

        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(360px,0.78fr)]">
          <div className="grid content-start gap-6">
            <Card>
              <CardHeader>
                <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                  <KeyRound className="size-4" /> STEP 1
                </div>
                <CardTitle>Connect a model profile</CardTitle>
                <CardDescription>
                  OpenAI-compatible credentials are encrypted by the API and are
                  not returned to this page.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <form
                  className="grid gap-4 sm:grid-cols-2"
                  onSubmit={createModel}
                >
                  <Field label="Profile name">
                    <Input name="name" placeholder="Local model" required />
                  </Field>
                  <Field label="Model identifier">
                    <Input
                      name="model_identifier"
                      placeholder="gpt-5-mini"
                      required
                    />
                  </Field>
                  <Field label="Base URL">
                    <Input
                      name="base_url"
                      type="url"
                      defaultValue="https://api.openai.com/v1"
                      required
                    />
                  </Field>
                  <Field label="API key">
                    <Input
                      name="api_key"
                      type="password"
                      placeholder="Stored encrypted"
                      required
                    />
                  </Field>
                  <div className="flex items-center justify-between gap-3 sm:col-span-2">
                    <p className="text-xs text-muted-foreground">
                      {inventory.models.length
                        ? `${inventory.models.length} persisted profile${inventory.models.length === 1 ? "" : "s"} available.`
                        : "No model profile configured yet."}
                    </p>
                    <Button type="submit" disabled={mutation === "model"}>
                      {mutation === "model" ? (
                        <LoaderCircle className="animate-spin" />
                      ) : (
                        <ShieldCheck />
                      )}
                      Save profile
                    </Button>
                  </div>
                </form>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                  <Database className="size-4" /> STEP 2
                </div>
                <CardTitle>Create the durable workspace</CardTitle>
                <CardDescription>
                  Select any persisted project after reload, then submit a goal
                  to it.
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-5">
                <form className="flex items-end gap-3" onSubmit={createProject}>
                  <div className="flex-1">
                    <Field label="New project">
                      <Input
                        name="name"
                        placeholder="Foundation demo"
                        required
                      />
                    </Field>
                  </div>
                  <Button type="submit" disabled={mutation === "project"}>
                    {mutation === "project" ? (
                      <LoaderCircle className="animate-spin" />
                    ) : (
                      <Box />
                    )}
                    Create
                  </Button>
                </form>
                <Field
                  label="Active project"
                  hint="This selection is restored locally, then validated against the API on reload."
                >
                  <select
                    className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                    value={selectedProjectId}
                    onChange={(event) =>
                      setSelectedProjectId(event.target.value)
                    }
                  >
                    <option value="">Select a persisted project</option>
                    {inventory.projects.map((project) => (
                      <option key={project.id} value={project.id}>
                        {project.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <form className="grid gap-3" onSubmit={createGoal}>
                  <Field label="Goal title">
                    <Input
                      name="title"
                      placeholder="Produce a governed foundation result"
                      required
                      disabled={!selectedProjectId}
                    />
                  </Field>
                  <Field label="Goal details">
                    <Textarea
                      name="description"
                      placeholder="Describe the outcome and evidence you expect."
                      disabled={!selectedProjectId}
                    />
                  </Field>
                  <Button
                    type="submit"
                    className="justify-self-end"
                    disabled={!selectedProjectId || mutation === "goal"}
                  >
                    {mutation === "goal" ? (
                      <LoaderCircle className="animate-spin" />
                    ) : (
                      <Play />
                    )}
                    Submit goal
                  </Button>
                </form>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                  <Bot className="size-4" /> STEP 3
                </div>
                <CardTitle>Provision the governed agent</CardTitle>
                <CardDescription>
                  Creates version-pinned skill and MCP definitions, a lifetime
                  budget, and one immutable agent version.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <form
                  className="grid gap-4 sm:grid-cols-2"
                  onSubmit={provisionAgent}
                >
                  <Field label="Agent name">
                    <Input
                      name="agent_name"
                      placeholder="Foundation operator"
                      required
                    />
                  </Field>
                  <Field label="Model profile">
                    <select
                      name="model_profile_id"
                      className="h-8 w-full rounded-lg border bg-background px-2.5 text-sm"
                      required
                      defaultValue=""
                    >
                      <option value="" disabled>
                        Select a profile
                      </option>
                      {inventory.models.map((model) => (
                        <option key={model.id} value={model.id}>
                          {model.name} · {model.model_identifier}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Skill name">
                    <Input
                      name="skill_name"
                      defaultValue="Foundation research"
                      required
                    />
                  </Field>
                  <Field label="Skill content reference">
                    <Input
                      name="skill_ref"
                      defaultValue="skills://foundation-research/v1"
                      required
                    />
                  </Field>
                  <Field label="MCP server">
                    <Input
                      name="mcp_name"
                      defaultValue="Foundation tools"
                      required
                    />
                  </Field>
                  <Field
                    label="Budget (minor USD units)"
                    hint="50000 means $500.00."
                  >
                    <Input
                      name="budget_minor_units"
                      type="number"
                      min="0"
                      defaultValue="50000"
                      required
                    />
                  </Field>
                  <Field label="Enforcement">
                    <select
                      name="enforcement_mode"
                      className="h-8 w-full rounded-lg border bg-background px-2.5 text-sm"
                      defaultValue="hard_stop"
                    >
                      <option value="hard_stop">Hard stop</option>
                      <option value="warning">Warning</option>
                    </select>
                  </Field>
                  <div className="sm:col-span-2">
                    <Field label="Agent instructions">
                      <Textarea
                        name="instructions"
                        defaultValue="Use the attached skill and approved echo tool. Produce an inspectable result and preserve audit evidence."
                        required
                      />
                    </Field>
                  </div>
                  <div className="flex items-center justify-between gap-3 sm:col-span-2">
                    <p className="text-xs text-muted-foreground">
                      {inventory.skills.length} skills ·{" "}
                      {inventory.servers.length} MCP servers ·{" "}
                      {inventory.agents.length} agents persisted
                    </p>
                    <Button
                      type="submit"
                      disabled={
                        !inventory.models.length || mutation === "agent"
                      }
                    >
                      {mutation === "agent" ? (
                        <LoaderCircle className="animate-spin" />
                      ) : (
                        <ServerCog />
                      )}
                      Provision stack
                    </Button>
                  </div>
                </form>
              </CardContent>
            </Card>
          </div>

          <div className="grid content-start gap-6 lg:sticky lg:top-6 lg:self-start">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <CircleDollarSign className="size-4" /> Cost ledger
                </CardTitle>
                <CardDescription>
                  Authoritative reconciled entries for the selected goal&apos;s
                  run attempts.
                </CardDescription>
              </CardHeader>
              <CardContent>
                {ledger.length ? (
                  <div className="grid gap-3">
                    <div className="grid grid-cols-2 gap-3">
                      <Metric
                        label="Entries"
                        value={ledger.length}
                        icon={<History className="size-4" />}
                      />
                      <Metric
                        label="Actual"
                        value={costLabel}
                        icon={<CircleDollarSign className="size-4" />}
                      />
                    </div>
                    {ledger
                      .slice(-5)
                      .reverse()
                      .map((entry) => (
                        <div
                          key={entry.id}
                          className="flex items-center justify-between gap-3 border-t pt-3 text-sm"
                        >
                          <div>
                            <p className="font-medium">
                              {entry.action_type.replaceAll("_", " ")}
                            </p>
                            <p className="text-xs text-muted-foreground">
                              {entry.status} · {displayDate(entry.created_at)}
                            </p>
                          </div>
                          <Badge variant="outline">
                            {entry.is_zero_cost
                              ? "zero cost"
                              : `${entry.actual_amount_minor_units ?? 0} minor`}
                          </Badge>
                        </div>
                      ))}
                  </div>
                ) : (
                  <EmptyState>
                    No cost entries have been recorded for these runs.
                  </EmptyState>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Wrench className="size-4" /> Tool activity & audit
                </CardTitle>
                <CardDescription>
                  Ordered product events expose what ran without exposing
                  private reasoning.
                </CardDescription>
              </CardHeader>
              <CardContent>
                {visibleEvents.length ? (
                  <div className="grid gap-3">
                    {visibleEvents.map((event) => (
                      <div
                        key={event.id}
                        className="grid grid-cols-[auto_1fr] gap-3"
                      >
                        <div className="mt-1 size-2 rounded-full bg-foreground" />
                        <div className="min-w-0 border-b pb-3 last:border-0 last:pb-0">
                          <div className="flex items-center justify-between gap-2">
                            <p className="truncate text-sm font-medium">
                              {event.event_type}
                            </p>
                            <span className="font-mono text-[10px] text-muted-foreground">
                              #{event.sequence_number}
                            </span>
                          </div>
                          <p className="mt-1 text-xs text-muted-foreground">
                            {displayDate(event.occurred_at)}
                          </p>
                          {event.event_type === "tool.invoked" &&
                          typeof event.payload.tool === "string" ? (
                            <Badge className="mt-2" variant="outline">
                              tool: {event.payload.tool}
                            </Badge>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <EmptyState>
                    No audit events are available for this project yet.
                  </EmptyState>
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      </div>
    </main>
  )
}
