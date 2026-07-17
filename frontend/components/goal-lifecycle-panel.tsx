"use client"

import { FormEvent, useCallback, useEffect, useState } from "react"
import {
  Ban,
  CheckCircle2,
  Compass,
  GitBranch,
  History,
  LoaderCircle,
  PauseCircle,
  PlayCircle,
  RefreshCw,
  ShieldAlert,
} from "lucide-react"

import {
  ApiError,
  Goal,
  GoalLifecycleCommand,
  GoalLifecycleEvent,
  GoalSteeringRequest,
  SteeringTaskChangeInput,
  SteeringTaskSpecInput,
  TaskGraphRevision,
  TaskGraphRevisionDetail,
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

type BadgeVariant = "default" | "secondary" | "destructive" | "outline"

const GOAL_STATUS_META: Record<string, { label: string; variant: BadgeVariant }> = {
  draft: { label: "Draft", variant: "secondary" },
  active: { label: "Active", variant: "default" },
  paused: { label: "Paused", variant: "outline" },
  completed: { label: "Completed", variant: "default" },
  cancelled: { label: "Cancelled", variant: "destructive" },
  failed: { label: "Failed", variant: "destructive" },
}

interface ChangeRow {
  changeType: "added" | "revised" | "superseded"
  clientId: string
  taskId: string
  title: string
  description: string
  dependsOn: string
}

function blankRow(): ChangeRow {
  return {
    changeType: "added",
    clientId: "",
    taskId: "",
    title: "",
    description: "",
    dependsOn: "",
  }
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
    <div className="rounded-xl border border-dashed bg-muted/20 px-4 py-6 text-center text-sm text-muted-foreground">
      {children}
    </div>
  )
}

interface GoalLifecyclePanelProps {
  goalId: string
  goal: Goal | undefined
  onRefresh: () => Promise<unknown>
}

export function GoalLifecyclePanel({
  goalId,
  goal,
  onRefresh,
}: GoalLifecyclePanelProps) {
  const [commands, setCommands] = useState<GoalLifecycleCommand[]>([])
  const [steeringRequests, setSteeringRequests] = useState<
    GoalSteeringRequest[]
  >([])
  const [revisions, setRevisions] = useState<TaskGraphRevision[]>([])
  const [lifecycleEvents, setLifecycleEvents] = useState<
    GoalLifecycleEvent[]
  >([])
  const [revisionDetails, setRevisionDetails] = useState<
    Record<number, TaskGraphRevisionDetail>
  >({})
  const [expandedRevision, setExpandedRevision] = useState<number | null>(
    null
  )
  const [loading, setLoading] = useState(false)
  const [unauthorized, setUnauthorized] = useState(false)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [mutation, setMutation] = useState("")
  const [reason, setReason] = useState("")
  const [applyingRequestId, setApplyingRequestId] = useState<string | null>(
    null
  )
  const [changeSummary, setChangeSummary] = useState("")
  const [changeRows, setChangeRows] = useState<ChangeRow[]>([blankRow()])

  const load = useCallback(async () => {
    if (!goalId) {
      setCommands([])
      setSteeringRequests([])
      setRevisions([])
      setLifecycleEvents([])
      return
    }
    setLoading(true)
    setError("")
    try {
      const [commandList, steeringList, revisionList, eventList] =
        await Promise.all([
          api<GoalLifecycleCommand[]>(`/goals/${goalId}/lifecycle-commands`),
          api<GoalSteeringRequest[]>(`/goals/${goalId}/steering-requests`),
          api<TaskGraphRevision[]>(`/goals/${goalId}/graph-revisions`),
          api<GoalLifecycleEvent[]>(`/goals/${goalId}/lifecycle-events`),
        ])
      setCommands(commandList)
      setSteeringRequests(steeringList)
      setRevisions(revisionList)
      setLifecycleEvents(eventList)
      setUnauthorized(false)
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 404) {
        setUnauthorized(true)
        setCommands([])
        setSteeringRequests([])
        setRevisions([])
        setLifecycleEvents([])
      } else {
        setError(
          caught instanceof Error
            ? caught.message
            : "Unable to load goal lifecycle state"
        )
      }
    } finally {
      setLoading(false)
    }
  }, [goalId])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setRevisionDetails({})
      setExpandedRevision(null)
      setApplyingRequestId(null)
      void load()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [load])

  async function runLifecycleCommand(command: "pause" | "resume" | "cancel") {
    if (!goalId) return
    setMutation(command)
    setError("")
    setNotice("")
    try {
      await api<GoalLifecycleCommand>(
        `/goals/${goalId}/${command}`,
        jsonBody({ reason: reason.trim() || null })
      )
      setReason("")
      setNotice(
        `Goal ${command} requested and persisted. Workers honor the control at the next safe boundary.`
      )
      await Promise.all([load(), onRefresh()])
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : `Unable to ${command} the goal`
      )
    } finally {
      setMutation("")
    }
  }

  async function submitSteering(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!goalId) return
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    const instructionValue = String(form.get("instruction") ?? "").trim()
    if (!instructionValue) return
    const baseRevisionRaw = String(
      form.get("base_revision_number") ?? ""
    ).trim()
    setMutation("steer")
    setError("")
    setNotice("")
    try {
      await api<GoalSteeringRequest>(
        `/goals/${goalId}/steer`,
        jsonBody({
          instruction: instructionValue,
          base_revision_number: baseRevisionRaw
            ? Number(baseRevisionRaw)
            : null,
        })
      )
      setNotice("Steering instruction submitted and persisted for review.")
      formElement.reset()
      await load()
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Unable to submit steering instruction"
      )
    } finally {
      setMutation("")
    }
  }

  function updateChangeRow(index: number, patch: Partial<ChangeRow>) {
    setChangeRows((rows) =>
      rows.map((row, rowIndex) =>
        rowIndex === index ? { ...row, ...patch } : row
      )
    )
  }

  function addChangeRow() {
    setChangeRows((rows) => [...rows, blankRow()])
  }

  function removeChangeRow(index: number) {
    setChangeRows((rows) => rows.filter((_, rowIndex) => rowIndex !== index))
  }

  async function applySteering(request: GoalSteeringRequest) {
    if (!goalId) return
    const changes: SteeringTaskChangeInput[] = changeRows
      .filter((row) => row.title.trim() || row.taskId.trim())
      .map((row) => {
        if (row.changeType === "superseded") {
          return { change_type: "superseded", task_id: row.taskId.trim() }
        }
        const task: SteeringTaskSpecInput = {
          client_id: row.clientId.trim(),
          title: row.title.trim(),
          description: row.description.trim() || null,
          depends_on: row.dependsOn
            .split(",")
            .map((value) => value.trim())
            .filter(Boolean),
        }
        return row.changeType === "revised"
          ? { change_type: "revised", task_id: row.taskId.trim(), task }
          : { change_type: "added", task }
      })

    if (!changeSummary.trim() || !changes.length) {
      setError(
        "Provide a change summary and at least one task change before applying."
      )
      return
    }

    setMutation(`apply-${request.id}`)
    setError("")
    setNotice("")
    try {
      await api<TaskGraphRevisionDetail>(
        `/goals/${goalId}/steering-requests/${request.id}/apply`,
        jsonBody({ change_summary: changeSummary.trim(), changes })
      )
      setNotice("Steering applied as a durable task graph revision.")
      setChangeSummary("")
      setChangeRows([blankRow()])
      setApplyingRequestId(null)
      await Promise.all([load(), onRefresh()])
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Unable to apply the steering request"
      )
    } finally {
      setMutation("")
    }
  }

  async function toggleRevision(revisionNumber: number) {
    if (expandedRevision === revisionNumber) {
      setExpandedRevision(null)
      return
    }
    setExpandedRevision(revisionNumber)
    if (!goalId || revisionDetails[revisionNumber]) return
    try {
      const detail = await api<TaskGraphRevisionDetail>(
        `/goals/${goalId}/graph-revisions/${revisionNumber}`
      )
      setRevisionDetails((current) => ({
        ...current,
        [revisionNumber]: detail,
      }))
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Unable to load the revision detail"
      )
    }
  }

  if (!goalId) {
    return (
      <Card className="mb-6">
        <CardContent className="py-8 text-center text-sm text-muted-foreground">
          Select a goal to inspect its lifecycle controls and steering
          history.
        </CardContent>
      </Card>
    )
  }

  const status = goal?.status ?? "unknown"
  const statusMeta = GOAL_STATUS_META[status] ?? {
    label: status,
    variant: "secondary" as const,
  }
  const pendingControl = goal?.pending_control ?? null
  const terminal = ["completed", "cancelled", "failed"].includes(status)
  const canPause = status === "active" && !pendingControl
  const canResume = status === "paused" && !pendingControl
  const canCancel = !terminal && !pendingControl
  const requestedSteering = steeringRequests.filter(
    (request) => request.status === "requested"
  )
  const resolvedSteering = [...steeringRequests]
    .filter((request) => request.status !== "requested")
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
  const orderedRevisions = [...revisions].sort(
    (a, b) => b.revision_number - a.revision_number
  )
  const orderedEvents = [...lifecycleEvents].sort(
    (a, b) => b.sequence_number - a.sequence_number
  )
  const recentCommands = [...commands]
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .slice(0, 6)

  return (
    <Card className="mb-6">
      <CardHeader>
        <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <Compass className="size-4" /> GOAL LIFECYCLE & STEERING
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle>Lifecycle controls</CardTitle>
            <CardDescription>
              Pause, resume, cancel, and steer the selected goal with durable,
              attributable evidence.
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant={statusMeta.variant}>{statusMeta.label}</Badge>
            {pendingControl ? (
              <Badge variant="outline" className="gap-1.5">
                <LoaderCircle className="size-3 animate-spin" />
                Applying {pendingControl}…
              </Badge>
            ) : null}
          </div>
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

        {unauthorized ? (
          <div className="flex gap-3 rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-sm">
            <ShieldAlert className="mt-0.5 size-4 text-amber-600" />
            <span>
              You do not have access to this goal&apos;s lifecycle controls,
              or the goal no longer exists.
            </span>
          </div>
        ) : (
          <>
            {status === "cancelled" ? (
              <div className="flex gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
                <Ban className="mt-0.5 size-4 text-destructive" />
                <span>
                  Cancellation is durable.{" "}
                  {goal?.forced_termination_completed_at
                    ? `Sandboxes were forcibly terminated at ${displayDate(goal.forced_termination_completed_at)}.`
                    : goal?.cancellation_grace_expires_at
                      ? `Active runs have a cooperative grace period until ${displayDate(goal.cancellation_grace_expires_at)}, after which termination is forced.`
                      : "Active runs are stopping cooperatively."}
                </span>
              </div>
            ) : null}

            <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
              <Textarea
                aria-label="Lifecycle command reason"
                placeholder="Optional reason for this control (visible in audit evidence)"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                disabled={terminal}
              />
              <div className="flex flex-wrap items-start gap-2 sm:flex-col">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!canPause || Boolean(mutation)}
                  onClick={() => void runLifecycleCommand("pause")}
                >
                  {mutation === "pause" ? (
                    <LoaderCircle className="animate-spin" />
                  ) : (
                    <PauseCircle />
                  )}
                  Pause
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!canResume || Boolean(mutation)}
                  onClick={() => void runLifecycleCommand("resume")}
                >
                  {mutation === "resume" ? (
                    <LoaderCircle className="animate-spin" />
                  ) : (
                    <PlayCircle />
                  )}
                  Resume
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  disabled={!canCancel || Boolean(mutation)}
                  onClick={() => void runLifecycleCommand("cancel")}
                >
                  {mutation === "cancel" ? (
                    <LoaderCircle className="animate-spin" />
                  ) : (
                    <Ban />
                  )}
                  Cancel
                </Button>
              </div>
            </div>

            {recentCommands.length ? (
              <div className="grid gap-2">
                <p className="text-xs font-medium text-muted-foreground">
                  RECENT LIFECYCLE COMMANDS
                </p>
                {recentCommands.map((command) => (
                  <div
                    key={command.id}
                    className="flex items-center justify-between gap-3 rounded-lg border p-3 text-xs"
                  >
                    <span>
                      {command.command_type} · {command.reason ?? "no reason supplied"}
                    </span>
                    <Badge
                      variant={
                        command.status === "rejected" ? "destructive" : "outline"
                      }
                    >
                      {command.status}
                    </Badge>
                  </div>
                ))}
              </div>
            ) : null}

            <form
              className="grid gap-3 rounded-xl border p-4"
              onSubmit={submitSteering}
            >
              <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                <Compass className="size-3.5" /> SUBMIT STEERING INSTRUCTION
              </div>
              <Textarea
                name="instruction"
                placeholder="Describe how the remaining work should change…"
                disabled={terminal}
                required
              />
              <div className="flex items-end gap-3">
                <div className="grid gap-1.5">
                  <Label htmlFor="base-revision">
                    Base revision (optional)
                  </Label>
                  <Input
                    id="base-revision"
                    name="base_revision_number"
                    type="number"
                    min="0"
                    placeholder={`${goal?.active_graph_revision_number ?? 0}`}
                    disabled={terminal}
                  />
                </div>
                <Button
                  type="submit"
                  size="sm"
                  disabled={terminal || mutation === "steer"}
                >
                  {mutation === "steer" ? (
                    <LoaderCircle className="animate-spin" />
                  ) : (
                    <Compass />
                  )}
                  Submit steering instruction
                </Button>
              </div>
            </form>
          </>
        )}

        <div className="grid gap-3">
          <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <History className="size-3.5" /> STEERING REQUESTS
          </p>
          {loading && !steeringRequests.length ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <LoaderCircle className="size-4 animate-spin" /> Loading
              steering history…
            </p>
          ) : requestedSteering.length || resolvedSteering.length ? (
            <div className="grid gap-3">
              {requestedSteering.map((request) => (
                <div key={request.id} className="grid gap-3 rounded-xl border p-4 text-sm">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <p className="font-medium">{request.instruction}</p>
                      <p className="text-xs text-muted-foreground">
                        Base revision {request.base_revision_number} ·{" "}
                        {displayDate(request.created_at)}
                      </p>
                    </div>
                    <Badge variant="outline">{request.status}</Badge>
                  </div>
                  {!unauthorized ? (
                    applyingRequestId === request.id ? (
                      <div className="grid gap-3 border-t pt-3">
                        <Textarea
                          aria-label="Change summary"
                          placeholder="Summarize this revision"
                          value={changeSummary}
                          onChange={(event) =>
                            setChangeSummary(event.target.value)
                          }
                        />
                        {changeRows.map((row, index) => (
                          <div
                            key={index}
                            className="grid gap-2 rounded-lg border p-3 sm:grid-cols-2"
                          >
                            <div className="grid gap-1.5">
                              <Label htmlFor={`change-type-${index}`}>
                                Change type
                              </Label>
                              <select
                                id={`change-type-${index}`}
                                className="h-9 rounded-lg border bg-background px-3 text-sm"
                                value={row.changeType}
                                onChange={(event) =>
                                  updateChangeRow(index, {
                                    changeType: event.target
                                      .value as ChangeRow["changeType"],
                                  })
                                }
                              >
                                <option value="added">Added</option>
                                <option value="revised">Revised</option>
                                <option value="superseded">Superseded</option>
                              </select>
                            </div>
                            {row.changeType !== "added" ? (
                              <div className="grid gap-1.5">
                                <Label htmlFor={`task-id-${index}`}>
                                  Task ID
                                </Label>
                                <Input
                                  id={`task-id-${index}`}
                                  value={row.taskId}
                                  onChange={(event) =>
                                    updateChangeRow(index, {
                                      taskId: event.target.value,
                                    })
                                  }
                                />
                              </div>
                            ) : null}
                            {row.changeType !== "superseded" ? (
                              <>
                                <div className="grid gap-1.5">
                                  <Label htmlFor={`client-id-${index}`}>
                                    Client ID
                                  </Label>
                                  <Input
                                    id={`client-id-${index}`}
                                    value={row.clientId}
                                    onChange={(event) =>
                                      updateChangeRow(index, {
                                        clientId: event.target.value,
                                      })
                                    }
                                  />
                                </div>
                                <div className="grid gap-1.5">
                                  <Label htmlFor={`title-${index}`}>
                                    Title
                                  </Label>
                                  <Input
                                    id={`title-${index}`}
                                    value={row.title}
                                    onChange={(event) =>
                                      updateChangeRow(index, {
                                        title: event.target.value,
                                      })
                                    }
                                  />
                                </div>
                                <div className="grid gap-1.5 sm:col-span-2">
                                  <Label htmlFor={`depends-on-${index}`}>
                                    Depends on (comma separated)
                                  </Label>
                                  <Input
                                    id={`depends-on-${index}`}
                                    value={row.dependsOn}
                                    onChange={(event) =>
                                      updateChangeRow(index, {
                                        dependsOn: event.target.value,
                                      })
                                    }
                                  />
                                </div>
                              </>
                            ) : null}
                            <div className="sm:col-span-2">
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                onClick={() => removeChangeRow(index)}
                                disabled={changeRows.length <= 1}
                              >
                                Remove change
                              </Button>
                            </div>
                          </div>
                        ))}
                        <div className="flex items-center justify-between gap-2">
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={addChangeRow}
                          >
                            Add change
                          </Button>
                          <div className="flex gap-2">
                            <Button
                              type="button"
                              variant="outline"
                              size="sm"
                              onClick={() => setApplyingRequestId(null)}
                            >
                              Cancel
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              disabled={mutation === `apply-${request.id}`}
                              onClick={() => void applySteering(request)}
                            >
                              {mutation === `apply-${request.id}` ? (
                                <LoaderCircle className="animate-spin" />
                              ) : (
                                <GitBranch />
                              )}
                              Apply as revision
                            </Button>
                          </div>
                        </div>
                      </div>
                    ) : (
                      <Button
                        variant="outline"
                        size="sm"
                        className="justify-self-start"
                        onClick={() => setApplyingRequestId(request.id)}
                      >
                        <GitBranch /> Apply as revision
                      </Button>
                    )
                  ) : null}
                </div>
              ))}
              {resolvedSteering.map((request) => (
                <div
                  key={request.id}
                  className="flex items-start justify-between gap-3 rounded-lg border p-3 text-xs"
                >
                  <div>
                    <p className="font-medium">{request.instruction}</p>
                    <p className="mt-1 text-muted-foreground">
                      {request.applied_revision_number !== null
                        ? `Applied as revision ${request.applied_revision_number}`
                        : "Rejected"}{" "}
                      · {displayDate(request.resolved_at)}
                    </p>
                  </div>
                  <Badge
                    variant={
                      request.status === "rejected" ? "destructive" : "outline"
                    }
                  >
                    {request.status}
                  </Badge>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState>
              No steering instructions have been submitted for this goal yet.
            </EmptyState>
          )}
        </div>

        <div className="grid gap-3">
          <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <GitBranch className="size-3.5" /> TASK GRAPH REVISIONS
          </p>
          {orderedRevisions.length ? (
            <div className="grid gap-2">
              {orderedRevisions.map((revision) => {
                const detail = revisionDetails[revision.revision_number]
                const expanded = expandedRevision === revision.revision_number
                return (
                  <div key={revision.id} className="rounded-lg border p-3 text-xs">
                    <button
                      type="button"
                      className="flex w-full items-center justify-between gap-2 text-left"
                      onClick={() => void toggleRevision(revision.revision_number)}
                    >
                      <span>
                        Revision {revision.revision_number} ·{" "}
                        {revision.change_summary ?? "No summary"}
                      </span>
                      <span className="text-muted-foreground">
                        {displayDate(revision.created_at)}
                      </span>
                    </button>
                    {expanded ? (
                      <div className="mt-2 grid gap-1 border-t pt-2">
                        {detail ? (
                          detail.tasks.map((task) => (
                            <div
                              key={task.task_id}
                              className="flex items-center justify-between gap-2"
                            >
                              <span>
                                {(task.task_snapshot.title as string) ??
                                  task.task_id.slice(0, 8)}
                              </span>
                              <Badge variant="outline">{task.change_type}</Badge>
                            </div>
                          ))
                        ) : (
                          <p className="flex items-center gap-2 text-muted-foreground">
                            <LoaderCircle className="size-3 animate-spin" />{" "}
                            Loading revision detail…
                          </p>
                        )}
                      </div>
                    ) : null}
                  </div>
                )
              })}
            </div>
          ) : (
            <EmptyState>
              No task graph revisions have been recorded for this goal yet.
            </EmptyState>
          )}
        </div>

        <div className="grid gap-3">
          <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <History className="size-3.5" /> INTERVENTION & LIFECYCLE HISTORY
          </p>
          {orderedEvents.length ? (
            <div className="grid gap-2">
              {orderedEvents.slice(0, 20).map((event) => (
                <div
                  key={event.id}
                  className="flex items-center justify-between gap-3 rounded-lg border p-3 text-xs"
                >
                  <div>
                    <p className="font-medium">{event.event_type}</p>
                    <p className="mt-0.5 text-muted-foreground">
                      {event.prior_goal_status ?? "—"} →{" "}
                      {event.resulting_goal_status ?? "—"} ·{" "}
                      {displayDate(event.occurred_at)}
                    </p>
                  </div>
                  <span className="font-mono text-[10px] text-muted-foreground">
                    #{event.sequence_number}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState>
              No lifecycle or steering events are recorded for this goal yet.
            </EmptyState>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
