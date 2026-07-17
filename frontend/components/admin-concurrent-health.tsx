"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  GitCommitHorizontal,
  LoaderCircle,
  RefreshCw,
  Server,
  ShieldAlert,
  Waypoints,
} from "lucide-react"

import {
  ApiError,
  AuditEvent,
  Goal,
  Project,
  Task,
  TaskDependency,
  WorkspaceConflict,
  WorkspaceLease,
  WorkspacePromotion,
  api,
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

interface WorkerSummary {
  owner: string
  active: number
  stale: number
  fenced: number
  projectCount: number
  resources: string[]
}

interface ProjectConcurrencySummary {
  project: Project
  activeGoals: number
  conflictCount: number
  overlapKeys: string[]
}

function displayDate(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}

function shortId(value: string) {
  return value.slice(0, 8)
}

function Metric({
  label,
  value,
  icon,
}: {
  label: string
  value: number
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

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed bg-muted/20 px-4 py-6 text-center text-sm text-muted-foreground">
      {children}
    </div>
  )
}

export function AdminConcurrentHealth({ projects }: { projects: Project[] }) {
  const [leases, setLeases] = useState<WorkspaceLease[]>([])
  const [conflicts, setConflicts] = useState<WorkspaceConflict[]>([])
  const [promotions, setPromotions] = useState<WorkspacePromotion[]>([])
  const [fenceEvents, setFenceEvents] = useState<AuditEvent[]>([])
  const [projectSummaries, setProjectSummaries] = useState<
    ProjectConcurrencySummary[]
  >([])
  const [loading, setLoading] = useState(true)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState("")
  const projectKey = projects.map((project) => project.id).join(",")

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      // Authorization is deliberately established before requesting any
      // cross-project detail. A regular user never receives or renders it.
      const leaseList = await api<WorkspaceLease[]>(
        "/admin/workspace/leases?limit=500"
      )
      setForbidden(false)

      const [conflictList, promotionList, events, goalsByProject] =
        await Promise.all([
          api<WorkspaceConflict[]>("/admin/workspace/conflicts?limit=200"),
          api<WorkspacePromotion[]>("/admin/workspace/promotions?limit=100"),
          api<AuditEvent[]>("/audit-events?limit=500"),
          Promise.all(
            projects.map(async (project) => ({
              project,
              goals: await api<Goal[]>(`/projects/${project.id}/goals`),
            }))
          ),
        ])

      const activeGoalsByProject = await Promise.all(
        goalsByProject.map(async ({ project, goals }) => {
          const activeGoals = goals.filter((goal) => goal.status === "active")
          const graphs = await Promise.all(
            activeGoals.map(async (goal) => ({
              goal,
              graph: await api<{
                tasks: Task[]
                dependencies: TaskDependency[]
              }>(`/goals/${goal.id}/task-graph`),
            }))
          )
          const keyOwners = new Map<string, Set<string>>()
          for (const { goal, graph } of graphs) {
            for (const task of graph.tasks) {
              for (const intent of task.resource_intent) {
                if (!keyOwners.has(intent.resource_key)) {
                  keyOwners.set(intent.resource_key, new Set())
                }
                keyOwners.get(intent.resource_key)!.add(goal.id)
              }
            }
          }
          return {
            project,
            activeGoals: activeGoals.length,
            conflictCount: conflictList.filter(
              (conflict) => conflict.project_id === project.id
            ).length,
            overlapKeys: [...keyOwners.entries()]
              .filter(([, goalIds]) => goalIds.size > 1)
              .map(([key]) => key)
              .sort(),
          }
        })
      )

      setLeases(leaseList)
      setConflicts(conflictList)
      setPromotions(promotionList)
      setFenceEvents(
        events
          .filter((event) => event.event_type === "workspace.lease_acquired")
          .slice(-8)
          .reverse()
      )
      setProjectSummaries(activeGoalsByProject)
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 403) {
        setForbidden(true)
        setLeases([])
        setConflicts([])
        setPromotions([])
        setFenceEvents([])
        setProjectSummaries([])
      } else {
        setError(
          caught instanceof Error
            ? caught.message
            : "Unable to load concurrent workload health"
        )
      }
    } finally {
      setLoading(false)
    }
    // projectKey prevents refetching when the inventory array identity changes
    // but its installation-wide project set does not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectKey])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const workers = useMemo(() => {
    const grouped = new Map<
      string,
      {
        active: number
        stale: number
        fenced: number
        projects: Set<string>
        resources: Set<string>
      }
    >()
    for (const lease of leases) {
      if (!grouped.has(lease.owner)) {
        grouped.set(lease.owner, {
          active: 0,
          stale: 0,
          fenced: 0,
          projects: new Set(),
          resources: new Set(),
        })
      }
      const summary = grouped.get(lease.owner)!
      summary[lease.state] += 1
      summary.projects.add(lease.project_id)
      summary.resources.add(lease.resource_key)
    }
    return [...grouped.entries()]
      .map<WorkerSummary>(([owner, summary]) => ({
        owner,
        active: summary.active,
        stale: summary.stale,
        fenced: summary.fenced,
        projectCount: summary.projects.size,
        resources: [...summary.resources].sort(),
      }))
      .sort((left, right) => left.owner.localeCompare(right.owner))
  }, [leases])

  const staleCount = leases.filter((lease) => lease.state === "stale").length
  const fencedCount = leases.filter((lease) => lease.state === "fenced").length
  const activeGoalCount = projectSummaries.reduce(
    (total, project) => total + project.activeGoals,
    0
  )
  const degraded = staleCount > 0 || fencedCount > 0 || conflicts.length > 0
  const projectName = Object.fromEntries(
    projects.map((project) => [project.id, project.name])
  )

  if (forbidden) return null

  return (
    <Card className="mb-6">
      <CardHeader>
        <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <ShieldAlert className="size-4" /> ADMIN · CONCURRENT WORKLOAD HEALTH
        </div>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle>Installation workspace safety</CardTitle>
            <CardDescription>
              Cross-project leases, fencing evidence, conflicts, and resource
              overlap risk. This panel is visible only to admins.
            </CardDescription>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void load()}
            disabled={loading}
          >
            <RefreshCw className={loading ? "animate-spin" : ""} />
            Refresh
          </Button>
        </div>
      </CardHeader>
      <CardContent className="grid gap-5">
        {error ? (
          <div className="flex items-start justify-between gap-4 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
            <span>{error}</span>
            <Button variant="outline" size="sm" onClick={() => void load()}>
              Retry
            </Button>
          </div>
        ) : null}

        {loading && !workers.length && !projectSummaries.length ? (
          <div className="flex items-center gap-3 rounded-xl border p-4 text-sm text-muted-foreground">
            <LoaderCircle className="size-4 animate-spin" />
            Loading installation-wide workspace evidence…
          </div>
        ) : (
          <>
            <div
              className={`flex items-start gap-3 rounded-xl border p-4 text-sm ${
                degraded
                  ? "border-amber-500/30 bg-amber-500/5"
                  : "border-emerald-500/30 bg-emerald-500/5"
              }`}
            >
              {degraded ? (
                <AlertTriangle className="mt-0.5 size-4 text-amber-600" />
              ) : (
                <CheckCircle2 className="mt-0.5 size-4 text-emerald-600" />
              )}
              <span>
                {degraded
                  ? "Degraded workspace health: stale or fenced leases and conflicts need operator review."
                  : "Workspace health is clear: no stale leases, fenced workers, or recorded conflicts."}
              </span>
            </div>

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
              <Metric
                label="Active workers"
                value={workers.filter((worker) => worker.active > 0).length}
                icon={<Server className="size-4" />}
              />
              <Metric
                label="Active leases"
                value={
                  leases.filter((lease) => lease.state === "active").length
                }
                icon={<Activity className="size-4" />}
              />
              <Metric
                label="Stale / fenced"
                value={staleCount + fencedCount}
                icon={<ShieldAlert className="size-4" />}
              />
              <Metric
                label="Conflicts"
                value={conflicts.length}
                icon={<AlertTriangle className="size-4" />}
              />
              <Metric
                label="Active goals"
                value={activeGoalCount}
                icon={<Waypoints className="size-4" />}
              />
            </div>

            <div className="grid gap-5 xl:grid-cols-2">
              <section className="grid content-start gap-3">
                <div>
                  <h3 className="text-sm font-semibold">Worker lease state</h3>
                  <p className="text-xs text-muted-foreground">
                    Lease counts and stale or superseded fencing evidence by
                    worker.
                  </p>
                </div>
                {workers.length ? (
                  workers.map((worker) => (
                    <div key={worker.owner} className="rounded-xl border p-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-mono text-sm">
                          {worker.owner}
                        </span>
                        <div className="flex gap-1.5">
                          <Badge variant="outline">
                            {worker.active} active
                          </Badge>
                          {worker.stale ? (
                            <Badge variant="secondary">
                              {worker.stale} stale
                            </Badge>
                          ) : null}
                          {worker.fenced ? (
                            <Badge variant="destructive">
                              {worker.fenced} fenced
                            </Badge>
                          ) : null}
                        </div>
                      </div>
                      <p className="mt-2 text-xs text-muted-foreground">
                        {worker.projectCount} project
                        {worker.projectCount === 1 ? "" : "s"} ·{" "}
                        {worker.resources.join(", ")}
                      </p>
                    </div>
                  ))
                ) : (
                  <EmptyState>
                    No workers currently hold resource leases.
                  </EmptyState>
                )}
              </section>

              <section className="grid content-start gap-3">
                <div>
                  <h3 className="text-sm font-semibold">
                    Concurrent goals by project
                  </h3>
                  <p className="text-xs text-muted-foreground">
                    Active goals and resource keys claimed by more than one
                    goal.
                  </p>
                </div>
                {projectSummaries.length ? (
                  projectSummaries.map((summary) => (
                    <div
                      key={summary.project.id}
                      className="rounded-xl border p-3"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="text-sm font-medium">
                          {summary.project.name}
                        </span>
                        <div className="flex gap-1.5">
                          <Badge variant="outline">
                            {summary.activeGoals} active
                          </Badge>
                          {summary.conflictCount ? (
                            <Badge variant="destructive">
                              {summary.conflictCount} conflicts
                            </Badge>
                          ) : null}
                        </div>
                      </div>
                      <p className="mt-2 text-xs text-muted-foreground">
                        {summary.overlapKeys.length
                          ? `Overlap risk: ${summary.overlapKeys.join(", ")}`
                          : "No cross-goal resource overlap detected."}
                      </p>
                    </div>
                  ))
                ) : (
                  <EmptyState>
                    No projects are available to summarize.
                  </EmptyState>
                )}
              </section>
            </div>

            <div className="grid gap-5 xl:grid-cols-2">
              <section className="grid content-start gap-3">
                <div>
                  <h3 className="text-sm font-semibold">Conflict drill-down</h3>
                  <p className="text-xs text-muted-foreground">
                    Affected projects, runs, and resource revisions.
                  </p>
                </div>
                {conflicts.length ? (
                  conflicts.slice(0, 8).map((conflict) => (
                    <div
                      key={`${conflict.run_id}-${conflict.occurred_at}`}
                      className="rounded-xl border border-amber-500/30 p-3"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="text-sm font-medium">
                          {projectName[conflict.project_id] ??
                            shortId(conflict.project_id)}
                        </span>
                        <Badge variant="destructive">
                          run {shortId(conflict.run_id)}
                        </Badge>
                      </div>
                      <p className="mt-2 text-xs text-muted-foreground">
                        {conflict.resources
                          .map(
                            (resource) =>
                              `${resource.resource_key} ${resource.expected_revision}→${resource.actual_revision}`
                          )
                          .join(", ")}
                      </p>
                    </div>
                  ))
                ) : (
                  <EmptyState>
                    No workspace conflicts have been recorded.
                  </EmptyState>
                )}
              </section>

              <section className="grid content-start gap-3">
                <div>
                  <h3 className="text-sm font-semibold">
                    Recent fence and promotion evidence
                  </h3>
                  <p className="text-xs text-muted-foreground">
                    Token acquisition and immutable workspace revision activity.
                  </p>
                </div>
                {[...fenceEvents.slice(0, 4), ...promotions.slice(0, 4)]
                  .length ? (
                  <div className="grid gap-2">
                    {fenceEvents.slice(0, 4).map((event) => (
                      <div
                        key={event.id}
                        className="rounded-xl border p-3 text-sm"
                      >
                        <div className="flex items-center gap-2">
                          <GitCommitHorizontal className="size-4" />
                          Fence token{" "}
                          {String(event.payload.fencing_token ?? "—")}
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {String(event.payload.worker_id ?? "unknown worker")}{" "}
                          ·{" "}
                          {String(
                            event.payload.resource_key ?? "unknown resource"
                          )}{" "}
                          · {displayDate(event.occurred_at)}
                        </p>
                      </div>
                    ))}
                    {promotions.slice(0, 4).map((promotion) => (
                      <div
                        key={`${promotion.run_id}-${promotion.occurred_at}`}
                        className="rounded-xl border p-3 text-sm"
                      >
                        <div className="flex items-center gap-2">
                          <Waypoints className="size-4" />
                          Promotion {promotion.status}
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {projectName[promotion.project_id] ??
                            shortId(promotion.project_id)}{" "}
                          · run {shortId(promotion.run_id)} ·{" "}
                          {promotion.resource_deltas
                            .map((delta) => delta.resource_key)
                            .join(", ")}
                        </p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <EmptyState>
                    No fence-token or promotion evidence is available yet.
                  </EmptyState>
                )}
              </section>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
