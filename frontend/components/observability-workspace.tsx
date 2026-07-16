"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Activity,
  AlertTriangle,
  ArchiveRestore,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleOff,
  Database,
  ExternalLink,
  FileCheck2,
  HardDrive,
  LoaderCircle,
  Radio,
  RefreshCw,
  ServerCog,
  Settings,
  ShieldCheck,
  Workflow,
} from "lucide-react"

import {
  ApiError,
  ObservabilityHealth,
  ObservabilityRecord,
  Run,
  TelemetryAttempt,
  api,
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

interface ObservabilityWorkspaceProps {
  goalId: string
  runs: Run[]
}

function displayDate(value: string | null) {
  if (!value) return "No evidence yet"
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}

function shortId(value: string | null) {
  return value ? value.slice(0, 8) : "—"
}

function statusVariant(status: string | null) {
  if (["failed", "dropped", "unavailable", "stale"].includes(status ?? ""))
    return "destructive" as const
  if (["delayed", "degraded", "pending"].includes(status ?? ""))
    return "secondary" as const
  return "outline" as const
}

function EvidenceLink({
  label,
  value,
}: {
  label: string
  value: string | null
}) {
  if (!value) return null
  return (
    <Badge variant="outline" title={value}>
      {label} {shortId(value)}
    </Badge>
  )
}

function DeliveryState({ attempt }: { attempt: TelemetryAttempt }) {
  return (
    <div className="rounded-lg border bg-background p-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={statusVariant(attempt.status)}>{attempt.status}</Badge>
        <span className="font-medium">{attempt.destination}</span>
        <span className="text-muted-foreground">
          attempt {attempt.attempt_number}
        </span>
      </div>
      {attempt.failure_code ? (
        <p className="mt-1 text-destructive">
          {attempt.failure_code}:{" "}
          {attempt.failure_message ?? "failure details redacted"}
        </p>
      ) : null}
      {attempt.retry_after ? (
        <p className="mt-1 text-muted-foreground">
          Retry scheduled {displayDate(attempt.retry_after)}
        </p>
      ) : null}
    </div>
  )
}

function TimelineRecord({
  record,
  selected,
  loading,
  onSelect,
}: {
  record: ObservabilityRecord
  selected: ObservabilityRecord | null
  loading: boolean
  onSelect: () => void
}) {
  const open = selected?.id === record.id
  const evidenceCount = [
    record.audit_event_id,
    record.cost_ledger_entry_id,
    record.approval_request_id,
    record.approval_decision_id,
    record.artifact_id,
    record.artifact_version_id,
    record.model_call_id,
    record.tool_call_id,
    record.mcp_call_id,
    record.sandbox_id,
    record.checkpoint_id,
  ].filter(Boolean).length

  return (
    <li className="relative pl-7 before:absolute before:top-2 before:bottom-[-1.25rem] before:left-[7px] before:w-px before:bg-border last:before:hidden">
      <span className="absolute top-1.5 left-0 size-3.5 rounded-full border-2 border-background bg-foreground" />
      <div className="rounded-xl border bg-background p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <p className="font-medium">{record.operation_name}</p>
              <Badge variant="outline">
                {record.event_kind.replaceAll("_", " ")}
              </Badge>
              {record.status ? (
                <Badge variant={statusVariant(record.status)}>
                  {record.status}
                </Badge>
              ) : null}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {displayDate(record.occurred_at)} · correlation{" "}
              {shortId(record.correlation_id)}
              {record.run_id ? ` · run ${shortId(record.run_id)}` : ""}
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={onSelect}
            disabled={loading}
          >
            {loading ? (
              <LoaderCircle className="animate-spin" />
            ) : open ? (
              <ChevronUp />
            ) : (
              <ChevronDown />
            )}
            {open ? "Hide details" : "Evidence details"}
          </Button>
        </div>

        <div className="mt-2 flex flex-wrap gap-1">
          {record.trace_id ? (
            <Badge
              variant="link"
              title={`span ${record.span_id ?? "unavailable"}`}
            >
              <ExternalLink /> trace {shortId(record.trace_id)}
            </Badge>
          ) : (
            <Badge variant="secondary">external trace unavailable</Badge>
          )}
          {evidenceCount ? (
            <Badge variant="outline">
              {evidenceCount} canonical evidence link
              {evidenceCount === 1 ? "" : "s"}
            </Badge>
          ) : (
            <Badge variant="secondary">canonical event only</Badge>
          )}
          {record.telemetry_attempts.map((attempt) => (
            <Badge key={attempt.id} variant={statusVariant(attempt.status)}>
              telemetry {attempt.status}
            </Badge>
          ))}
        </div>

        {open && selected ? (
          <div className="mt-3 grid gap-3 border-t pt-3 text-xs">
            <div className="grid gap-1 rounded-lg bg-muted/40 p-2 font-mono text-[11px] text-muted-foreground sm:grid-cols-2">
              <span>correlation {selected.correlation_id}</span>
              <span>request {selected.request_id ?? "unavailable"}</span>
              <span>trace {selected.trace_id ?? "unavailable"}</span>
              <span>span {selected.span_id ?? "unavailable"}</span>
            </div>
            <div className="flex flex-wrap gap-1">
              <EvidenceLink label="audit" value={selected.audit_event_id} />
              <EvidenceLink
                label="cost"
                value={selected.cost_ledger_entry_id}
              />
              <EvidenceLink
                label="approval"
                value={selected.approval_request_id}
              />
              <EvidenceLink
                label="decision"
                value={selected.approval_decision_id}
              />
              <EvidenceLink label="artifact" value={selected.artifact_id} />
              <EvidenceLink
                label="version"
                value={selected.artifact_version_id}
              />
              <EvidenceLink label="model" value={selected.model_call_id} />
              <EvidenceLink label="tool" value={selected.tool_call_id} />
              <EvidenceLink label="MCP" value={selected.mcp_call_id} />
              <EvidenceLink label="sandbox" value={selected.sandbox_id} />
              <EvidenceLink label="checkpoint" value={selected.checkpoint_id} />
            </div>
            <div className="grid gap-2 sm:grid-cols-3">
              <div className="rounded-lg bg-muted/40 p-2">
                <p className="font-medium">Event attributes</p>
                <pre className="mt-1 overflow-auto text-[11px] whitespace-pre-wrap text-muted-foreground">
                  {JSON.stringify(selected.attributes, null, 2)}
                </pre>
              </div>
              <div className="rounded-lg bg-muted/40 p-2">
                <p className="font-medium">Capture policy</p>
                <pre className="mt-1 overflow-auto text-[11px] whitespace-pre-wrap text-muted-foreground">
                  {JSON.stringify(selected.capture_policy_evidence, null, 2)}
                </pre>
              </div>
              <div className="rounded-lg bg-muted/40 p-2">
                <p className="font-medium">Redaction evidence</p>
                <pre className="mt-1 overflow-auto text-[11px] whitespace-pre-wrap text-muted-foreground">
                  {JSON.stringify(selected.redaction_evidence, null, 2)}
                </pre>
              </div>
            </div>
            {selected.telemetry_attempts.length ? (
              <div className="grid gap-2">
                {selected.telemetry_attempts.map((attempt) => (
                  <DeliveryState key={attempt.id} attempt={attempt} />
                ))}
              </div>
            ) : (
              <p className="rounded-lg border border-dashed p-2 text-muted-foreground">
                No external delivery was attempted. This canonical Agentic OS
                record remains available independently of telemetry export.
              </p>
            )}
          </div>
        ) : null}
      </div>
    </li>
  )
}

function HealthTile({
  label,
  status,
  detail,
  icon,
}: {
  label: string
  status: string
  detail: string
  icon: React.ReactNode
}) {
  return (
    <div className="rounded-xl border bg-background p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          {icon} {label}
        </span>
        <Badge variant={statusVariant(status)}>{status}</Badge>
      </div>
      <p className="mt-2 text-sm font-medium">{detail}</p>
    </div>
  )
}

export function ObservabilityWorkspace({
  goalId,
  runs,
}: ObservabilityWorkspaceProps) {
  const [selectedRunId, setSelectedRunId] = useState("")
  const [records, setRecords] = useState<ObservabilityRecord[]>([])
  const [detail, setDetail] = useState<ObservabilityRecord | null>(null)
  const [detailLoadingId, setDetailLoadingId] = useState("")
  const [timelineLoading, setTimelineLoading] = useState(false)
  const [timelineError, setTimelineError] = useState("")
  const [health, setHealth] = useState<ObservabilityHealth | null>(null)
  const [healthLoading, setHealthLoading] = useState(false)
  const [healthError, setHealthError] = useState("")
  const [adminDenied, setAdminDenied] = useState(false)
  const effectiveRunId = runs.some((run) => run.id === selectedRunId)
    ? selectedRunId
    : ""

  const loadTimeline = useCallback(async () => {
    if (!goalId) {
      setRecords([])
      setDetail(null)
      return
    }
    setTimelineLoading(true)
    setTimelineError("")
    try {
      const path = effectiveRunId
        ? `/runs/${effectiveRunId}/observability-timeline?limit=500`
        : `/goals/${goalId}/observability-timeline?limit=500`
      setRecords(await api<ObservabilityRecord[]>(path))
    } catch (reason) {
      setTimelineError(
        reason instanceof Error
          ? reason.message
          : "Unable to load correlated timeline"
      )
    } finally {
      setTimelineLoading(false)
    }
  }, [effectiveRunId, goalId])

  const loadHealth = useCallback(async () => {
    setHealthLoading(true)
    setHealthError("")
    try {
      setHealth(await api<ObservabilityHealth>("/admin/observability/health"))
      setAdminDenied(false)
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 403) {
        setAdminDenied(true)
        setHealth(null)
      } else {
        setHealthError(
          reason instanceof Error
            ? reason.message
            : "Unable to load system health"
        )
      }
    } finally {
      setHealthLoading(false)
    }
  }, [])

  const refresh = useCallback(async () => {
    await Promise.all([loadTimeline(), loadHealth()])
  }, [loadHealth, loadTimeline])

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0)
    const poll = window.setInterval(() => void refresh(), 10_000)
    return () => {
      window.clearTimeout(initial)
      window.clearInterval(poll)
    }
  }, [refresh])

  async function selectRecord(record: ObservabilityRecord) {
    if (detail?.id === record.id) {
      setDetail(null)
      return
    }
    setDetailLoadingId(record.id)
    setTimelineError("")
    try {
      setDetail(
        await api<ObservabilityRecord>(`/observability-records/${record.id}`)
      )
    } catch (reason) {
      setTimelineError(
        reason instanceof Error
          ? reason.message
          : "Unable to load evidence detail"
      )
    } finally {
      setDetailLoadingId("")
    }
  }

  const failedDeliveries = useMemo(
    () =>
      records
        .flatMap((record) => record.telemetry_attempts)
        .filter((attempt) =>
          ["failed", "dropped", "delayed"].includes(attempt.status)
        ).length,
    [records]
  )
  const recoveryEvidence = useMemo(() => {
    const checkpointRecords = records.filter((record) => record.checkpoint_id)
    const durableRuns = runs.filter((run) => run.langgraph_thread_id)
    return {
      checkpointCount: checkpointRecords.length,
      durableRunCount: durableRuns.length,
      latestRecordAt: records[0]?.occurred_at ?? null,
    }
  }, [records, runs])

  return (
    <section className="mb-6 grid gap-6">
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <Workflow className="size-4" /> CORRELATED EXECUTION EVIDENCE
          </div>
          <CardTitle>Goal and run timeline</CardTitle>
          <CardDescription>
            Canonical Agentic OS evidence is shown independently from optional
            external trace delivery. Refreshed every 10 seconds.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <label className="grid min-w-64 flex-1 gap-1.5 text-sm font-medium">
              Timeline scope
              <select
                className="h-9 rounded-lg border bg-background px-3 text-sm"
                value={effectiveRunId}
                onChange={(event) => {
                  setSelectedRunId(event.target.value)
                  setDetail(null)
                }}
                disabled={!goalId}
              >
                <option value="">Entire selected goal</option>
                {runs.map((run) => (
                  <option key={run.id} value={run.id}>
                    Run {shortId(run.id)} · attempt {run.attempt_number} ·{" "}
                    {run.status}
                  </option>
                ))}
              </select>
            </label>
            <Button
              variant="outline"
              onClick={() => void refresh()}
              disabled={timelineLoading}
            >
              <RefreshCw className={timelineLoading ? "animate-spin" : ""} />{" "}
              Refresh evidence
            </Button>
          </div>

          {!goalId ? (
            <div className="rounded-xl border border-dashed p-7 text-center text-sm text-muted-foreground">
              Select a goal to inspect its persisted correlated timeline.
            </div>
          ) : timelineLoading && records.length === 0 ? (
            <div className="flex items-center justify-center gap-2 rounded-xl border border-dashed p-7 text-sm text-muted-foreground">
              <LoaderCircle className="animate-spin" /> Loading canonical
              evidence…
            </div>
          ) : timelineError ? (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>Timeline unavailable</AlertTitle>
              <AlertDescription>{timelineError}</AlertDescription>
            </Alert>
          ) : records.length === 0 ? (
            <div className="rounded-xl border border-dashed p-7 text-center text-sm text-muted-foreground">
              No canonical observability records exist for this scope yet. Run
              or resume work, then refresh.
            </div>
          ) : (
            <>
              {failedDeliveries ? (
                <Alert>
                  <AlertTriangle />
                  <AlertTitle>
                    External telemetry is delayed or degraded
                  </AlertTitle>
                  <AlertDescription>
                    {failedDeliveries} delivery attempt
                    {failedDeliveries === 1 ? "" : "s"} need attention. The{" "}
                    {records.length} canonical record
                    {records.length === 1 ? " remains" : "s remain"} available
                    below.
                  </AlertDescription>
                </Alert>
              ) : null}
              <ol className="grid gap-5">
                {records.map((record) => (
                  <TimelineRecord
                    key={record.id}
                    record={record}
                    selected={detail}
                    loading={detailLoadingId === record.id}
                    onSelect={() => void selectRecord(record)}
                  />
                ))}
              </ol>
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <ArchiveRestore className="size-4" /> OPERATOR RECOVERY EVIDENCE
          </div>
          <CardTitle>Durable work and restart readiness</CardTitle>
          <CardDescription>
            Goal-scoped evidence available to permitted operators. Deployment
            configuration remains restricted to administrators.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!goalId ? (
            <div className="rounded-xl border border-dashed p-7 text-center text-sm text-muted-foreground">
              Select a goal to inspect its persisted recovery evidence.
            </div>
          ) : timelineLoading && records.length === 0 ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <LoaderCircle className="animate-spin" /> Loading recovery
              evidence…
            </div>
          ) : timelineError && records.length === 0 ? (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>Recovery evidence unavailable</AlertTitle>
              <AlertDescription>{timelineError}</AlertDescription>
            </Alert>
          ) : records.length === 0 ? (
            <div className="rounded-xl border border-dashed p-7 text-center text-sm text-muted-foreground">
              No durable execution evidence exists yet. Start or resume the
              selected goal, then refresh.
            </div>
          ) : (
            <div className="grid gap-3 sm:grid-cols-3">
              <HealthTile
                label="Canonical records"
                status="available"
                detail={`${records.length} persisted timeline record${records.length === 1 ? "" : "s"}`}
                icon={<HardDrive className="size-4" />}
              />
              <HealthTile
                label="Checkpoint links"
                status={
                  recoveryEvidence.checkpointCount ? "available" : "pending"
                }
                detail={`${recoveryEvidence.checkpointCount} resumable checkpoint reference${recoveryEvidence.checkpointCount === 1 ? "" : "s"}`}
                icon={<FileCheck2 className="size-4" />}
              />
              <HealthTile
                label="Durable run threads"
                status={
                  recoveryEvidence.durableRunCount ? "available" : "pending"
                }
                detail={`${recoveryEvidence.durableRunCount} run thread${recoveryEvidence.durableRunCount === 1 ? "" : "s"} · latest ${displayDate(recoveryEvidence.latestRecordAt)}`}
                icon={<Workflow className="size-4" />}
              />
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <ServerCog className="size-4" /> ADMIN OBSERVABILITY HEALTH
          </div>
          <CardTitle>Delivery and system health</CardTitle>
          <CardDescription>
            Installation-wide queues, workers, database, sandbox, event stream,
            exporters, and capture policy from the admin API.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          {adminDenied ? (
            <Alert>
              <ShieldCheck />
              <AlertTitle>Admin role required</AlertTitle>
              <AlertDescription>
                Operator timeline evidence remains available for permitted
                projects. System-wide health and exporter configuration are
                intentionally restricted.
              </AlertDescription>
            </Alert>
          ) : healthLoading && !health ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <LoaderCircle className="animate-spin" /> Loading installation
              health…
            </div>
          ) : healthError ? (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>Health API unavailable</AlertTitle>
              <AlertDescription>{healthError}</AlertDescription>
            </Alert>
          ) : health ? (
            <>
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border p-3">
                <div className="flex items-center gap-2">
                  {health.status === "healthy" ? (
                    <CheckCircle2 className="size-5 text-emerald-600" />
                  ) : (
                    <AlertTriangle className="size-5 text-amber-600" />
                  )}
                  <div>
                    <p className="font-medium">Installation {health.status}</p>
                    <p className="text-xs text-muted-foreground">
                      Checked {displayDate(health.checked_at)}
                    </p>
                  </div>
                </div>
                <Badge variant={statusVariant(health.status)}>
                  {health.status}
                </Badge>
              </div>

              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {Object.entries(health.deployment.checks).map(
                  ([name, check]) => (
                    <HealthTile
                      key={name}
                      label={name.replaceAll("_", " ")}
                      status={check.status}
                      detail={check.detail}
                      icon={<Settings className="size-4" />}
                    />
                  )
                )}
                <HealthTile
                  label="Database"
                  status={health.database.status}
                  detail={`${health.database.latency_ms.toFixed(1)} ms probe`}
                  icon={<Database className="size-4" />}
                />
                <HealthTile
                  label="Queues"
                  status={health.queues.status}
                  detail={`${health.queues.depth} ready or pending`}
                  icon={<Workflow className="size-4" />}
                />
                <HealthTile
                  label="Workers"
                  status={health.workers.status}
                  detail={`${health.workers.active} active · ${health.workers.stale} stale · ${health.workers.retry_count} retries`}
                  icon={<Activity className="size-4" />}
                />
                <HealthTile
                  label="Sandbox"
                  status={health.sandbox.status}
                  detail={Object.entries(health.sandbox.runtimes)
                    .map(([runtime, state]) => `${runtime} ${state.status}`)
                    .join(" · ")}
                  icon={<ServerCog className="size-4" />}
                />
                <HealthTile
                  label="Event stream"
                  status={health.event_stream.status}
                  detail={
                    health.event_stream.latest_record_at
                      ? `Latest ${displayDate(health.event_stream.latest_record_at)}`
                      : "No canonical events recorded"
                  }
                  icon={<Radio className="size-4" />}
                />
                <HealthTile
                  label="Telemetry"
                  status={health.telemetry.status}
                  detail={`${health.telemetry.exporters.length} exporter${health.telemetry.exporters.length === 1 ? "" : "s"} configured`}
                  icon={<ExternalLink className="size-4" />}
                />
              </div>

              <div className="grid gap-3 lg:grid-cols-2">
                <div className="rounded-xl border bg-background p-3">
                  <div className="mb-3 flex items-center gap-2 text-sm font-medium">
                    <ArchiveRestore className="size-4" /> Backup, restore, and
                    upgrade commands
                  </div>
                  <div className="grid gap-2 font-mono text-xs text-muted-foreground">
                    {Object.entries(health.maintenance.commands).map(
                      ([name, command]) => (
                        <div key={name} className="rounded-lg bg-muted/40 p-2">
                          <span className="font-sans font-medium text-foreground">
                            {name.replaceAll("_", " ")}
                          </span>
                          <p className="mt-1 break-all">{command}</p>
                        </div>
                      )
                    )}
                  </div>
                </div>
                <div className="rounded-xl border bg-background p-3">
                  <div className="mb-3 flex items-center gap-2 text-sm font-medium">
                    <FileCheck2 className="size-4" /> Latest maintenance
                    evidence
                  </div>
                  {health.maintenance.events.length ? (
                    <div className="grid gap-2">
                      {health.maintenance.events.map((event) => (
                        <div
                          key={event.id}
                          className="rounded-lg bg-muted/40 p-2 text-xs"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <span className="font-medium">
                              {event.event_type
                                .replace("operations.", "")
                                .replaceAll("_", " ")}
                            </span>
                            <span className="text-muted-foreground">
                              {displayDate(event.occurred_at)}
                            </span>
                          </div>
                          <p className="mt-1 line-clamp-2 font-mono text-[11px] break-all text-muted-foreground">
                            {JSON.stringify(event.evidence)}
                          </p>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="rounded-lg border border-dashed p-5 text-center text-sm text-muted-foreground">
                      No maintenance command evidence has been recorded yet.
                    </div>
                  )}
                </div>
              </div>

              {health.telemetry.exporters.length === 0 ? (
                <Alert>
                  <CircleOff />
                  <AlertTitle>External telemetry disabled</AlertTitle>
                  <AlertDescription>
                    No exporter is configured. Canonical Agentic OS timelines,
                    audit evidence, cost records, and health remain available.
                  </AlertDescription>
                </Alert>
              ) : (
                <div className="grid gap-2">
                  {health.telemetry.exporters.map((exporter) => (
                    <div
                      key={exporter.id}
                      className="rounded-xl border bg-background p-3 text-sm"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <span className="font-medium">
                            {exporter.exporter_type}
                          </span>
                          <Badge
                            variant={exporter.enabled ? "outline" : "secondary"}
                          >
                            {exporter.enabled ? "enabled" : "disabled"}
                          </Badge>
                          <Badge
                            variant={
                              exporter.configured ? "outline" : "destructive"
                            }
                          >
                            {exporter.configured
                              ? "configured"
                              : "endpoint missing"}
                          </Badge>
                        </div>
                        <span className="text-xs text-muted-foreground">
                          prompts{" "}
                          {exporter.capture_prompts
                            ? "captured"
                            : "not captured"}{" "}
                          · outputs{" "}
                          {exporter.capture_outputs
                            ? "captured"
                            : "not captured"}
                        </span>
                      </div>
                      <p className="mt-2 text-xs text-muted-foreground">
                        Redaction policy:{" "}
                        {JSON.stringify(exporter.redaction_policy_evidence)}
                      </p>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : null}
        </CardContent>
      </Card>
    </section>
  )
}
