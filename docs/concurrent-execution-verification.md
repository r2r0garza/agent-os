# Concurrent goal execution and workspace safety verification

This documents the Sprint 11 end-to-end evidence that multiple goals can run
concurrently in the same project without corrupting shared workspace state,
that resource conflicts are detected and surfaced for resolution, and that a
worker failure mid-concurrent-run recovers cleanly. It ties automated test
coverage and a manual smoke workflow back to each Sprint 11 exit criterion.

## Automated coverage

### Backend: concurrent goal execution and workspace promotion (`#67`)

`backend/tests/test_concurrent_goal_execution.py` exercises the worker and
workspace-promotion path with real threads and a real subprocess worker
against PostgreSQL:

- `test_two_goals_with_disjoint_resource_keys_complete_concurrently` —
  exit criterion 1. Two goals writing to disjoint resource keys both reach
  `completed`, and a shared counter proves their task execution actually
  overlaps in wall-clock time rather than merely both finishing eventually.
- `test_two_goals_with_overlapping_resource_keys_serialize_without_corruption` —
  exit criteria 1 and 2. Two goals that write the same resource key never
  execute concurrently on that key; the workspace protocol serializes the
  conflicting tasks via the resource lease, and the shared resource ends at
  the correct final revision reflecting both writes.
- `test_concurrent_goal_decomposition_produces_independent_task_dags` —
  two goals decomposed concurrently through `POST
  /api/v1/goals/{id}/task-graph/decompose` each get their own complete,
  non-overlapping task DAG with no cross-goal dependency leakage.
- `test_worker_killed_mid_promotion_recovers_via_stale_lease` — exit
  criterion 5(d). A worker subprocess is `SIGKILL`ed immediately after
  `promote_workspace_changes` stages its revision and lease mutations in an
  open transaction; the rollback discards the partial promotion entirely,
  the lease goes stale, and a restarted worker recovers and completes the
  task from a consistent workspace state.

`backend/tests/test_workspace.py` (`WorkspacePromotionTests`) covers the
promotion primitives directly: disjoint resources promote atomically,
promotion against a changed expected revision persists an explicit conflict
record instead of silently overwriting, and a stale fencing token cannot
promote.

`backend/tests/test_bootstrap_concurrency.py` covers concurrent
cold-start/bootstrap paths (default team/user/membership creation, inventory
fan-out) resolving to a single row under concurrent load — a prerequisite
for safely running concurrent goals against a fresh installation.

### Backend: lease, conflict, and promotion evidence API (`#68`)

`backend/tests/test_workspace_api.py` (`WorkspaceApiTests`) covers exit
criteria 2 and 4 at the API boundary:

- `test_project_member_lists_active_stale_and_fenced_leases` —
  `GET /api/v1/projects/{id}/workspace/leases` (project-scoped) and
  `GET /api/v1/admin/workspace/leases` (installation-scoped) both return
  active, stale, and fenced lease states.
- `test_project_member_lists_conflicts_and_promotion_deltas` —
  `GET /api/v1/projects/{id}/workspace/conflicts` and
  `GET /api/v1/projects/{id}/workspace/promotions` return structured
  conflict evidence (resource key, conflicting run, occurred-at) and
  promotion deltas.
- `test_project_and_installation_access_boundaries` — project-scoped
  workspace evidence is restricted to project members; installation-scoped
  admin evidence is restricted to installation admins.

### Frontend: concurrent goal and conflict resolution views (`#69`)

`frontend/components/concurrent-workspace-panel.test.tsx` covers exit
criterion 3: the panel renders per-goal progress and task-level resource
intent from `Goal`/`Task`/`Run` data, lists workspace conflicts fetched from
`GET /projects/{id}/workspace/conflicts`, and offers two resolution actions
per conflict — **discard** (`POST /goals/{id}/cancel`) and **retry from safe
revision** (`POST /goals/{id}/resume`) — without requiring the user to read
database tables.

### Frontend: operator/admin concurrent workload health (`#70`)

`frontend/components/admin-concurrent-health.test.tsx` covers exit
criterion 4: the admin view aggregates per-worker lease state (active,
stale, fenced) and per-project concurrency (active goal count, conflict
count, overlapping resource keys) from the same lease/conflict/promotion
evidence APIs.

Both are mounted together in the operator console's concurrency tab via
`frontend/components/operator-workspace.tsx` (`ConcurrentWorkspacePanel` and
`AdminConcurrentHealth`).

### Regression coverage

`backend/tests/test_restart_recovery.py` (`test_worker_process_kill_and_restart_resumes_without_duplicating_work`)
continues to pass, confirming the pre-existing single-goal kill/restart
recovery path is unaffected by the concurrent-execution changes.

## Running the automated evidence

Requires a local PostgreSQL 16 instance (Docker or Podman):

```bash
docker run -d --name agentic-os-verify-pg \
  -e POSTGRES_USER=agentic_os -e POSTGRES_PASSWORD=agentic_os -e POSTGRES_DB=agentic_os \
  -p 5432:5432 postgres:16

cd backend
source .venv/bin/activate  # or your project virtualenv
AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
  alembic upgrade head

AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
  PYTHONPATH=src python -m pytest \
  tests/test_concurrent_goal_execution.py \
  tests/test_workspace.py \
  tests/test_workspace_api.py \
  tests/test_bootstrap_concurrency.py \
  tests/test_restart_recovery.py -v
```

Frontend:

```bash
cd frontend
npm run lint
npm run typecheck
npm test -- concurrent-workspace-panel admin-concurrent-health
```

Both automated suites skip cleanly with an explicit message if PostgreSQL
(backend) or npm dependencies (frontend) are unavailable; a skip is not
evidence of correctness and must be reported as a blocker, not a pass.

## Manual smoke workflow

Run this against a local Compose stack (see
[`local-deployment.md`](local-deployment.md)) or `frontend`/`api`/`worker`
started directly against the PostgreSQL instance above.

1. **Start services.** `docker compose up --build --wait` from the
   repository root, or run `api`, `worker`, and `frontend` locally against
   the same `AGENTIC_OS_DATABASE_URL`.
2. **Create a project** through the operator console (or
   `POST /api/v1/projects`).
3. **Submit two goals to the same project.** Give each goal a task whose
   `resource_intent` writes to the *same* resource key (for example,
   `shared/output.md`) so the second goal is forced through the conflict
   path rather than completing independently. (To observe the
   non-conflicting path instead, use disjoint resource keys — both goals
   should show independent progress with no conflict entries.)
4. **Verify independent progress.** In the operator console, open the
   concurrency tab and confirm the *Concurrent workspace* panel
   (`ConcurrentWorkspacePanel`) lists both goals with live task/run status
   as workers pick them up.
5. **Observe the conflict.** Once the second goal's write attempt collides
   with the first goal's held resource lease, confirm a conflict entry
   appears in the panel showing the affected resource key(s), the
   conflicting run, and a timestamp — sourced from
   `GET /api/v1/projects/{id}/workspace/conflicts`.
6. **Resolve through the UI.** Use either resolution action on the
   conflict card:
   - **Discard conflicting run** — cancels the losing goal
     (`POST /goals/{id}/cancel`); confirm the goal transitions toward
     `cancelled` and its active runs stop.
   - **Retry from safe revision** — resumes the goal
     (`POST /goals/{id}/resume`); confirm the worker retries the affected
     task against the current (post-conflict) resource revision and the
     goal reaches `completed`.
7. **Confirm workspace integrity.** Fetch
   `GET /api/v1/projects/{id}/workspace/promotions` and confirm the
   resource key's final revision reflects exactly one coherent sequence of
   writes — no missing writes, no duplicated writes, no resource stuck at
   an intermediate/uncommitted revision.
8. **Check operator health.** Open the *Admin concurrent health* view
   (`AdminConcurrentHealth`) and confirm it shows the worker's lease as
   released (not stale/fenced) after resolution, and that the project's
   conflict count reflects the resolved conflict.

## Exit criteria traceability

| Sprint 11 exit criterion | Evidence |
| --- | --- |
| 1. Concurrent goals execute without corrupting shared state | `test_two_goals_with_disjoint_resource_keys_complete_concurrently`, `test_two_goals_with_overlapping_resource_keys_serialize_without_corruption`, manual steps 3–4, 7 |
| 2. Workspace protocol detects and surfaces conflicts via API | `test_project_member_lists_conflicts_and_promotion_deltas`, manual step 5 |
| 3. Frontend shows concurrent progress and conflict resolution | `concurrent-workspace-panel.test.tsx`, manual steps 4, 6 |
| 4. Operator/admin views expose concurrent workload health | `admin-concurrent-health.test.tsx`, manual step 8 |
| 5(a–d). Concurrent/conflicting/recovery verification | backend tests above |
| 5(d) specifically: worker failure mid-run recovers without corruption | `test_worker_killed_mid_promotion_recovers_via_stale_lease`, `test_restart_recovery.py` regression |
| 5(e). Frontend operational views for concurrent workloads | `concurrent-workspace-panel.test.tsx`, `admin-concurrent-health.test.tsx` |
| 6. Documentation of the safety model | tracked separately in `#72` (`docs/workspace-safety.md`) |
