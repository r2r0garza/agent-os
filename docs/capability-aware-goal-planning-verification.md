# Capability-aware goal planning verification

This runbook consolidates Sprint 14 verification: durable capability-aware
planning evidence, versioned goal-planning preview/accept/override/task-graph
APIs, orchestrator team selection from explicit capability/grant/policy/budget
metadata, task-DAG and run-snapshot fail-closed enforcement, the frontend
goal-planning workflow, and restart recovery of accepted plans. Each section
is tied back to a numbered Sprint 14 exit criterion.

## Automated verification

Start PostgreSQL 16 and use the repository-local backend environment:

```bash
docker run -d --name agentic-os-verify-pg \
  -e POSTGRES_USER=agentic_os -e POSTGRES_PASSWORD=agentic_os \
  -e POSTGRES_DB=agentic_os -p 5432:5432 postgres:16

cd backend
source .venv/bin/activate
export AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os
export AGENTIC_OS_MASTER_KEY=$(python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
alembic upgrade head

PYTHONPATH=src:tests python -m pytest -q \
  tests/test_goal_planning_persistence.py \
  tests/test_goal_planning_api.py \
  tests/test_domain_migrations.py

PYTHONPATH=src:tests python -m pytest -q \
  tests/test_worker.py \
  tests/test_restart_recovery.py

PYTHONPATH=src:tests python -m pytest -q tests/test_redaction.py

PYTHONPATH=src:tests python -m pytest -q tests/test_api.py
```

Run each group as a separate `pytest` invocation as shown above. Running all
backend test modules in one process against a long-lived PostgreSQL
connection can trip `psycopg.errors.FeatureNotSupported: cached plan must not
change result type` on unrelated, already-passing tests (a prepared-statement
cache artifact of running many DDL-touching suites in one session); grouping
avoids it and matches the pattern in
[docs/governed-capability-lifecycle-verification.md](governed-capability-lifecycle-verification.md).

The suite proves, per exit criterion:

1. **Durable, queryable planning evidence**
   (`tests/test_goal_planning_persistence.py`) —
   `test_planning_records_persist_queryable_redacted_evidence_and_audit`
   shows a planning session persists derived capability requirements,
   candidate evidence (eligible and rejected, with rejection reasons),
   selection decisions, and policy/budget/model/tool constraint evidence as
   durable rows queryable independent of the goal, and that the persisted
   record is redacted of credential values.
   `test_valid_override_preserves_prior_evidence_and_updates_assignment`
   shows an operator override is recorded as new evidence alongside — not
   overwriting — the prior automatic selection.
   `test_invalid_cross_team_candidate_rolls_back_entire_planning_record`
   proves a planning session that would reference a cross-team candidate
   fails closed and leaves no partial record.
2. **Versioned preview/accept/override/task-graph APIs**
   (`tests/test_goal_planning_api.py`, `src/agentic_os/api/routers/goal_planning.py`) —
   `POST /api/v1/goals/{id}/planning-sessions` (preview),
   `POST .../planning-sessions/{id}/overrides`, and
   `POST .../planning-sessions/{id}/accept` (task-graph materialization) are
   exercised end to end.
   `test_preview_rejects_unauthorized_actor` and
   `test_preview_rejects_unknown_agent_version` prove backend-enforced
   team/project access and input validation ahead of any selection logic.
   `test_preview_computes_eligibility_and_persists_evidence`,
   `test_preview_derives_requirements_discovers_candidates_and_assigns`, and
   `test_preview_forms_team_across_task_specific_capabilities` prove the
   preview endpoint derives per-task capability requirements and forms a
   multi-agent team from real candidate evidence rather than a single
   goal-wide guess.
   `test_preview_rejects_assignment_to_ineligible_candidate` and
   `test_override_rejects_ineligible_candidate_but_persists_audit_trail`
   prove an override to an ineligible candidate is rejected with HTTP 422
   and rejection reasons while still recording an audit trail.
   `test_override_replaces_assignment_when_eligible` and
   `test_accept_materializes_task_assignment_and_is_idempotent` prove an
   eligible override is honored and that accepting the same plan twice does
   not double-materialize task assignments.
   `test_accept_rejects_unresolved_assignment` proves acceptance fails
   closed if any required capability track still lacks a resolved
   assignment.
3. **Capability/grant/policy/budget-aware team selection**
   (`tests/test_goal_planning_api.py::test_preview_uses_enabled_skill_capabilities`,
   `test_preview_requires_healthy_enabled_mcp_and_compatible_model`,
   `test_preview_rejects_policy_denial_and_exhausted_default_budget`,
   `test_preview_evaluates_budget_and_tool_constraints`,
   `src/agentic_os/domain/team_selection.py`) — selection only considers
   agent versions whose capability manifest, granted skill resources,
   enabled/healthy MCP tool grants, and compatible model profile actually
   satisfy a task's derived capability requirement; a candidate that is
   otherwise capable but denied by policy or has an exhausted default
   budget is recorded as an evidenced rejection rather than silently
   dropped or silently selected.
4. **Fail-closed task DAG / run-snapshot enforcement**
   (`tests/test_worker.py::test_run_snapshot_preserves_planning_identifiers`,
   `test_planning_assignment_drift_fails_closed_before_execution`,
   `test_planning_assignment_invalidated_fails_closed_before_execution`) —
   a completed run's snapshot and its `run.started` audit event carry the
   originating `planning_session_id` and `planning_assignment_id`. If the
   live planning assignment is later overridden to point at a different
   agent version than the one pinned at materialization time, the worker
   raises before dispatch (`TaskExecutionError: ... no longer selects ...`),
   never calls `invoke_tool`, marks the task `failed` with reason code
   `planning_assignment_drifted`, and records a `task.failed` audit event.
   The same fail-closed path applies when the planning assignment is marked
   `invalid` (`planning_assignment_invalidated`), covering revoked
   selections as well as drifted ones.
5. **Frontend goal-planning workflow**
   (`frontend/components/goal-planning-panel.test.tsx`) — "previews
   candidates, applies an eligible override, and accepts the task graph"
   exercises the full submit-goal → preview → compare-candidates →
   override → accept → inspect-task-graph flow against the real API client
   surface (`frontend/lib/api.ts`). "shows no-eligible-agent evidence and
   keeps acceptance disabled" proves the panel surfaces rejection evidence
   and blocks acceptance when a required capability has no eligible
   candidate. "renders access denial without exposing planning controls"
   and "shows a retry action when planning history is temporarily
   unavailable" cover the unauthorized and transient-error states an
   operator can hit. `operator-workspace.tsx` wires the panel into the
   authenticated operator console.
6. **Verification** — this document, plus the commands above and the manual
   smoke walkthrough below, tie automated evidence to every Sprint 14 exit
   criterion.

Run frontend and repository checks separately:

```bash
cd frontend
pnpm lint
pnpm typecheck
pnpm test

cd ..
PATH=/opt/homebrew/bin:$PATH ./agentic-os index check
git diff --check
```

## Manual smoke walkthrough

1. Start PostgreSQL, apply `alembic upgrade head`, then start FastAPI and the
   frontend using the root [README.md](../README.md). Set
   `AGENTIC_OS_MASTER_KEY` for a durable credential key.
2. Configure at least two agent versions with distinct capability manifests,
   granted skill resources, granted MCP tools, and model profiles, per
   [docs/governed-capability-lifecycle-verification.md](governed-capability-lifecycle-verification.md).
   Leave one capability track with no eligible agent to observe rejection
   evidence later.
3. In the operator console's goal-planning panel, submit a goal and preview
   a plan. Confirm the panel shows, per required capability: the derived
   requirement, eligible candidates with their evidence, and rejected
   candidates with rejection reasons (including the track with no eligible
   agent).
4. Apply an override to one task's assignment, choosing a different eligible
   candidate. Confirm the override is accepted and the prior automatic
   selection remains visible as evidence rather than being erased.
5. Attempt to override a task to a candidate that fails a policy or budget
   check. Confirm the API returns HTTP 422 with rejection reasons and the
   panel surfaces the denial without silently falling back.
6. Accept the plan. Confirm the resulting task DAG shows the pinned agent
   assignment, required capabilities, and selection rationale for each task,
   and that re-accepting the same planning session does not create duplicate
   task assignments.
7. Run the scheduled tasks: `python -m agentic_os worker run-once
   --worker-id demo-worker-1`. Inspect each task's run evidence panel and
   confirm the pinned snapshot carries the originating planning session and
   assignment identifiers.
8. Revoke or degrade a capability backing an already-accepted assignment
   (disable the granted MCP tool, revoke the skill grant, or override the
   planning assignment to a different candidate after acceptance) and run a
   new attempt against the same task. Confirm the worker fails closed before
   any tool dispatch, with a `task.failed` audit event carrying
   `planning_assignment_drifted` or `planning_assignment_invalidated` (or
   the pre-existing `tool_disabled` / `mcp_health_degraded` / `policy_denied`
   reason codes from
   [docs/governed-capability-lifecycle-verification.md](governed-capability-lifecycle-verification.md)
   when the drift is at the grant/policy layer instead of the planning
   layer).
9. Confirm restart recovery of an accepted plan: start a run for a
   planning-assigned task, pause it mid-flight, kill the worker, and restart
   it, per
   [docs/restart-recovery-verification.md](restart-recovery-verification.md).
   Confirm the recovered attempt's snapshot still carries the same
   `planning_session_id` and `planning_assignment_id` as the interrupted
   attempt (per `test_run_snapshot_preserves_planning_identifiers`), rather
   than re-resolving the assignment from scratch.

## Operator-facing guidance

- **Capability metadata is the only selection authority.** Team selection
  considers only explicit agent capability manifests, granted skill
  resources, enabled/healthy granted MCP tools, compatible model profiles,
  and policy/budget metadata — never free-text or embedding-based guessing.
  A candidate that is not backed by this explicit evidence is never
  eligible, regardless of how well its name or description matches the
  goal.
- **Override semantics.** An override does not replace history: the original
  automatic selection and its rejection/eligibility evidence remain
  queryable alongside the override decision. Overrides are still bound by
  eligibility — an operator cannot override a task to a candidate that
  fails a capability, policy, or budget check; that attempt is rejected with
  explicit rejection reasons, not silently coerced.
- **Fail-closed after acceptance.** Accepting a plan pins each task's agent
  assignment and required capabilities into the task DAG, but the worker
  re-verifies that pinned assignment immediately before every dispatch. If
  the live planning assignment has drifted (overridden to a different
  candidate) or been invalidated since acceptance, the task fails closed
  with `planning_assignment_drifted` / `planning_assignment_invalidated`
  before any tool call — the same fail-closed posture as a revoked MCP grant
  or a denying policy.
- **Redaction.** Persisted planning evidence and run snapshots never contain
  credential values; only credential presence/type metadata is preserved. If
  a planning record, override audit event, or run snapshot ever renders a
  credential value, treat it as a release blocker and rotate the credential
  immediately.

## Interpreting failures

- **Running the full backend test suite in one `pytest` invocation reports a
  `cached plan must not change result type` failure on an unrelated,
  otherwise-passing test:** this is a prepared-statement cache artifact of
  running many DDL-touching modules against one long-lived PostgreSQL
  session, not a Sprint 14 regression. Run the grouped invocations shown
  above (or run the single failing test module in isolation) to confirm.
- **A planning session's evidence disappears after an override:** the
  append-only evidence model in
  `backend/src/agentic_os/domain/planning.py` regressed; overrides must add
  a new decision record, never delete or overwrite prior candidate/rejection
  evidence.
- **A task runs after its planning assignment was overridden to a different
  agent version, or after it was marked invalid:** the pre-dispatch
  re-check in `backend/src/agentic_os/worker/runner.py` regressed; inspect
  `task.failed` events for a missing `planning_assignment_drifted` /
  `planning_assignment_invalidated` reason code and confirm `invoke_tool`
  was never called for that attempt.
- **A run snapshot is missing `planning_session_id` /
  `planning_assignment_id`:** the snapshot builder in
  `backend/src/agentic_os/worker/configuration.py` regressed; this evidence
  must survive both normal execution and restart recovery.
- **An ineligible override or accept-with-unresolved-assignment succeeds
  instead of returning HTTP 422:** the eligibility/validation checks in
  `backend/src/agentic_os/api/routers/goal_planning.py` regressed.
- **`./agentic-os index check` reports stale after a doc-only change:**
  confirm no tracked source file changed; if the manifest still drifts, run
  `PATH=/opt/homebrew/bin:$PATH ./agentic-os index build --incremental` and
  re-check before committing.
