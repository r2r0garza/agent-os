"use client"

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react"
import {
  Bot,
  CheckCircle2,
  GitCompareArrows,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  Workflow,
} from "lucide-react"

import {
  Agent,
  ApiError,
  GoalPlanningAcceptance,
  GoalPlanningSession,
  PlanningAssignment,
  PlanningCandidate,
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
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"

function evidenceSummary(value: Record<string, unknown>) {
  const entries = Object.entries(value)
  if (!entries.length) return "No additional constraints"
  return entries
    .slice(0, 4)
    .map(([key, detail]) => {
      const label = key.replaceAll("_", " ")
      if (Array.isArray(detail))
        return `${label}: ${detail.join(", ") || "none"}`
      if (detail && typeof detail === "object") return `${label}: recorded`
      return `${label}: ${String(detail)}`
    })
    .join(" · ")
}

function candidateReason(candidate: PlanningCandidate) {
  if (candidate.eligible) {
    return candidate.matched_capabilities.length
      ? `Matches ${candidate.matched_capabilities.join(", ")}`
      : "Eligible under current constraints"
  }
  if (candidate.rejection_reasons.length) {
    return candidate.rejection_reasons.join(" · ").replaceAll("_", " ")
  }
  if (candidate.missing_capabilities.length) {
    return `Missing ${candidate.missing_capabilities.join(", ")}`
  }
  return "Not eligible under current constraints"
}

function candidateName(candidate: PlanningCandidate, agents: Agent[]) {
  return (
    agents.find((agent) => agent.id === candidate.agent_id)?.name ??
    "Unknown agent"
  )
}

function taskName(assignment: PlanningAssignment, tasks: Task[]) {
  return (
    tasks.find((task) => task.id === assignment.assignment_key)?.title ??
    assignment.assignment_key
  )
}

interface GoalPlanningPanelProps {
  goalId: string
  tasks: Task[]
  agents: Agent[]
  onAccepted: () => Promise<void>
}

export function GoalPlanningPanel({
  goalId,
  tasks,
  agents,
  onAccepted,
}: GoalPlanningPanelProps) {
  const [sessions, setSessions] = useState<GoalPlanningSession[]>([])
  const [session, setSession] = useState<GoalPlanningSession | null>(null)
  const [loading, setLoading] = useState(false)
  const [mutation, setMutation] = useState("")
  const [error, setError] = useState("")
  const [unauthorized, setUnauthorized] = useState(false)
  const [notice, setNotice] = useState("")
  const [overrideCandidates, setOverrideCandidates] = useState<
    Record<string, string>
  >({})
  const [overrideReasons, setOverrideReasons] = useState<
    Record<string, string>
  >({})

  const loadSessions = useCallback(async () => {
    if (!goalId) {
      setSessions([])
      setSession(null)
      setUnauthorized(false)
      setError("")
      return
    }
    setSessions([])
    setSession(null)
    setLoading(true)
    setError("")
    setUnauthorized(false)
    try {
      const records = await api<GoalPlanningSession[]>(
        `/goals/${goalId}/planning-sessions`
      )
      setSessions(records)
      setSession(records.at(-1) ?? null)
    } catch (reason) {
      if (
        reason instanceof ApiError &&
        [401, 403, 404].includes(reason.status)
      ) {
        setUnauthorized(true)
      } else {
        setError(
          reason instanceof Error ? reason.message : "Unable to load planning"
        )
      }
    } finally {
      setLoading(false)
    }
  }, [goalId])

  useEffect(() => {
    // Planning sessions are external API state and must follow the selected goal.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadSessions()
  }, [loadSessions])

  const candidateById = useMemo(
    () =>
      new Map(
        (session?.candidates ?? []).map((candidate) => [
          candidate.id,
          candidate,
        ])
      ),
    [session]
  )

  const preview = async () => {
    if (!goalId) return
    setMutation("preview")
    setError("")
    setNotice("")
    try {
      const record = await api<GoalPlanningSession>(
        `/goals/${goalId}/planning-sessions`,
        jsonBody({})
      )
      setSessions((current) => [...current, record])
      setSession(record)
      setNotice(
        record.validation_status === "valid"
          ? "Planning preview is ready for review."
          : "Preview created, but one or more assignments still need an eligible agent."
      )
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to preview plan"
      )
    } finally {
      setMutation("")
    }
  }

  const applyOverride = async (
    event: FormEvent<HTMLFormElement>,
    assignment: PlanningAssignment
  ) => {
    event.preventDefault()
    if (!session) return
    const agentVersionId = overrideCandidates[assignment.id] ?? ""
    const reason = overrideReasons[assignment.id]?.trim() ?? ""
    if (!agentVersionId || !reason) {
      setError(
        "Choose a candidate and explain the override before applying it."
      )
      return
    }
    setMutation(`override:${assignment.id}`)
    setError("")
    setNotice("")
    try {
      const record = await api<GoalPlanningSession>(
        `/goals/${goalId}/planning-sessions/${session.id}/overrides`,
        jsonBody({
          assignment_key: assignment.assignment_key,
          agent_version_id: agentVersionId,
          reason,
        })
      )
      setSession(record)
      setSessions((current) =>
        current.map((item) => (item.id === record.id ? record : item))
      )
      setNotice(`Assignment for ${taskName(assignment, tasks)} was updated.`)
    } catch (reasonValue) {
      setError(
        reasonValue instanceof Error
          ? reasonValue.message
          : "The override could not be applied"
      )
      await loadSessions()
    } finally {
      setMutation("")
    }
  }

  const accept = async () => {
    if (!session) return
    setMutation("accept")
    setError("")
    setNotice("")
    try {
      const accepted = await api<GoalPlanningAcceptance>(
        `/goals/${goalId}/planning-sessions/${session.id}/accept`,
        jsonBody({})
      )
      setSession(accepted)
      setSessions((current) =>
        current.map((item) => (item.id === accepted.id ? accepted : item))
      )
      setNotice(
        `Plan accepted and task graph revision ${accepted.graph_revision_number ?? "recorded"} is scheduled.`
      )
      await onAccepted()
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to accept plan"
      )
    } finally {
      setMutation("")
    }
  }

  if (!goalId) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="size-4" /> Capability-aware planning
          </CardTitle>
          <CardDescription>
            Select a goal to form and review its proposed agent team.
          </CardDescription>
        </CardHeader>
      </Card>
    )
  }

  if (unauthorized) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldAlert className="size-4" /> Capability-aware planning
          </CardTitle>
          <CardDescription>
            You do not have access to planning records for this goal.
          </CardDescription>
        </CardHeader>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <Sparkles className="size-4" /> TEAM FORMATION
        </div>
        <CardTitle>Review the proposed team and task plan</CardTitle>
        <CardDescription>
          Compare capability and governance evidence, override eligible
          assignments, then materialize the accepted plan into scheduled work.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-5">
        {error ? (
          <div className="flex items-start justify-between gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
            <span>{error}</span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void loadSessions()}
            >
              <RefreshCw /> Retry
            </Button>
          </div>
        ) : null}
        {notice ? (
          <div className="flex items-center gap-2 rounded-xl border bg-emerald-500/5 p-3 text-sm">
            <CheckCircle2 className="size-4 text-emerald-600" /> {notice}
          </div>
        ) : null}

        {loading && !session ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <LoaderCircle className="size-4 animate-spin" /> Loading planning
            history…
          </div>
        ) : !session ? (
          <div className="grid gap-3 rounded-xl border border-dashed p-5">
            <p className="text-sm text-muted-foreground">
              {tasks.length
                ? "No planning preview exists yet. Agentic OS will derive requirements from the task graph and compare the latest governed agent versions."
                : "Decompose this goal into capability-bearing tasks before requesting a planning preview."}
            </p>
            <Button
              className="justify-self-start"
              onClick={() => void preview()}
              disabled={!tasks.length || mutation === "preview"}
            >
              {mutation === "preview" ? (
                <LoaderCircle className="animate-spin" />
              ) : (
                <GitCompareArrows />
              )}
              Preview team and plan
            </Button>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge
                  variant={
                    session.status === "accepted" ? "default" : "outline"
                  }
                >
                  {session.status}
                </Badge>
                <Badge
                  variant={
                    session.validation_status === "valid"
                      ? "secondary"
                      : "destructive"
                  }
                >
                  {session.validation_status}
                </Badge>
                <span className="text-xs text-muted-foreground">
                  Preview revision {session.revision_number} · {sessions.length}{" "}
                  planning record
                  {sessions.length === 1 ? "" : "s"}
                </span>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void preview()}
                disabled={
                  session.status === "accepted" || mutation === "preview"
                }
              >
                <RefreshCw
                  className={mutation === "preview" ? "animate-spin" : ""}
                />
                New preview
              </Button>
            </div>

            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-xl border p-4">
                <p className="mb-3 text-xs font-medium text-muted-foreground">
                  REQUIRED CAPABILITIES
                </p>
                <div className="flex flex-wrap gap-2">
                  {session.requirements.map((requirement) => (
                    <Badge
                      key={requirement.id}
                      variant="outline"
                      title={requirement.rationale ?? ""}
                    >
                      {requirement.capability_key}
                    </Badge>
                  ))}
                </div>
                <p className="mt-3 text-xs text-muted-foreground">
                  {evidenceSummary(session.constraints_snapshot)}
                </p>
              </div>
              <div className="rounded-xl border p-4">
                <p className="mb-3 text-xs font-medium text-muted-foreground">
                  CANDIDATE SUMMARY
                </p>
                <p className="text-2xl font-semibold">
                  {
                    session.candidates.filter((candidate) => candidate.eligible)
                      .length
                  }
                  <span className="ml-1 text-sm font-normal text-muted-foreground">
                    eligible of {session.candidates.length}
                  </span>
                </p>
                <p className="mt-2 text-xs text-muted-foreground">
                  Policy, budget, model, skill, and MCP evidence is redacted by
                  the API.
                </p>
              </div>
            </div>

            <div className="grid gap-3">
              <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                <Bot className="size-4" /> ELIGIBLE AND REJECTED CANDIDATES
              </p>
              {session.candidates.length > 0 &&
              !session.candidates.some((candidate) => candidate.eligible) ? (
                <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
                  No eligible agent satisfies the current capability and
                  governance constraints. Provision or repair an agent, then
                  create a new preview.
                </div>
              ) : null}
              {session.candidates.length ? (
                session.candidates.map((candidate) => (
                  <div
                    key={candidate.id}
                    className="grid gap-2 rounded-xl border p-4 md:grid-cols-[minmax(0,0.7fr)_minmax(0,1.3fr)]"
                  >
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="font-medium">
                          {candidateName(candidate, agents)}
                        </p>
                        <Badge
                          variant={
                            candidate.eligible ? "default" : "destructive"
                          }
                        >
                          {candidate.eligible ? "Eligible" : "Rejected"}
                        </Badge>
                      </div>
                      <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                        {candidate.agent_version_id}
                      </p>
                    </div>
                    <div className="text-xs text-muted-foreground">
                      <p>{candidateReason(candidate)}</p>
                      <p className="mt-1">
                        {evidenceSummary(candidate.constraints_snapshot)}
                      </p>
                    </div>
                  </div>
                ))
              ) : (
                <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
                  No candidate agent versions are available. Provision an agent
                  with the required capabilities, then create a new preview.
                </div>
              )}
            </div>

            <div className="grid gap-3">
              <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                <Workflow className="size-4" /> TASK ASSIGNMENTS AND OVERRIDES
              </p>
              {session.assignments.map((assignment) => {
                const selected = assignment.candidate_id
                  ? candidateById.get(assignment.candidate_id)
                  : undefined
                const eligibleCandidates = session.candidates.filter(
                  (candidate) => candidate.eligible
                )
                return (
                  <form
                    key={assignment.id}
                    className="grid gap-3 rounded-xl border p-4"
                    onSubmit={(event) => void applyOverride(event, assignment)}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <p className="font-medium">
                          {taskName(assignment, tasks)}
                        </p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {assignment.rationale ||
                            "No selection rationale recorded."}
                        </p>
                      </div>
                      <Badge
                        variant={
                          assignment.validation_status === "valid"
                            ? "secondary"
                            : "destructive"
                        }
                      >
                        {selected
                          ? candidateName(selected, agents)
                          : assignment.validation_status.replaceAll("_", " ")}
                      </Badge>
                    </div>
                    {session.status !== "accepted" ? (
                      <div className="grid gap-3 md:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)_auto] md:items-end">
                        <div className="grid gap-1.5">
                          <Label htmlFor={`candidate-${assignment.id}`}>
                            Override agent
                          </Label>
                          <select
                            id={`candidate-${assignment.id}`}
                            className="h-9 rounded-lg border bg-background px-3 text-sm"
                            value={overrideCandidates[assignment.id] ?? ""}
                            onChange={(event) =>
                              setOverrideCandidates((current) => ({
                                ...current,
                                [assignment.id]: event.target.value,
                              }))
                            }
                          >
                            <option value="">Choose eligible candidate</option>
                            {eligibleCandidates.map((candidate) => (
                              <option
                                key={candidate.id}
                                value={candidate.agent_version_id}
                              >
                                {candidateName(candidate, agents)}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="grid gap-1.5">
                          <Label htmlFor={`reason-${assignment.id}`}>
                            Override reason
                          </Label>
                          <Textarea
                            id={`reason-${assignment.id}`}
                            className="min-h-9"
                            placeholder="Why this eligible agent is preferred"
                            value={overrideReasons[assignment.id] ?? ""}
                            onChange={(event) =>
                              setOverrideReasons((current) => ({
                                ...current,
                                [assignment.id]: event.target.value,
                              }))
                            }
                          />
                        </div>
                        <Button
                          type="submit"
                          variant="outline"
                          disabled={mutation === `override:${assignment.id}`}
                        >
                          {mutation === `override:${assignment.id}` ? (
                            <LoaderCircle className="animate-spin" />
                          ) : (
                            <GitCompareArrows />
                          )}
                          Apply
                        </Button>
                      </div>
                    ) : null}
                  </form>
                )
              })}
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
              <p className="text-xs text-muted-foreground">
                {session.status === "accepted"
                  ? "Accepted assignments are pinned in the durable task-graph revision."
                  : session.validation_status === "valid"
                    ? "All assignments are valid and ready to materialize."
                    : "Resolve every assignment to an eligible candidate before acceptance."}
              </p>
              <Button
                onClick={() => void accept()}
                disabled={
                  session.status === "accepted" ||
                  session.validation_status !== "valid" ||
                  mutation === "accept"
                }
              >
                {mutation === "accept" ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <CheckCircle2 />
                )}
                {session.status === "accepted"
                  ? "Plan accepted"
                  : "Accept and schedule"}
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
