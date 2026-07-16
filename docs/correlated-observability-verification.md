# Correlated observability verification

This checklist verifies the Sprint 6 operator timeline and admin health views
against persisted, versioned backend APIs. It intentionally uses no mock
frontend state.

## Automated coverage

Run the backend suite from `backend/` with the local PostgreSQL instance
available:

```bash
source .venv/bin/activate
python -m pytest tests/test_observability_api.py tests/test_worker.py \
  tests/test_restart_recovery.py
```

- `test_observability_api.py` covers redaction of secrets in timeline and
  detail responses, project/task/run ownership authorization, admin-only
  health and failed-delivery views, and degraded/stale health reporting for
  workers, sandboxes, telemetry, and the event stream.
- `test_worker.py` covers correlated evidence persisted by a real worker run
  (`test_worker_persists_correlated_records_when_export_is_disabled`) and
  telemetry export failure isolation from the completed product transaction
  (`test_export_failure_is_persisted_without_rolling_back_completed_run`).
- `test_restart_recovery.py`
  (`test_worker_process_kill_and_restart_resumes_without_duplicating_work`)
  covers restart continuity: a killed and resumed worker run keeps one stable
  task-derived correlation id across both attempts, canonical observability
  records and telemetry export attempts for the interrupted and completed
  runs remain queryable through `/api/v1/tasks/{task_id}/observability-timeline`
  with secrets redacted, and `/api/v1/admin/observability/health` stays
  reachable after the restart.

Run `./agentic-os index check` after backend source changes, and from
`frontend/` run `pnpm lint`, `pnpm typecheck`, and `pnpm build` for frontend
changes.

## Manual verification

The steps below exercise the same workflow interactively when a fixture or
automated case is not enough evidence on its own (e.g. exporter outage
timing, browser reload behavior).

## Setup

1. Start PostgreSQL and the backend API using the repository-local development
   workflow.
2. From `frontend/`, run `pnpm dev` and open the operator console as the default
   admin user.
3. Select a persisted project and a goal that has at least one run with model,
   tool, approval, budget, artifact, sandbox, or checkpoint activity.

## Operator timeline

1. Confirm **Goal and run timeline** loads canonical records for the selected
   goal from `/api/v1/goals/{goal_id}/observability-timeline`.
2. Change **Timeline scope** to a run and confirm only that run's correlated
   events remain. Return to the entire goal scope and confirm all goal events
   return in chronological order.
3. Open **Evidence details** for model, tool, approval, cost, artifact, sandbox,
   and checkpoint records. Confirm the trace/span reference, correlation ID,
   linked canonical evidence IDs, capture policy, and redaction evidence match
   the backend response.
4. Confirm secrets do not appear in attributes, capture policy, redaction
   evidence, delivery evidence, or failure messages. Redacted values should be
   visibly represented by the backend's redaction marker.
5. Reload the browser. Confirm the persisted project/goal selection is restored
   and the same timeline is refetched from the backend rather than retained only
   in client memory.

## Telemetry disabled, delayed, failed, and recovered

1. With no telemetry exporter configured, confirm the admin view says external
   telemetry is disabled while the canonical timeline remains available.
2. Configure an exporter with prompt/output capture disabled. Confirm the view
   shows its enabled/configured state, capture flags, and redaction policy
   evidence.
3. Point the exporter at an unavailable sink and run work. Confirm failed or
   delayed delivery badges and retry timing appear without removing canonical
   timeline records, audit links, or cost evidence.
4. Restore the sink and process pending delivery. Confirm a refresh or the
   10-second poll shows delivered/recovered state while correlation identifiers
   remain unchanged.

## Admin health and authorization

1. Confirm **Delivery and system health** reports database latency, task queue
   depth, active/stale workers, retry/failure counts, Docker/Podman availability,
   latest event-stream record, delivery counts, and exporter status from
   `/api/v1/admin/observability/health`.
2. Create or use fixtures for a stale worker, failed run, delayed event stream,
   unavailable sandbox runtime, and failed telemetry delivery. Confirm each state
   is visibly distinct and the overall health state is degraded appropriately.
3. Restart the worker and repair telemetry delivery. Confirm retry/recovery state
   updates without losing the prior canonical history.
4. Restart the frontend with `AGENTIC_OS_USER_ID` set to a regular project member.
   Confirm the operator timeline remains available but the installation health
   panel shows **Admin role required**.
5. Use a regular user without project membership and confirm the timeline shows
   the backend authorization error with a refresh/retry action available.

## Frontend checks

From `frontend/`, run:

```bash
pnpm lint
pnpm typecheck
pnpm build
```
