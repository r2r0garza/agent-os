"use client"

import { useState } from "react"
import {
  AlertTriangle,
  Bot,
  ChevronDown,
  ChevronUp,
  CircleDollarSign,
  LoaderCircle,
  ShieldAlert,
  ShieldCheck,
  Wrench,
} from "lucide-react"

import { Agent, AuditEvent, GovernanceEvidence, Run, api } from "@/lib/api"
import { GovernanceLookups } from "@/components/governance-workspace"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"

const ENFORCEMENT_EVENT_TYPES = new Set([
  "policy.decision",
  "policy.approval_required",
  "budget.exhausted",
  "budget.warning_threshold",
  "budget.override_applied",
  "approval.approved",
  "approval.denied",
  "approval.expired",
  "governance.admin_override_created",
  "workspace.promotion_denied",
  "workspace.promotion_conflict",
])
const EVIDENCE_EVENT_TYPES = new Set([
  ...ENFORCEMENT_EVENT_TYPES,
  "tool.invoked",
  "skill.invoked",
])
const HARNESS_EVENT_TYPES = new Set([
  "harness.capability_check_failed",
  "harness.invocation_started",
  "harness.invocation_completed",
  "harness.invocation_failed",
  "harness.output_recorded",
])

function HarnessEvidenceRow({ event }: { event: AuditEvent }) {
  const payload = event.payload
  switch (event.event_type) {
    case "harness.invocation_started":
      return (
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-1">
            <Bot className="size-3 text-muted-foreground" /> model call started ·{" "}
            {String(payload.model_identifier ?? "unknown model")}
          </span>
          <Badge variant="outline">{String(payload.endpoint ?? "endpoint redacted")}</Badge>
        </div>
      )
    case "harness.invocation_completed": {
      const usage = (payload.usage ?? {}) as Record<string, unknown>
      const usageText = Object.entries(usage)
        .map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`)
        .join(" · ")
      return (
        <div>
          <div className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-1">
              <ShieldCheck className="size-3 text-emerald-600" /> model call completed ·{" "}
              {String(payload.attempts ?? 1)} attempt(s) ·{" "}
              {String(payload.tool_rounds ?? 0)} tool round(s)
            </span>
            <Badge variant="outline">
              {String(payload.finish_reason ?? "finish reason unavailable")}
            </Badge>
          </div>
          {usageText ? (
            <p className="mt-0.5 text-muted-foreground">{usageText}</p>
          ) : null}
        </div>
      )
    }
    case "harness.invocation_failed":
      return (
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-1 text-destructive">
            <AlertTriangle className="size-3" /> model call failed ·{" "}
            {String(payload.diagnostic ?? "unknown failure")}
          </span>
          <Badge variant="destructive">
            {String(payload.attempts ?? 0)} attempt(s)
          </Badge>
        </div>
      )
    case "harness.capability_check_failed": {
      const failures = Array.isArray(payload.failures)
        ? (payload.failures as { capability: string; status: string; diagnostic: string }[])
        : []
      return (
        <div>
          <span className="flex items-center gap-1 text-destructive">
            <AlertTriangle className="size-3" /> blocked before invocation: missing required
            capability evidence
          </span>
          <div className="mt-1 flex flex-wrap gap-1">
            {failures.map((failure) => (
              <Badge
                key={failure.capability}
                variant="destructive"
                title={failure.diagnostic}
              >
                {failure.capability}: {failure.status}
              </Badge>
            ))}
          </div>
        </div>
      )
    }
    case "harness.output_recorded":
      return (
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-1">
            <Bot className="size-3 text-muted-foreground" /> model output recorded to run
          </span>
          <span className="text-muted-foreground">{displayDate(event.occurred_at)}</span>
        </div>
      )
    default:
      return null
  }
}

function displayDate(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}

interface RunEvidencePanelProps {
  run: Run
  agents: Agent[]
  lookups: GovernanceLookups
}

export function RunEvidencePanel({
  run,
  agents,
  lookups,
}: RunEvidencePanelProps) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")
  const [evidence, setEvidence] = useState<GovernanceEvidence | null>(null)

  async function toggle() {
    const next = !open
    setOpen(next)
    if (next && evidence === null) {
      setLoading(true)
      setError("")
      try {
        setEvidence(
          await api<GovernanceEvidence>(
            `/governance/evidence?run_id=${run.id}&limit=500`
          )
        )
      } catch (reason) {
        setError(
          reason instanceof Error
            ? reason.message
            : "Unable to load run evidence"
        )
      } finally {
        setLoading(false)
      }
    }
  }

  const snapshot = run.snapshot
  const agent = agents.find((entry) => entry.id === snapshot.agent_id)
  const evidenceEvents = (evidence?.audit_events ?? []).filter((event) =>
    EVIDENCE_EVENT_TYPES.has(event.event_type)
  )
  const harnessEvents = (evidence?.audit_events ?? []).filter((event) =>
    HARNESS_EVENT_TYPES.has(event.event_type)
  )
  const ledger = evidence?.cost_ledger_entries ?? []

  return (
    <div className="mt-2 border-t pt-2">
      <Button
        variant="ghost"
        size="sm"
        className="h-6 gap-1 px-1.5 text-[11px]"
        onClick={() => void toggle()}
      >
        {open ? (
          <ChevronUp className="size-3" />
        ) : (
          <ChevronDown className="size-3" />
        )}
        {open
          ? "Hide pinned snapshot & evidence"
          : "View pinned snapshot & evidence"}
      </Button>

      {open ? (
        loading ? (
          <div className="mt-2 flex items-center gap-2 text-[11px] text-muted-foreground">
            <LoaderCircle className="size-3 animate-spin" /> Loading evidence…
          </div>
        ) : error ? (
          <p className="mt-2 text-[11px] text-destructive">{error}</p>
        ) : (
          <div className="mt-2 grid gap-3 text-[11px]">
            <div>
              <p className="font-medium text-muted-foreground">
                Pinned configuration snapshot
              </p>
              <div className="mt-1 flex flex-wrap gap-1">
                {agent ? (
                  <Badge variant="outline">
                    {agent.name} · v{snapshot.agent_version_number ?? "?"}
                  </Badge>
                ) : null}
                {snapshot.model_profile_version_id ? (
                  <Badge variant="outline">
                    {lookups.modelProfileVersionName[
                      snapshot.model_profile_version_id
                    ] ??
                      `model version ${snapshot.model_profile_version_id.slice(0, 8)}`}
                  </Badge>
                ) : (
                  <Badge variant="secondary">no model profile pinned</Badge>
                )}
                {(snapshot.skill_version_ids ?? []).map((id) => (
                  <Badge key={id} variant="secondary">
                    {lookups.skillVersionName[id] ??
                      `skill version ${id.slice(0, 8)}`}
                  </Badge>
                ))}
                {(snapshot.mcp_server_version_ids ?? []).map((id) => (
                  <Badge key={id} variant="secondary">
                    {lookups.mcpVersionName[id] ??
                      `MCP version ${id.slice(0, 8)}`}
                  </Badge>
                ))}
                {(snapshot.enabled_tools ?? []).map((tool) => (
                  <Badge key={tool} variant="outline">
                    tool: {tool}
                  </Badge>
                ))}
                {snapshot.policy_decision ? (
                  <Badge
                    variant={
                      snapshot.policy_decision === "deny"
                        ? "destructive"
                        : "outline"
                    }
                  >
                    policy: {snapshot.policy_decision}
                  </Badge>
                ) : null}
              </div>
            </div>

            {snapshot.model_profile_version_id ? (
              <div>
                <p className="font-medium text-muted-foreground">
                  Model invocation evidence
                </p>
                <div className="mt-1 grid gap-1.5">
                  {harnessEvents.length ? (
                    harnessEvents.map((event) => (
                      <HarnessEvidenceRow key={event.id} event={event} />
                    ))
                  ) : (
                    <p className="text-muted-foreground">
                      No model invocation evidence recorded for this run yet.
                    </p>
                  )}
                </div>
              </div>
            ) : null}

            {evidence?.approval_requests.length ? (
              <div>
                <p className="font-medium text-muted-foreground">
                  Approval evidence
                </p>
                <div className="mt-1 grid gap-1">
                  {evidence.approval_requests.map((request) => (
                    <div
                      key={request.id}
                      className="flex items-center justify-between gap-2"
                    >
                      <span>{request.action_type.replaceAll("_", " ")}</span>
                      <Badge
                        variant={
                          request.status === "denied" ||
                          request.status === "expired"
                            ? "destructive"
                            : "outline"
                        }
                      >
                        {request.status}
                      </Badge>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {evidence?.budget_reservations.length ? (
              <div>
                <p className="font-medium text-muted-foreground">
                  Budget reservations
                </p>
                <div className="mt-1 grid gap-1">
                  {evidence.budget_reservations.map((reservation) => (
                    <div
                      key={reservation.id}
                      className="flex items-center justify-between gap-2"
                    >
                      <span>
                        {reservation.action_type.replaceAll("_", " ")} ·{" "}
                        {reservation.status}
                      </span>
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
                  ))}
                </div>
              </div>
            ) : null}

            {ledger.length ? (
              <div>
                <p className="font-medium text-muted-foreground">
                  Cost & budget evidence
                </p>
                <div className="mt-1 grid gap-1">
                  {ledger.map((entry) => (
                    <div
                      key={entry.id}
                      className="flex items-center justify-between gap-2"
                    >
                      <span className="flex items-center gap-1">
                        <CircleDollarSign className="size-3" />
                        {entry.action_type.replaceAll("_", " ")} ·{" "}
                        {entry.status}
                      </span>
                      <Badge variant="outline">
                        {entry.is_zero_cost
                          ? "zero cost"
                          : entry.is_unpriced
                            ? "unpriced"
                            : `${entry.actual_amount_minor_units ?? entry.reserved_amount_minor_units} ${entry.currency} minor`}
                      </Badge>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <p className="text-muted-foreground">
                No cost ledger entries recorded for this run yet.
              </p>
            )}

            <div>
              <p className="font-medium text-muted-foreground">
                Policy, budget & tool events
              </p>
              <div className="mt-1 grid gap-1">
                {evidenceEvents.length ? (
                  evidenceEvents.map((event) => {
                    const decision =
                      typeof event.payload.decision === "string"
                        ? event.payload.decision
                        : null
                    const isEnforcement = ENFORCEMENT_EVENT_TYPES.has(
                      event.event_type
                    )
                    const denied =
                      decision === "deny" ||
                      event.event_type === "budget.exhausted" ||
                      event.event_type === "workspace.promotion_denied" ||
                      event.event_type === "workspace.promotion_conflict"
                    const Icon = !isEnforcement
                      ? Wrench
                      : denied
                        ? ShieldAlert
                        : ShieldCheck
                    return (
                      <div
                        key={event.id}
                        className="flex items-center justify-between gap-2"
                      >
                        <span className="flex items-center gap-1">
                          <Icon
                            className={`size-3 ${
                              isEnforcement
                                ? denied
                                  ? "text-destructive"
                                  : "text-emerald-600"
                                : "text-muted-foreground"
                            }`}
                          />
                          {event.event_type}
                          {decision ? ` · ${decision}` : ""}
                        </span>
                        <span className="text-muted-foreground">
                          {displayDate(event.occurred_at)}
                        </span>
                      </div>
                    )
                  })
                ) : (
                  <p className="text-muted-foreground">
                    No policy, budget, or tool evidence recorded for this run
                    yet.
                  </p>
                )}
              </div>
            </div>
          </div>
        )
      ) : null}
    </div>
  )
}
