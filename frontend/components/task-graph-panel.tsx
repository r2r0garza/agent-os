"use client"

import {
  CircleDollarSign,
  Lock,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
  Wrench,
} from "lucide-react"

import {
  Agent,
  AssignmentCandidate,
  AuditEvent,
  CostLedgerEntry,
  Run,
  Task,
  TaskDependency,
} from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"

type BadgeVariant = "default" | "secondary" | "destructive" | "outline"

const TASK_STATUS_META: Record<string, { label: string; variant: BadgeVariant }> = {
  pending: { label: "Pending", variant: "secondary" },
  ready: { label: "Ready", variant: "outline" },
  running: { label: "Running", variant: "default" },
  blocked: { label: "Blocked", variant: "destructive" },
  completed: { label: "Completed", variant: "default" },
  failed: { label: "Failed", variant: "destructive" },
  cancelled: { label: "Cancelled", variant: "secondary" },
}

const RUN_STATUS_META: Record<string, { label: string; variant: BadgeVariant }> = {
  queued: { label: "Queued", variant: "secondary" },
  running: { label: "Running", variant: "default" },
  waiting_approval: { label: "Waiting on approval", variant: "outline" },
  completed: { label: "Completed", variant: "default" },
  failed: { label: "Failed", variant: "destructive" },
  cancelled: { label: "Cancelled", variant: "secondary" },
}

const PROMOTION_EVENT_TYPES = new Set([
  "workspace.promotion_conflict",
  "workspace.promotion_denied",
  "workspace.promoted",
])

const CONFLICT_EVENT_TYPES = new Set([
  "workspace.promotion_conflict",
  "workspace.promotion_denied",
])

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

/** Longest-path depth from each task's dependencies; used to lay out the graph in execution-order columns. */
function computeDepths(tasks: Task[], dependencies: TaskDependency[]): Map<string, number> {
  const dependsOn = new Map<string, string[]>()
  for (const task of tasks) dependsOn.set(task.id, [])
  for (const edge of dependencies) {
    if (!dependsOn.has(edge.task_id)) dependsOn.set(edge.task_id, [])
    dependsOn.get(edge.task_id)!.push(edge.depends_on_task_id)
  }

  const depth = new Map<string, number>()
  const visiting = new Set<string>()

  function resolve(taskId: string): number {
    const cached = depth.get(taskId)
    if (cached !== undefined) return cached
    if (visiting.has(taskId)) return 0
    visiting.add(taskId)
    const parents = dependsOn.get(taskId) ?? []
    const value = parents.length
      ? 1 + Math.max(...parents.map((parentId) => resolve(parentId)))
      : 0
    visiting.delete(taskId)
    depth.set(taskId, value)
    return value
  }

  for (const task of tasks) resolve(task.id)
  return depth
}

function resolveAgentLabel(
  agentVersionId: string | null | undefined,
  candidates: AssignmentCandidate[],
  agents: Agent[]
): string | null {
  if (!agentVersionId) return null
  const candidate = candidates.find((entry) => entry.agent_version_id === agentVersionId)
  if (!candidate) return null
  const agent = agents.find((item) => item.id === candidate.agent_id)
  return `${agent?.name ?? "Unknown agent"} · v${candidate.agent_version_number}`
}

function blockedReason(task: Task, dependencyTitles: string[]): string | null {
  if (task.status === "blocked") {
    if (task.lease_owner) {
      return `Waiting on a workspace resource lease held by ${task.lease_owner}${
        task.lease_expires_at ? ` until ${displayDate(task.lease_expires_at)}` : ""
      }.`
    }
    if (dependencyTitles.length) {
      return `Waiting on: ${dependencyTitles.join(", ")}.`
    }
    if (task.assignment_status === "no_eligible_agent") {
      return "No agent version satisfies this task's required capabilities."
    }
    if (task.assignment_status === "blocked") {
      return "Blocked by policy or budget evaluation. See assignment rationale."
    }
    return "Blocked pending scheduler re-evaluation."
  }
  if (task.status === "pending" && dependencyTitles.length) {
    return `Queued behind: ${dependencyTitles.join(", ")}.`
  }
  if (task.status === "ready") {
    return "Eligible to run; awaiting a worker claim."
  }
  return null
}

interface TaskGraphPanelProps {
  loading: boolean
  error: string
  onRetry: () => void
  tasks: Task[]
  dependencies: TaskDependency[]
  runs: Run[]
  ledger: CostLedgerEntry[]
  events: AuditEvent[]
  agents: Agent[]
}

export function TaskGraphPanel({
  loading,
  error,
  onRetry,
  tasks,
  dependencies,
  runs,
  ledger,
  events,
  agents,
}: TaskGraphPanelProps) {
  if (loading) {
    return (
      <div className="flex items-center gap-3 rounded-xl border border-dashed p-6 text-sm text-muted-foreground">
        <LoaderCircle className="size-4 animate-spin" />
        Loading the task graph…
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-start justify-between gap-4 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
        <span>{error}</span>
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RefreshCw className="size-3.5" /> Retry
        </Button>
      </div>
    )
  }

  if (tasks.length === 0) {
    return (
      <EmptyState>
        The goal is persisted. No task has been decomposed for it yet.
      </EmptyState>
    )
  }

  const taskById = new Map(tasks.map((task) => [task.id, task]))
  const dependsOn = new Map<string, string[]>()
  for (const edge of dependencies) {
    if (!dependsOn.has(edge.task_id)) dependsOn.set(edge.task_id, [])
    dependsOn.get(edge.task_id)!.push(edge.depends_on_task_id)
  }
  const depths = computeDepths(tasks, dependencies)
  const columnCount = Math.max(0, ...Array.from(depths.values())) + 1
  const columns: Task[][] = Array.from({ length: columnCount }, () => [])
  for (const task of tasks) columns[depths.get(task.id) ?? 0].push(task)

  const runsByTask = new Map<string, Run[]>()
  for (const run of runs) {
    if (!runsByTask.has(run.task_id)) runsByTask.set(run.task_id, [])
    runsByTask.get(run.task_id)!.push(run)
  }
  for (const taskRuns of runsByTask.values()) {
    taskRuns.sort((a, b) => a.attempt_number - b.attempt_number)
  }

  const ledgerByRun = new Map<string, CostLedgerEntry[]>()
  for (const entry of ledger) {
    if (!entry.run_id) continue
    if (!ledgerByRun.has(entry.run_id)) ledgerByRun.set(entry.run_id, [])
    ledgerByRun.get(entry.run_id)!.push(entry)
  }

  const toolEventsByRun = new Map<string, number>()
  const interruptedRuns = new Set<string>()
  for (const event of events) {
    if (event.event_type === "tool.invoked" && event.run_id) {
      toolEventsByRun.set(event.run_id, (toolEventsByRun.get(event.run_id) ?? 0) + 1)
    }
    if (event.event_type === "run.interrupted" && event.run_id) {
      interruptedRuns.add(event.run_id)
    }
  }

  const conflictEventsByTask = new Map<string, AuditEvent[]>()
  const promotionEvents: AuditEvent[] = []
  for (const event of events) {
    if (!PROMOTION_EVENT_TYPES.has(event.event_type)) continue
    promotionEvents.push(event)
    if (event.task_id && CONFLICT_EVENT_TYPES.has(event.event_type)) {
      if (!conflictEventsByTask.has(event.task_id)) conflictEventsByTask.set(event.task_id, [])
      conflictEventsByTask.get(event.task_id)!.push(event)
    }
  }
  promotionEvents.sort((a, b) => b.sequence_number - a.sequence_number)

  return (
    <div className="grid gap-6">
      <div className="grid gap-4 overflow-x-auto pb-2">
        <div
          className="grid gap-4"
          style={{
            gridTemplateColumns: `repeat(${columnCount}, minmax(240px, 1fr))`,
          }}
        >
          {columns.map((columnTasks, columnIndex) => (
            <div key={columnIndex} className="grid content-start gap-3">
              <p className="text-xs font-medium text-muted-foreground">
                {columnIndex === 0 ? "Starting tasks" : `Depends on stage ${columnIndex}`}
              </p>
              {columnTasks.map((task) => {
                const statusMeta = TASK_STATUS_META[task.status] ?? {
                  label: task.status,
                  variant: "secondary" as const,
                }
                const dependencyTitles = (dependsOn.get(task.id) ?? [])
                  .map((depId) => taskById.get(depId)?.title)
                  .filter((title): title is string => Boolean(title))
                const reason = blockedReason(task, dependencyTitles)
                const agentLabel = resolveAgentLabel(
                  task.assigned_agent_version_id,
                  task.assignment_candidates,
                  agents
                )
                const taskRuns = runsByTask.get(task.id) ?? []
                const taskConflicts = conflictEventsByTask.get(task.id) ?? []

                return (
                  <div key={task.id} className="rounded-xl border bg-background p-3">
                    <div className="flex items-start justify-between gap-2">
                      <p className="font-medium">{task.title}</p>
                      <Badge variant={statusMeta.variant}>{statusMeta.label}</Badge>
                    </div>

                    {dependencyTitles.length ? (
                      <p className="mt-2 text-xs text-muted-foreground">
                        Depends on: {dependencyTitles.join(", ")}
                      </p>
                    ) : null}

                    <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                      <Badge variant="outline" className="gap-1">
                        <Wrench className="size-3" />
                        {agentLabel ?? task.assignment_status.replaceAll("_", " ")}
                      </Badge>
                      {task.lease_owner ? (
                        <Badge variant="outline" className="gap-1">
                          <Lock className="size-3" />
                          {task.lease_owner}
                        </Badge>
                      ) : null}
                      {taskConflicts.length ? (
                        <Badge variant="destructive" className="gap-1">
                          <ShieldAlert className="size-3" />
                          {taskConflicts.length} conflict
                          {taskConflicts.length === 1 ? "" : "s"}
                        </Badge>
                      ) : null}
                    </div>

                    {reason ? (
                      <p className="mt-2 text-xs text-muted-foreground">{reason}</p>
                    ) : null}

                    {taskRuns.length ? (
                      <div className="mt-3 grid gap-2 border-t pt-2">
                        {taskRuns.map((run) => {
                          const runMeta = RUN_STATUS_META[run.status] ?? {
                            label: run.status,
                            variant: "secondary" as const,
                          }
                          const cost = (ledgerByRun.get(run.id) ?? []).reduce(
                            (total, entry) => total + (entry.actual_amount_minor_units ?? 0),
                            0
                          )
                          const toolCount = toolEventsByRun.get(run.id) ?? 0
                          const isRetry = run.attempt_number > 1
                          const wasInterrupted = interruptedRuns.has(run.id)
                          return (
                            <div key={run.id} className="text-xs">
                              <div className="flex items-center justify-between gap-2">
                                <span className="font-medium">
                                  Attempt {run.attempt_number}
                                  {isRetry ? " (retry)" : ""}
                                </span>
                                <Badge variant={runMeta.variant}>{runMeta.label}</Badge>
                              </div>
                              <p className="mt-0.5 text-muted-foreground">
                                {wasInterrupted
                                  ? "Interrupted by worker restart, then reconciled. "
                                  : ""}
                                {toolCount} tool call{toolCount === 1 ? "" : "s"} ·{" "}
                                <CircleDollarSign className="inline size-3" /> {cost} minor ·{" "}
                                {displayDate(run.started_at)}
                              </p>
                            </div>
                          )
                        })}
                      </div>
                    ) : null}
                  </div>
                )
              })}
            </div>
          ))}
        </div>
      </div>

      <div>
        <p className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <ShieldAlert className="size-3.5" /> WORKSPACE PROMOTION HISTORY
        </p>
        {promotionEvents.length ? (
          <div className="grid gap-2">
            {promotionEvents.slice(0, 8).map((event) => {
              const resources = (event.payload.resources ?? event.payload.expected_revisions) as
                | Record<string, unknown>
                | undefined
              const variant: BadgeVariant =
                event.event_type === "workspace.promoted" ? "default" : "destructive"
              const label =
                event.event_type === "workspace.promoted"
                  ? "Promoted"
                  : event.event_type === "workspace.promotion_denied"
                    ? "Denied (stale lease)"
                    : "Conflict (revision changed)"
              return (
                <div
                  key={event.id}
                  className="flex items-center justify-between gap-3 rounded-xl border p-3 text-xs"
                >
                  <div className="min-w-0">
                    <p className="font-medium">
                      {taskById.get(event.task_id ?? "")?.title ?? "Unknown task"}
                    </p>
                    <p className="mt-0.5 text-muted-foreground">
                      {resources ? Object.keys(resources).join(", ") : "no resource keys"} ·{" "}
                      {displayDate(event.occurred_at)}
                    </p>
                  </div>
                  <Badge variant={variant}>{label}</Badge>
                </div>
              )
            })}
          </div>
        ) : (
          <EmptyState>
            No workspace promotions recorded yet for this goal&apos;s runs.
          </EmptyState>
        )}
      </div>
    </div>
  )
}
