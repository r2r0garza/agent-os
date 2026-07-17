# Goal lifecycle, steering, and cancellation verification

This is the consolidated Sprint 9 verification for durable goal pause, resume,
cancel, steering, graph revision, worker-control, frontend, and recovery
behavior. Goals remain the authoritative durable object; lifecycle commands
and steering requests are committed and attributed before the API acknowledges
them.

## Automated verification

Start PostgreSQL 16 if it is not already available:

```bash
docker run -d --name agentic-os-verify-pg \
  -e POSTGRES_USER=agentic_os -e POSTGRES_PASSWORD=agentic_os -e POSTGRES_DB=agentic_os \
  -p 5432:5432 postgres:16
# or use the equivalent podman command
```

Run the focused backend coverage from the repository root:

```bash
cd backend
source .venv/bin/activate
AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
  PYTHONPATH=src pytest \
  tests/test_domain_migrations.py \
  tests/test_goal_lifecycle_api.py \
  tests/test_scheduler.py \
  tests/test_worker.py \
  tests/test_sandbox_execution.py -v
```

The suite proves:

- lifecycle commands, steering requests, graph revisions, cancellation
  deadlines, ordered events, and actor attribution survive a new database
  session;
- pause, resume, cancel, steering, revision inspection, authorization,
  idempotent replay, stale revision rejection, and completed-task immutability
  work through versioned APIs;
- an API dependency restart still reads the exact committed lifecycle,
  steering, revision, and completed-task evidence without duplicate events;
- the scheduler does not claim work for paused or cancelled goals;
- a worker cooperatively pauses at a safe boundary, reconciles an expired
  paused attempt after restart, and forces cancellation after the grace
  deadline without overwriting attempt history;
- a live sandbox receives a forced stop and cleanup when cancellation becomes
  effective.

Run the frontend workflow checks:

```bash
cd frontend
pnpm test -- goal-lifecycle-panel.test.tsx
pnpm lint
pnpm typecheck
```

These checks cover loading and empty states, pause/resume/cancel controls,
pending controls, steering submission and application, graph revision and
event history, authorization failures, degraded API responses, and recovering
or cancelled explanations using real API contracts.

Finish with repository integrity checks:

```bash
git diff --check
PATH=/opt/homebrew/bin:$PATH ./agentic-os index build --incremental
PATH=/opt/homebrew/bin:$PATH ./agentic-os index check
```

## Manual end-to-end smoke test

Use a disposable local database and artifact root. Start the API, worker, and
frontend with the same `AGENTIC_OS_DATABASE_URL`, then:

1. In the operator console, create or select a project and submit a goal whose
   task graph contains at least one running task and one remaining task.
2. Open **Goal lifecycle and steering**. Select **Pause**, record the displayed
   command ID, and confirm the goal becomes `paused`. Verify no new task is
   claimed and the active run reaches a durable cancelled attempt while its
   task remains resumable.
3. Submit a steering instruction that revises the remaining task and adds a
   review task. Apply it, then inspect revision 1. Confirm the completed task
   is unchanged, unfinished work is explicitly superseded, dependencies point
   at replacement tasks, and assignment/policy/budget evidence is visible.
4. Select **Resume**. Confirm the goal becomes `active`, the worker acknowledges
   the pending resume control, and only the revised ready graph is dispatched.
5. While a task is active, select **Cancel**. Confirm queued work becomes
   `cancelled`. During the grace period, confirm cooperative interruption; to
   exercise forced stop, keep a sandbox active beyond the displayed deadline
   and confirm forced-termination timestamps plus sandbox stop/cleanup events.
6. Stop the API and worker. Restart both against the same PostgreSQL database
   and artifact root. Reload the console and confirm the selected goal remains
   cancelled with the same command IDs, steering request, graph revision,
   event sequence, run attempts, artifacts, cost entries, and audit evidence.
   A further worker run must not claim cancelled work.
7. Run `agentic-os operations backup`, verify the archive, and restore it into
   an isolated database and artifact root as described in
   [local operations verification](local-operations-verification.md). Start a
   stopped API/worker pair against the restored targets and repeat the evidence
   checks from step 6.

## Evidence to record

For the goal under test, retain the responses or screenshots for:

- `GET /api/v1/goals/{goal_id}`;
- lifecycle commands, steering requests, graph revisions, and lifecycle events
  under `/api/v1/goals/{goal_id}/...`;
- `GET /api/v1/goals/{goal_id}/task-graph`;
- `GET /api/v1/tasks/{task_id}/runs`;
- `GET /api/v1/audit-events` and `GET /api/v1/cost-ledger-entries`;
- the frontend lifecycle panel before restart, after restart, and after the
  isolated restore.

The expected invariant is one ordered, attributed history: restart or restore
may create a new run attempt after an interrupted lease, but must never erase a
committed command, revision, event, completed attempt, artifact, or ledger
entry, and must never create duplicate completion.

## Interpreting failures

- A PostgreSQL-dependent module skips when the configured database is
  unreachable. Start the disposable database and rerun; a skip is not a pass.
- A paused or cancelled task being claimed indicates a scheduler control
  regression.
- Missing or reordered events after restart indicate the API is not reading
  the committed lifecycle boundary.
- Mutation of completed work or silent replacement of dependencies indicates a
  graph revision integrity regression.
- A running sandbox that survives an expired cancellation grace period
  indicates a forced-stop regression.
- Evidence missing only after restore should be investigated with
  `operations verify-backup` before changing application state.
