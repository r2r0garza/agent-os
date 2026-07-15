# Restart recovery verification

This documents the local demonstration that the Sprint 1 foundation slice
survives a deliberate mid-run process termination and resumes from the last
committed PostgreSQL boundary (Sprint 1 exit criterion 6).

## Automated harness

`backend/tests/test_restart_recovery.py` runs the worker as a real OS
process (not an in-process call), kills it with `SIGKILL` while a run is
persisted as `"running"`, restarts a fresh worker process, and asserts:

- the interrupted run and its owning task remain durably `"running"` in
  PostgreSQL immediately after the kill (an explicit, inspectable
  recoverable state, not a lost or silently retried attempt);
- the restarted worker reconciles the interrupted attempt (`run.interrupted`
  audit event, run marked `"failed"`) and completes a new attempt with its
  own idempotency key, instead of duplicating the finished step;
- progress, audit, cost-ledger, and artifact evidence remain fetchable
  through the versioned API after the restart;
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
  PYTHONPATH=src python -m unittest tests.test_restart_recovery -v
```

The test skips with a clear message if PostgreSQL is unreachable. If Docker
or Podman is also available, it additionally exercises a real sandboxed
task inside the same restart-recovery flow; otherwise it exercises the
non-sandboxed tool/skill path only.

## Manual walkthrough

To observe the same recovery by hand:

1. Start PostgreSQL as above and apply migrations:
   ```bash
   cd backend
   AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
     alembic upgrade head
   ```
2. Configure a model profile, project, goal, skill (+version), MCP server
   (+version), agent (+version referencing the skill/MCP/tools), and budget
   through the versioned API (`POST /api/v1/...`; see
   `backend/tests/test_api.py` for exact payloads), then persist a `pending`
   `Task` for the goal with `assigned_agent_version_id` set.
3. Start the worker with a short lease and a deliberate pause so you have
   time to kill it mid-run:
   ```bash
   AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
     AGENTIC_OS_WORKER_TEST_PAUSE_AFTER_RUN_STARTED_SECONDS=30 \
     python -m agentic_os worker run-once --worker-id demo-worker-1 --lease-seconds 5
   ```
4. Once the process logs `run started; pausing ...`, confirm the run is
   `"running"` via `GET /api/v1/tasks/{task_id}/runs`, then kill the process
   (`kill -9 <pid>` or Ctrl-\\).
5. Wait for the lease to expire (5s in this example), then restart a plain
   worker process to resume:
   ```bash
   AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
     python -m agentic_os worker run-once --worker-id demo-worker-2
   ```
6. Inspect `GET /api/v1/tasks/{task_id}/runs`, `GET /api/v1/audit-events`,
   and `GET /api/v1/cost-ledger-entries` to see the interrupted first
   attempt, the completed second attempt, and the associated audit/cost
   evidence.

## Interpreting failures

- **The test skips**: PostgreSQL is not reachable at
  `AGENTIC_OS_DATABASE_URL` (defaults to
  `postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os`).
  Start the container above and re-run.
- **The task never reaches `"running"` before the timeout**: the worker
  subprocess failed to start or claim the task; check its captured stderr
  for a traceback, and confirm migrations were applied to the same database
  the test/worker points at.
- **The restarted worker exits non-zero**: it hit an unhandled exception
  while reconciling or re-executing the task; the process's stderr contains
  the error, and the task/run rows will show `"failed"` with a
  `task.failed` audit event rather than silently vanishing.
- **A duplicate `"completed"` run appears for the same attempt number**:
  this would indicate the idempotency/reconciliation guarantee regressed;
  it should never happen and is the primary correctness property this
  harness checks.
