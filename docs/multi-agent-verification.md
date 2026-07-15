# Multi-agent scheduling, conflict, and restart-recovery verification

This documents the local demonstration that the Sprint 2 vertical slice
survives multi-task, multi-agent scheduling under real concurrency,
including a genuinely conflicting resource-key pair, a safe parallel pair,
and a deliberate mid-run process kill while several tasks are in flight
(Sprint 2 exit criterion 6). It extends
[docs/restart-recovery-verification.md](restart-recovery-verification.md),
which covers the single-task Sprint 1 restart-recovery slice.

## Automated harness

`backend/tests/test_multi_agent_verification.py` builds a goal's task graph
entirely through the versioned API (`POST /api/v1/goals/{id}/task-graph`),
assigns tasks to two distinctly capable agents through
`POST /api/v1/tasks/{id}/assignment`, and runs the real
`agentic-os worker run-once` CLI as an OS process (not an in-process call
or a mocked scheduler):

- `MultiAgentVerificationTests.test_multi_agent_task_graph_respects_dependencies_and_safe_parallelism`
  submits one graph with a dependent chain (`root` → `downstream`, on
  different agents), a genuinely conflicting resource-key pair
  (`conflict-a`/`conflict-b`, on different agents, both writing the same
  key), and a disjoint parallel-safe pair (`parallel-a`/`parallel-b`).
  It runs the graph to completion with three concurrent worker threads in
  one process and asserts, entirely through the API and the persisted
  `Run.started_at`/`completed_at` evidence:
  - `downstream`'s run never starts before `root`'s run is committed
    completed;
  - `conflict-a` and `conflict-b`'s run windows never overlap, even though
    two idle workers and two different agents were available to run them
    concurrently;
  - every workspace promotion for the graph is `"promoted"` (no task ever
    reached an unsafe conflict state), and resulting resource revisions
    match one promotion per disjoint key and two serialized promotions for
    the conflicting key;
  - the two capability tracks were assigned to two different agent
    versions, evidencing real multi-agent delegation rather than one agent
    executing every task.
- `MultiAgentVerificationTests.test_restart_recovers_multiple_simultaneous_in_flight_tasks`
  submits the same shape of graph, starts four concurrent worker threads
  with a short lease and a deliberate pause immediately after each run
  commits as `"running"`, waits for at least two tasks to reach `"running"`
  simultaneously, and `SIGKILL`s the process. It then asserts:
  - the interrupted tasks remain durably `"running"` in PostgreSQL, and
    `downstream` (blocked on `root`) never started;
  - after the lease window expires, a fresh worker process reconciles every
    interrupted attempt (`run.interrupted` audit event, stale run marked
    `"failed"`) and completes a new attempt for each, instead of resuming or
    duplicating the finished step;
  - `downstream` still only runs after `root`'s restart-recovered attempt
    completes, so dependency ordering survives the crash;
  - a further restart with no remaining work claims zero tasks.

Run it against a local PostgreSQL 16 instance:

```bash
docker run -d --name agentic-os-verify-pg \
  -e POSTGRES_USER=agentic_os -e POSTGRES_PASSWORD=agentic_os -e POSTGRES_DB=agentic_os \
  -p 5432:5432 postgres:16
# or: podman run -d --name agentic-os-verify-pg ... postgres:16

cd backend
source .venv/bin/activate  # or your project virtualenv
AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
  PYTHONPATH=src python -m pytest tests/test_multi_agent_verification.py -v
```

The test module skips with a clear message if PostgreSQL is unreachable.

## Manual walkthrough

To observe the same behavior by hand:

1. Start PostgreSQL as above and apply migrations, then start the API:
   ```bash
   cd backend
   AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
     alembic upgrade head
   uvicorn agentic_os.api.app:create_app --factory --reload --host 127.0.0.1 --port 8000
   ```
2. Configure a model profile, project, and goal, then two agents (each with
   its own skill version, MCP tool version, and budget) through the
   versioned API — see `_build_project_and_agents` in
   `backend/tests/test_multi_agent_verification.py` for exact payloads.
3. Submit a task graph via `POST /api/v1/goals/{goal_id}/task-graph` with a
   dependent pair, a pair sharing one `resource_intent` write key, and a
   disjoint pair — see `_submit_task_graph` in the same test file.
4. Call `POST /api/v1/tasks/{task_id}/assignment` for each task and confirm
   `GET /api/v1/tasks/{task_id}/assignment` shows the expected agent version
   per capability.
5. Start several concurrent worker threads in one process:
   ```bash
   AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
     python -m agentic_os worker run-once --worker-id demo-worker --workers 3
   ```
6. Inspect `GET /api/v1/goals/{goal_id}/task-graph`,
   `GET /api/v1/tasks/{task_id}/runs`, and `GET /api/v1/audit-events` to see
   dependency ordering, the serialized conflicting pair, and the disjoint
   pair's promotions.
7. To observe restart recovery under multi-task load, resubmit a fresh
   graph, then start the worker with a short lease and a pause so several
   tasks stay `"running"` long enough to kill:
   ```bash
   AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
     AGENTIC_OS_WORKER_TEST_PAUSE_AFTER_RUN_STARTED_SECONDS=20 \
     python -m agentic_os worker run-once --worker-id demo-worker-1 --workers 4 --lease-seconds 5
   ```
   Once several tasks show `"running"` via the task-graph endpoint, kill the
   process (`kill -9 <pid>`), wait for the lease window to pass, then
   restart a plain worker to resume:
   ```bash
   AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
     python -m agentic_os worker run-once --worker-id demo-worker-2 --workers 4
   ```
   Inspect the task graph and audit events again to see every interrupted
   task reconcile and complete without duplicating work.

## Interpreting failures

- **The test module skips**: PostgreSQL is not reachable at
  `AGENTIC_OS_DATABASE_URL` (defaults to
  `postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os`).
  Start the container above and re-run.
- **`downstream`'s run starts before `root` completes**: dependency-ordered
  claiming (`agentic_os.worker.leases.claim_ready_task`) regressed; this
  should never happen and is the primary correctness property of the
  dependent-chain assertion.
- **The conflicting pair's run windows overlap**: workspace resource
  leasing (`agentic_os.worker.workspace.acquire_resource_leases`) failed to
  serialize two tasks writing the same resource key; this is the primary
  correctness property of the conflict assertion and should never happen
  regardless of how many idle workers or distinct agents are available.
- **A workspace promotion shows `"conflict"` or `"denied"`** instead of
  `"promoted"`: an unexpected resource-revision or lease/fencing-token
  mismatch occurred; inspect `WorkspacePromotion.conflict_details` and the
  `workspace.promotion_conflict`/`workspace.promotion_denied` audit events
  for the affected resource key.
- **Fewer than two tasks reach `"running"` before the restart test's kill
  timeout**: the worker subprocess failed to start or claim work; check its
  captured stderr for a traceback and confirm migrations were applied to
  the same database the test points at.
- **A duplicate `"completed"` run appears for the same attempt number, or a
  task needs more than one recovery attempt after its stale run is marked
  `"failed"`**: this would indicate the idempotency/reconciliation guarantee
  regressed; it should never happen.
