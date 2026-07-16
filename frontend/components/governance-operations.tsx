"use client"

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react"
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  CircleDollarSign,
  Clock3,
  History,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react"

import {
  AdminOverride,
  ApiError,
  ApprovalRequest,
  GovernanceEvidence,
  Run,
  Task,
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

function displayDate(value: string | null) {
  if (!value) return "No expiry"
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}

function compactJson(value: Record<string, unknown>) {
  return Object.keys(value).length
    ? JSON.stringify(value, null, 2)
    : "No evidence supplied."
}

function statusVariant(
  status: string
): "outline" | "secondary" | "destructive" {
  if (["denied", "expired", "cancelled", "rejected"].includes(status)) {
    return "destructive"
  }
  return status === "pending" || status === "active" ? "outline" : "secondary"
}

interface GovernanceOperationsProps {
  projectId: string
  tasks: Task[]
  runs: Run[]
  onRefresh: () => Promise<unknown>
}

export function GovernanceOperations({
  projectId,
  tasks,
  runs,
  onRefresh,
}: GovernanceOperationsProps) {
  const [requests, setRequests] = useState<ApprovalRequest[]>([])
  const [overrides, setOverrides] = useState<AdminOverride[]>([])
  const [evidence, setEvidence] = useState<GovernanceEvidence | null>(null)
  const [loading, setLoading] = useState(false)
  const [mutation, setMutation] = useState("")
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [adminUnauthorized, setAdminUnauthorized] = useState(false)
  const [reasons, setReasons] = useState<Record<string, string>>({})

  const load = useCallback(async () => {
    if (!projectId) {
      setRequests([])
      setOverrides([])
      setEvidence(null)
      return
    }
    setLoading(true)
    setError("")
    try {
      const [requestList, governanceEvidence] = await Promise.all([
        api<ApprovalRequest[]>(
          `/approval-requests?project_id=${projectId}&limit=250`
        ),
        api<GovernanceEvidence>(
          `/governance/evidence?project_id=${projectId}&limit=500`
        ),
      ])
      setRequests(requestList)
      setEvidence(governanceEvidence)
      try {
        setOverrides(
          await api<AdminOverride[]>(
            `/admin-overrides?project_id=${projectId}&limit=250`
          )
        )
        setAdminUnauthorized(false)
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 403) {
          setOverrides([])
          setAdminUnauthorized(true)
        } else {
          throw reason
        }
      }
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Unable to load governance state"
      )
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const pending = requests.filter((request) => request.status === "pending")
  const history = [...requests]
    .filter((request) => request.status !== "pending")
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
  const blockedTasks = tasks.filter((task) =>
    ["blocked", "failed"].includes(task.status)
  )
  const waitingRuns = runs.filter((run) =>
    ["waiting_approval", "failed"].includes(run.status)
  )
  const goalIds = useMemo(
    () => [...new Set(tasks.map((task) => task.goal_id))],
    [tasks]
  )
  const governanceEvents = (evidence?.audit_events ?? []).filter(
    (event) =>
      event.event_type.startsWith("approval.") ||
      event.event_type.startsWith("budget.") ||
      event.event_type.startsWith("policy.") ||
      event.event_type.startsWith("governance.")
  )
  const scopeOptions = useMemo(
    () => [
      {
        type: "project",
        id: projectId,
        label: `Project · ${projectId.slice(0, 8)}`,
      },
      ...goalIds.map((id) => ({
        type: "goal",
        id,
        label: `Goal · ${id.slice(0, 8)}`,
      })),
      ...tasks.map((task) => ({
        type: "task",
        id: task.id,
        label: `Task · ${task.title}`,
      })),
      ...runs.map((run) => ({
        type: "run",
        id: run.id,
        label: `Run · attempt ${run.attempt_number} (${run.id.slice(0, 8)})`,
      })),
    ],
    [goalIds, projectId, runs, tasks]
  )

  async function resolve(
    request: ApprovalRequest,
    decision: "approve" | "deny"
  ) {
    setMutation(`${decision}-${request.id}`)
    setError("")
    setNotice("")
    try {
      await api(
        `/approval-requests/${request.id}/${decision}`,
        jsonBody({
          reason: reasons[request.id]?.trim() || null,
        })
      )
      setNotice(
        decision === "approve"
          ? "Approval persisted. The task is ready to resume when all gates are clear."
          : "Denial persisted. The worker will not perform the gated action."
      )
      await Promise.all([load(), onRefresh()])
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to resolve approval"
      )
    } finally {
      setMutation("")
    }
  }

  async function createOverride(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    const [scopeType, scopeId] = String(form.get("scope") ?? "").split(":")
    setMutation("override")
    setError("")
    setNotice("")
    try {
      await api<AdminOverride>(
        "/admin-overrides",
        jsonBody({
          scope_type: scopeType,
          scope_id: scopeId,
          reason: form.get("reason"),
          expires_at: new Date(String(form.get("expires_at"))).toISOString(),
        })
      )
      setNotice("Scoped admin override persisted with audit evidence.")
      formElement.reset()
      await Promise.all([load(), onRefresh()])
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 403)
        setAdminUnauthorized(true)
      setError(
        reason instanceof Error ? reason.message : "Unable to create override"
      )
    } finally {
      setMutation("")
    }
  }

  if (!projectId) {
    return (
      <Card className="mb-6">
        <CardContent className="py-8 text-center text-sm text-muted-foreground">
          Select a project to load its durable approval and budget governance
          state.
        </CardContent>
      </Card>
    )
  }

  return (
    <section className="mb-6 grid gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <ShieldAlert className="size-4" /> APPROVALS & GOVERNANCE OPERATIONS
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void load()}
          disabled={loading}
        >
          <RefreshCw className={loading ? "animate-spin" : ""} /> Refresh
          governance
        </Button>
      </div>

      {error ? (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          <span>{error}</span>
          <Button variant="outline" size="sm" onClick={() => void load()}>
            Retry
          </Button>
        </div>
      ) : null}
      {notice ? (
        <div className="flex items-center gap-2 rounded-xl border bg-background p-4 text-sm">
          <CheckCircle2 className="size-4 text-emerald-600" /> {notice}
        </div>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Clock3 className="size-4" /> Pending approvals
            </CardTitle>
            <CardDescription>
              Review the redacted action preview before allowing or denying a
              side effect.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3">
            {loading && !requests.length ? (
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                <LoaderCircle className="size-4 animate-spin" /> Loading durable
                requests…
              </p>
            ) : pending.length ? (
              pending.map((request) => (
                <div
                  key={request.id}
                  className="grid gap-3 rounded-xl border p-4 text-sm"
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <p className="font-medium">
                        {request.action_type.replaceAll("_", " ")}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        Run {request.run_id.slice(0, 8)} · expires{" "}
                        {displayDate(request.expires_at)}
                      </p>
                    </div>
                    <Badge variant="outline">
                      {request.mode.replaceAll("_", " ")}
                    </Badge>
                  </div>
                  <pre className="max-h-40 overflow-auto rounded-lg bg-muted/40 p-3 text-xs whitespace-pre-wrap">
                    {compactJson(request.action_preview)}
                  </pre>
                  <details className="text-xs text-muted-foreground">
                    <summary className="cursor-pointer">
                      Policy evidence
                    </summary>
                    <pre className="mt-2 overflow-auto whitespace-pre-wrap">
                      {compactJson(request.policy_evidence)}
                    </pre>
                  </details>
                  <Textarea
                    aria-label={`Decision reason for ${request.action_type}`}
                    placeholder="Optional decision reason"
                    value={reasons[request.id] ?? ""}
                    onChange={(event) =>
                      setReasons((current) => ({
                        ...current,
                        [request.id]: event.target.value,
                      }))
                    }
                  />
                  <div className="flex justify-end gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={Boolean(mutation)}
                      onClick={() => void resolve(request, "deny")}
                    >
                      {mutation === `deny-${request.id}` ? (
                        <LoaderCircle className="animate-spin" />
                      ) : (
                        <Ban />
                      )}
                      Deny
                    </Button>
                    <Button
                      size="sm"
                      disabled={Boolean(mutation)}
                      onClick={() => void resolve(request, "approve")}
                    >
                      {mutation === `approve-${request.id}` ? (
                        <LoaderCircle className="animate-spin" />
                      ) : (
                        <ShieldCheck />
                      )}
                      Approve
                    </Button>
                  </div>
                </div>
              ))
            ) : (
              <div className="rounded-xl border border-dashed p-7 text-center text-sm text-muted-foreground">
                No pending approval requests for this project.
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <History className="size-4" /> Resolved history
            </CardTitle>
            <CardDescription>
              Approved, denied, expired, and cancelled requests remain visible
              after reload.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-2">
            {history.length ? (
              history.map((request) => (
                <div
                  key={request.id}
                  className="flex items-start justify-between gap-3 rounded-lg border p-3 text-xs"
                >
                  <div>
                    <p className="font-medium">
                      {request.action_type.replaceAll("_", " ")}
                    </p>
                    <p className="text-muted-foreground">
                      Run {request.run_id.slice(0, 8)} ·{" "}
                      {displayDate(request.resolved_at)}
                    </p>
                  </div>
                  <Badge variant={statusVariant(request.status)}>
                    {request.status}
                  </Badge>
                </div>
              ))
            ) : (
              <div className="rounded-xl border border-dashed p-7 text-center text-sm text-muted-foreground">
                No resolved approval history yet.
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ShieldCheck className="size-4" /> Admin overrides
            </CardTitle>
            <CardDescription>
              Time-bound exceptions are scoped to one project, task, or run and
              recorded in audit evidence.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            {adminUnauthorized ? (
              <div className="flex gap-3 rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-sm">
                <ShieldAlert className="mt-0.5 size-4 text-amber-600" />
                <span>
                  Admin role required. Approval decisions remain available for
                  authorized project members.
                </span>
              </div>
            ) : (
              <form className="grid gap-3" onSubmit={createOverride}>
                <div className="grid gap-1.5">
                  <Label htmlFor="override-scope">Scope</Label>
                  <select
                    id="override-scope"
                    name="scope"
                    required
                    className="h-9 rounded-lg border bg-background px-3 text-sm"
                  >
                    {scopeOptions.map((option) => (
                      <option
                        key={`${option.type}:${option.id}`}
                        value={`${option.type}:${option.id}`}
                      >
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="grid gap-1.5">
                  <Label htmlFor="override-expiry">Expires at</Label>
                  <Input
                    id="override-expiry"
                    name="expires_at"
                    type="datetime-local"
                    required
                  />
                </div>
                <div className="grid gap-1.5">
                  <Label htmlFor="override-reason">Reason</Label>
                  <Textarea
                    id="override-reason"
                    name="reason"
                    required
                    placeholder="Why is this bounded exception necessary?"
                  />
                </div>
                <Button type="submit" disabled={mutation === "override"}>
                  {mutation === "override" ? (
                    <LoaderCircle className="animate-spin" />
                  ) : (
                    <ShieldCheck />
                  )}
                  Create scoped override
                </Button>
              </form>
            )}
            <div className="grid gap-2 border-t pt-3">
              {overrides.length ? (
                [...overrides].reverse().map((override) => (
                  <div
                    key={override.id}
                    className="rounded-lg border p-3 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <p className="font-medium">
                        {override.scope_type} · {override.scope_id.slice(0, 8)}
                      </p>
                      <Badge
                        variant={
                          new Date(override.expires_at) > new Date()
                            ? "outline"
                            : "secondary"
                        }
                      >
                        {new Date(override.expires_at) > new Date()
                          ? "active"
                          : "expired"}
                      </Badge>
                    </div>
                    <p className="mt-1">{override.reason}</p>
                    <p className="mt-1 text-muted-foreground">
                      Expires {displayDate(override.expires_at)}
                    </p>
                  </div>
                ))
              ) : !adminUnauthorized ? (
                <p className="text-sm text-muted-foreground">
                  No override history for this project.
                </p>
              ) : null}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <AlertTriangle className="size-4" /> Blocked & recovery state
            </CardTitle>
            <CardDescription>
              Pending gates, failed work, and durable retry boundaries from the
              current task graph.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-2 text-sm">
            {blockedTasks.map((task) => (
              <div
                key={task.id}
                className="flex items-center justify-between gap-2 rounded-lg border p-3"
              >
                <span>{task.title}</span>
                <Badge variant="destructive">{task.status}</Badge>
              </div>
            ))}
            {waitingRuns.map((run) => (
              <div
                key={run.id}
                className="flex items-center justify-between gap-2 rounded-lg border p-3"
              >
                <span className="flex items-center gap-2">
                  <RotateCcw className="size-4" /> Run {run.id.slice(0, 8)}
                </span>
                <Badge
                  variant={
                    run.status === "waiting_approval"
                      ? "outline"
                      : "destructive"
                  }
                >
                  {run.status.replaceAll("_", " ")}
                </Badge>
              </div>
            ))}
            {!blockedTasks.length && !waitingRuns.length ? (
              <div className="rounded-xl border border-dashed p-7 text-center text-muted-foreground">
                No blocked or recoverable work in the selected goal.
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <CircleDollarSign className="size-4" /> Budget & policy evidence
          </CardTitle>
          <CardDescription>
            Reservations, reconciled costs, warning thresholds, hard stops,
            unpriced actions, and policy events from the backend.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 lg:grid-cols-2">
          <div className="grid content-start gap-2">
            <p className="text-xs font-medium text-muted-foreground">
              RESERVATIONS & LEDGER
            </p>
            {evidence?.budget_reservations.length ? (
              evidence.budget_reservations.map((reservation) => (
                <div
                  key={reservation.id}
                  className="rounded-lg border p-3 text-xs"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span>{reservation.action_type.replaceAll("_", " ")}</span>
                    <Badge
                      variant={
                        reservation.hard_stop_triggered
                          ? "destructive"
                          : "outline"
                      }
                    >
                      {reservation.is_unpriced
                        ? "unpriced"
                        : `${reservation.amount_minor_units} ${reservation.currency} minor`}
                    </Badge>
                  </div>
                  <p className="mt-1 text-muted-foreground">
                    {reservation.status}
                    {reservation.warning_triggered
                      ? " · warning threshold"
                      : ""}
                    {reservation.hard_stop_triggered ? " · hard stop" : ""}
                  </p>
                </div>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">
                No budget reservations recorded.
              </p>
            )}
            {evidence?.cost_ledger_entries.map((entry) => (
              <div
                key={entry.id}
                className="flex items-center justify-between gap-2 rounded-lg border p-3 text-xs"
              >
                <span>
                  {entry.action_type.replaceAll("_", " ")} · {entry.status}
                </span>
                <Badge
                  variant={
                    entry.hard_stop_triggered ? "destructive" : "secondary"
                  }
                >
                  {entry.is_unpriced
                    ? "unpriced"
                    : `${entry.actual_amount_minor_units ?? entry.reserved_amount_minor_units} ${entry.currency} minor`}
                </Badge>
              </div>
            ))}
          </div>
          <div className="grid content-start gap-2">
            <p className="text-xs font-medium text-muted-foreground">
              POLICY & APPROVAL EVENTS
            </p>
            {governanceEvents
              .slice(-20)
              .reverse()
              .map((event) => (
                <div key={event.id} className="rounded-lg border p-3 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span>{event.event_type}</span>
                    <span className="text-muted-foreground">
                      {displayDate(event.occurred_at)}
                    </span>
                  </div>
                  {typeof event.payload.reason === "string" ? (
                    <p className="mt-1 text-muted-foreground">
                      {event.payload.reason}
                    </p>
                  ) : null}
                </div>
              ))}
            {!governanceEvents.length ? (
              <p className="text-sm text-muted-foreground">
                No policy evidence recorded.
              </p>
            ) : null}
          </div>
        </CardContent>
      </Card>
    </section>
  )
}
