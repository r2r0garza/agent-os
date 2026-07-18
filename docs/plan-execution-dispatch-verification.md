# Accepted-plan dispatch across pinned agents verification

This documents the local demonstration that Sprint 15 exit criterion 2 holds:
an accepted capability-aware goal plan (Sprint 14,
[docs/capability-aware-goal-planning-verification.md](capability-aware-goal-planning-verification.md))
dispatches its materialized tasks through the real scheduler and worker
across multiple pinned agent versions, honoring dependencies, safe
parallelism, policy/budget preflight, retries, and recovery boundaries. It
extends [docs/multi-agent-verification.md](multi-agent-verification.md),
which proves the same dispatch guarantees for the older direct task-graph
API rather than the goal-planning → accept → `GoalPlanExecution` path.

## What changed

The claim/lease/dispatch machinery (`agentic_os.worker.leases.claim_ready_task`,
`agentic_os.worker.scheduler.run_scheduler_once`,
`agentic_os.worker.runner.run_task_worker_once`) already operated on any
`Task` row regardless of how it was assigned, so it required no new
primitives to dispatch a materialized plan's tasks across pinned agent
versions. The gap was that `GoalPlanExecution` (the durable per-plan
progress envelope from Sprint 14) only recomputed its status, task counters,
and `PlanTaskContextPackage.run_id` linkage lazily, when an operator polled
`GET /api/v1/goals/{goal_id}/planning-sessions/{id}/execution`. Nothing
updated it as dispatch actually happened.

`agentic_os.worker.runner.run_task_worker_once` now calls
`agentic_os.domain.plan_execution.refresh_plan_execution_progress` at every
point a claimed task's status changes (claim, goal-control interrupt before
dispatch, terminal failure, and normal completion), through a new
`_refresh_plan_execution_for_task` helper that is a no-op for tasks not
linked to an accepted plan via `PlanTaskContextPackage`. A plan's execution
status and progress counters are therefore live evidence of real dispatch,
not only a value computed the next time the API is read.

## Automated harness

`backend/tests/test_plan_execution_dispatch.py` builds two agent versions
(research/writing capabilities, each with its own skill, MCP tool, and
budget) and four tasks directly against the domain models, then drives them
through the real goal-planning API (`POST
/api/v1/goals/{goal_id}/planning-sessions` and `.../accept`) to materialize
an accepted `GoalPlanExecution`, and finally through the real in-process
scheduler (`run_scheduler_once`) rather than a mocked worker:

- `test_accepted_plan_dispatches_across_pinned_agents_with_dependencies_and_progress`
  drains a plan containing a dependent chain (`root` → `downstream`, on
  different pinned agent versions) and a genuinely conflicting resource-key
  pair (`conflict-a`/`conflict-b`, on different agent versions, both writing
  the same key). It asserts, through direct row reads (not the
  recompute-on-read execution endpoint):
  - the plan produced task runs on at least two distinct pinned agent
    versions;
  - `downstream`'s run never starts before `root`'s run is committed
    completed;
  - `conflict-a` and `conflict-b`'s run windows never overlap;
  - `GoalPlanExecution.status` reached `"completed"` and every
    `PlanTaskContextPackage.run_id` points at the task's completed run,
    proving the worker kept progress live during dispatch.
- `test_retry_after_transient_failure_preserves_attempt_history_without_reresolving_assignment`
  forces `downstream`'s first attempt to fail after its run and
  configuration snapshot are durably committed, brings the task back to
  claimable state (the same recovery shape restart reconciliation produces
  for an expired lease), and asserts the second attempt reuses -- rather
  than re-resolves -- the first attempt's configuration snapshot and pinned
  agent version, and that both attempts remain in `Run` history.
- `test_policy_denial_before_dispatch_fails_closed_without_partial_side_effects`
  denies policy for one of the two pinned agents after acceptance and
  asserts the affected tasks fail closed on the policy check that runs
  immediately before any tool side effect (no `tool.invoked` audit event),
  while the unaffected, independent tasks still complete, and
  `GoalPlanExecution` reflects the mixed `"failed"` outcome.

Because a worker thread that hits a dispatch-time failure exits that
thread's claim loop entirely rather than retrying (matching production,
where `deploy/worker-loop.sh` polls `worker run-once` again every few
seconds regardless of errors), these tests drain the scheduler through a
small bounded retry loop (`_drain_scheduler`) instead of assuming one
`run_scheduler_once` pass always exhausts every claimable task.

Run it against a local PostgreSQL 16 instance:

```bash
docker run -d --name agentic-os-verify-pg \
  -e POSTGRES_USER=agentic_os -e POSTGRES_PASSWORD=agentic_os -e POSTGRES_DB=agentic_os \
  -p 5432:5432 postgres:16
# or: podman run -d --name agentic-os-verify-pg ... postgres:16

cd backend
source .venv/bin/activate  # or your project virtualenv
AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
  PYTHONPATH=src python -m pytest tests/test_plan_execution_dispatch.py -v
```

The test module skips with a clear message if PostgreSQL is unreachable.

## Interpreting failures

- **The test module skips**: PostgreSQL is not reachable at
  `AGENTIC_OS_DATABASE_URL`. Start the container above and re-run.
- **`downstream`'s run starts before `root` completes, or the conflicting
  pair overlaps**: the underlying dispatch guarantees proven in
  [docs/multi-agent-verification.md](multi-agent-verification.md) regressed;
  this test exercises the same guarantees through the goal-planning
  materialization path instead of the direct task-graph API.
- **`GoalPlanExecution.status`/counters read stale after a direct row read**
  (not through the `.../execution` endpoint): the live-refresh wiring in
  `agentic_os.worker.runner._refresh_plan_execution_for_task` regressed, or a
  new task-status transition path was added to the worker without calling
  it.
- **The retry test shows two different `configuration_snapshot_id` values,
  or a different `agent_version_id`, across attempts**: retry re-resolved
  the pinned assignment from scratch instead of reusing the first attempt's
  snapshot (`agentic_os.worker.configuration.resolve_run_configuration`),
  which breaks the "do not re-resolve assignments from scratch" guarantee.
- **A `tool.invoked` audit event appears for a policy-denied task**: the
  policy preflight in `agentic_os.worker.runner._execute_claimed_task`
  stopped failing closed before a tool side effect.
