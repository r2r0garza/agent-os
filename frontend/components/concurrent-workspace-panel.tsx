"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Ban,
  CheckCircle2,
  GitMerge,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Users,
  Wrench,
} from "lucide-react"

import {
  Goal,
  Identifier,
  Run,
  Task,
  TaskDependency,
  WorkspaceConflict,
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

type BadgeVariant = "default" | "secondary" | "destructive" | "outline"

const GOAL_STATUS_VARIANT: Record<string, BadgeVariant> = {
  draft: "secondary",
  active: "default",
  paused: "outline",
  completed: "default",
  cancelled: "destructive",
  failed: "destructive",
}

interface GoalWorkspaceSummary {
  goal: Goal
  tasks: Task[]
  runs: Run[]
}

function displayDate(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed bg-muted/20 px-4 py-6 text-center text-sm text-muted-foreground">
      {children}
    </div>
  )
}

interface ConcurrentWorkspacePanelProps {
  projectId: string
  goals: Goal[]
  onRefresh: () => Promise<unknown>
}

export function ConcurrentWorkspacePanel({
  projectId,
  goals,
  onRefresh,
}: ConcurrentWorkspacePanelProps) {
  const [summaries, setSummaries] = useState<GoalWorkspaceSummary[]>([])
  const [conflicts, setConflicts] = useState<WorkspaceConflict[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [mutation, setMutation] = useState("")

  const relevantGoals = useMemo(
    () => goals.filter((goal) => goal.status === "active" || goal.status === "paused"),
    [goals]
  )
  const relevantGoalsKey = relevantGoals
    .map((goal) => `${goal.id}:${goal.status}`)
    .join(",")

  const load = useCallback(async () => {
    if (!projectId) {
      setSummaries([])
      setConflicts([])
      return
    }
    setLoading(true)
    setError("")
    try {
      const [goalSummaries, conflictList] = await Promise.all([
        Promise.all(
          relevantGoals.map(async (goal) => {
            const graph = await api<{
              tasks: Task[]
              dependencies: TaskDependency[]
            }>(`/goals/${goal.id}/task-graph`)
            const runs = (
              await Promise.all(
                graph.tasks.map((task) => api<Run[]>(`/tasks/${task.id}/runs`))
              )
            ).flat()
            return { goal, tasks: graph.tasks, runs }
          })
        ),
        api<WorkspaceConflict[]>(
          `/projects/${projectId}/workspace/conflicts?limit=50`
        ),
      ])
      setSummaries(goalSummaries)
      setConflicts(conflictList)
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Unable to load concurrent workspace state"
      )
    } finally {
      setLoading(false)
    }
    // relevantGoals is derived from goals every render; relevantGoalsKey keeps
    // this effect from refetching unless the set of concurrently-relevant
    // goals (or their statuses) actually changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, relevantGoalsKey])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const goalById = useMemo(
    () => Object.fromEntries(goals.map((goal) => [goal.id, goal])),
    [goals]
  )
  const taskGoalId = useMemo(() => {
    const map: Record<Identifier, Identifier> = {}
    for (const summary of summaries) {
      for (const task of summary.tasks) {
        map[task.id] = summary.goal.id
      }
    }
    return map
  }, [summaries])

  async function discardRun(conflict: WorkspaceConflict) {
    const goalId = taskGoalId[conflict.task_id]
    if (!goalId) return
    const key = `discard-${conflict.task_id}-${conflict.run_id}`
    setMutation(key)
    setError("")
    setNotice("")
    try {
      await api(
        `/goals/${goalId}/cancel`,
        jsonBody({
          reason: "Discarded a conflicting run from the workspace conflict panel.",
        })
      )
      setNotice(
        "Conflicting run discarded. The goal is cancelling and stopping active runs cooperatively."
      )
      await Promise.all([load(), onRefresh()])
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Unable to discard the conflicting run"
      )
    } finally {
      setMutation("")
    }
  }

  async function retryFromSafeRevision(conflict: WorkspaceConflict) {
    const goalId = taskGoalId[conflict.task_id]
    if (!goalId) return
    const key = `retry-${conflict.task_id}-${conflict.run_id}`
    setMutation(key)
    setError("")
    setNotice("")
    try {
      await api(
        `/goals/${goalId}/resume`,
        jsonBody({
          reason: "Retry from the current safe resource revision after a workspace conflict.",
        })
      )
      setNotice(
        "Goal resumed. Workers will retry the affected task from the current safe resource revision."
      )
      await Promise.all([load(), onRefresh()])
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Unable to retry from the safe revision"
      )
    } finally {
      setMutation("")
    }
  }

  if (!projectId) return null

  return (
    <Card className="mb-6">
      <CardHeader>
        <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <Users className="size-4" /> CONCURRENT GOALS & WORKSPACE CONFLICTS
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle>Concurrent workspace activity</CardTitle>
            <CardDescription>
              Goals running at the same time, the resources they touch, and
              any workspace conflicts that need resolution.
            </CardDescription>
          </div>
          {loading ? (
            <Badge variant="outline" className="gap-1.5">
              <LoaderCircle className="size-3 animate-spin" /> Loading
            </Badge>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="grid gap-5">
        {error ? (
          <div className="flex items-center justify-between gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
            <span>{error}</span>
            <Button variant="outline" size="sm" onClick={() => void load()}>
              <RefreshCw className="size-3.5" /> Retry
            </Button>
          </div>
        ) : null}
        {notice ? (
          <div className="flex items-center gap-2 rounded-xl border bg-background p-4 text-sm">
            <CheckCircle2 className="size-4 text-emerald-600" /> {notice}
          </div>
        ) : null}

        <div className="grid gap-3">
          <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <GitMerge className="size-3.5" /> ACTIVE GOAL PROGRESS
          </p>
          {loading && !summaries.length ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <LoaderCircle className="size-4 animate-spin" /> Loading
              concurrent goal progress…
            </p>
          ) : relevantGoals.length < 2 ? (
            <EmptyState>
              {relevantGoals.length === 0
                ? "No goals are currently active or paused in this project."
                : "Only one goal is currently active. Concurrent goal comparisons appear here once more than one goal is running."}
            </EmptyState>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {summaries.map(({ goal, tasks, runs }) => {
                const runningCount = runs.filter(
                  (run) => run.status === "running"
                ).length
                const failedCount = runs.filter(
                  (run) => run.status === "failed"
                ).length
                const resourceIntents = Array.from(
                  new Set(
                    tasks.flatMap((task) =>
                      task.resource_intent.map(
                        (entry) => `${entry.resource_key} (${entry.intent})`
                      )
                    )
                  )
                )
                return (
                  <div key={goal.id} className="grid gap-2 rounded-xl border p-4 text-sm">
                    <div className="flex items-center justify-between gap-2">
                      <p className="font-medium">{goal.title}</p>
                      <Badge variant={GOAL_STATUS_VARIANT[goal.status] ?? "secondary"}>
                        {goal.status}
                      </Badge>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {tasks.length} task{tasks.length === 1 ? "" : "s"} ·{" "}
                      {runningCount} running · {failedCount} failed
                    </p>
                    <div className="flex flex-wrap gap-1">
                      {resourceIntents.length ? (
                        resourceIntents.map((entry) => (
                          <Badge
                            key={entry}
                            variant="outline"
                            className="font-mono text-[10px]"
                          >
                            {entry}
                          </Badge>
                        ))
                      ) : (
                        <span className="text-xs text-muted-foreground">
                          No resource intent declared
                        </span>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        <div className="grid gap-3">
          <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <Ban className="size-3.5" /> WORKSPACE CONFLICTS
          </p>
          {loading && !conflicts.length ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <LoaderCircle className="size-4 animate-spin" /> Loading
              workspace conflicts…
            </p>
          ) : conflicts.length ? (
            <div className="grid gap-3">
              {conflicts.map((conflict) => {
                const goalId = taskGoalId[conflict.task_id]
                const goal = goalId ? goalById[goalId] : undefined
                const discardKey = `discard-${conflict.task_id}-${conflict.run_id}`
                const retryKey = `retry-${conflict.task_id}-${conflict.run_id}`
                const canDiscard = Boolean(
                  goal && !["cancelled", "completed", "failed"].includes(goal.status)
                )
                const canRetry = Boolean(goal && goal.status === "paused")
                return (
                  <div
                    key={`${conflict.task_id}-${conflict.run_id}-${conflict.occurred_at}`}
                    className="grid gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <p className="font-medium">Workspace conflict detected</p>
                        <p className="text-xs text-muted-foreground">
                          {goal ? goal.title : `Task ${conflict.task_id.slice(0, 8)}`}{" "}
                          · {displayDate(conflict.occurred_at)}
                        </p>
                      </div>
                      <Badge variant="destructive">conflict</Badge>
                    </div>
                    <div className="grid gap-1">
                      {conflict.resources.map((resource) => (
                        <div
                          key={resource.resource_key}
                          className="flex items-center justify-between gap-3 text-xs"
                        >
                          <span className="font-mono">{resource.resource_key}</span>
                          <span>
                            expected {resource.expected_revision} → actual{" "}
                            {resource.actual_revision}
                          </span>
                        </div>
                      ))}
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        size="sm"
                        variant="destructive"
                        disabled={!canDiscard || Boolean(mutation)}
                        onClick={() => void discardRun(conflict)}
                      >
                        {mutation === discardKey ? (
                          <LoaderCircle className="animate-spin" />
                        ) : (
                          <Ban />
                        )}
                        Discard conflicting run
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={!canRetry || Boolean(mutation)}
                        onClick={() => void retryFromSafeRevision(conflict)}
                      >
                        {mutation === retryKey ? (
                          <LoaderCircle className="animate-spin" />
                        ) : (
                          <RotateCcw />
                        )}
                        Retry from safe revision
                      </Button>
                      <Button size="sm" variant="ghost" disabled>
                        <Wrench /> Manual resolution
                      </Button>
                    </div>
                    {!canRetry ? (
                      <p className="text-xs text-muted-foreground">
                        Retry is available once the goal is paused; manual
                        resolution is not yet automated.
                      </p>
                    ) : null}
                  </div>
                )
              })}
            </div>
          ) : (
            <EmptyState>
              No workspace conflicts have been detected for this project.
            </EmptyState>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
