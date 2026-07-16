# Correlated observability verification

This checklist verifies the Sprint 6 operator timeline and admin health views
against persisted, versioned backend APIs. It intentionally uses no mock
frontend state.

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
