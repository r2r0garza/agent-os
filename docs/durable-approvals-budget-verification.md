# Durable approvals and budget governance verification

This runbook verifies the Sprint 5 vertical slice from persisted approval and
budget configuration through worker interrupts, operator/admin decisions,
resumed execution, and inspectable governance evidence.

## Automated verification

Start PostgreSQL 16 and use the repository-local backend environment:

```bash
docker run -d --name agentic-os-verify-pg \
  -e POSTGRES_USER=agentic_os -e POSTGRES_PASSWORD=agentic_os \
  -e POSTGRES_DB=agentic_os -p 5432:5432 postgres:16

cd backend
source .venv/bin/activate
export AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os
PYTHONPATH=src python -m unittest \
  tests.test_domain_migrations \
  tests.test_governance_api \
  tests.test_worker \
  tests.test_restart_recovery -v
```

The suite proves that:

- migrations preserve versioned approval configurations, requests, decisions,
  overrides, reservations, and reconciled ledger records;
- regular users can act only within their project, while override creation is
  admin-only and sensitive request/evidence fields are redacted;
- worker-created approval requests stop before side effects, can be approved
  through the real API, and resume with the same pinned configuration;
- deny and expiry decisions fail the waiting run without dispatching its tool;
- warning budgets allow priced work and record threshold evidence, while hard
  budgets reject over-limit or unpriced work before dispatch;
- scoped admin overrides are copied into reservation/ledger evidence;
- pending/running state survives a worker-process kill and is reconciled by a
  fresh process without duplicating completed work.

Run frontend and repository checks separately:

```bash
cd frontend
pnpm lint
pnpm typecheck
pnpm build

cd ..
./agentic-os index check
git diff --check
```

## Manual local demonstration

Use disposable data because the expiry and hard-stop cases intentionally leave
failed runs and governance records for inspection.

1. Start PostgreSQL, apply `alembic upgrade head`, then start FastAPI and the
   frontend using the root README. Use `AGENTIC_OS_USER_ID` (or the
   `X-Agentic-User-ID` header) to switch between a regular project member and an
   admin.
2. Create a project, goal, priced MCP tool, agent version, and budget. Configure
   the project approval mode as `every_tool_call`, or use `consequential` with
   `mcp.call` in `consequential_action_types`.
3. Submit an assigned task and run one worker iteration. Confirm the task becomes
   `blocked`, its run becomes `waiting_approval`, the approval appears under
   **Pending approvals**, and no `tool.invoked` event exists.
4. Approve the request as a regular project member. Confirm an
   `approval.approved` decision and `approval.resume_ready` event are visible,
   then run the worker again. Confirm exactly one tool invocation, a completed
   retry, a reconciled reservation/ledger entry, and the same pinned
   configuration snapshot on both attempts.
5. Repeat with a new task and deny its request. Repeat once more with a request
   whose expiry has passed and choose expire. In both cases confirm the waiting
   run fails, the decision remains visible after reload, and no tool dispatch is
   recorded.
6. Leave another request pending, stop the worker/API processes, restart them,
   and reload the console. Confirm the same request and `waiting_approval` run
   remain. Resolve it and confirm execution resumes from the durable boundary.
7. Set a warning budget below the priced tool cost. Confirm execution completes
   while `budget.warning_threshold` and warning-marked reservation/ledger
   evidence appear.
8. Set a hard-stop budget below the same price, then try an unpriced metered
   tool. Confirm `budget.exhausted` (or unpriced rejection) appears and neither
   action invokes the tool.
9. As a regular user, attempt to create a scoped override and confirm the API/UI
   reports `Admin role required`. As an admin, create a time-bound task or run
   override with a reason and `budget.allow_over_limit: true`; retry the action
   and confirm `governance.admin_override_created` and
   `budget.override_applied`, including actor, scope, reason, and expiry.
10. Reload the frontend after each mutation. Confirm pending/resolved approvals,
    overrides, reservations, ledger entries, and audit evidence are fetched from
    the backend and unchanged.

The exact approval queue and admin-control UI checks are also listed in
`docs/governance-operations-verification.md`. The process-kill procedure is in
`docs/restart-recovery-verification.md`.

## Interpreting failures

- **Approval exists but a tool was invoked:** the pre-side-effect interrupt
  regressed; inspect the run's approval request IDs and `policy.decision` events.
- **Approval succeeds but the task stays blocked:** inspect unresolved sibling
  requests and rejected decisions; resume occurs only when every required
  request is approved.
- **Warning mode hard-stops:** compare the persisted budget enforcement mode and
  the worker's pinned budget snapshot.
- **Hard-stop or unpriced work invokes a tool:** treat this as a governance
  release blocker and inspect reservation status before retrying.
- **Override has no effect:** verify admin role, scope containment, active time
  window, non-empty reason, and the explicit `budget` exception in its context.
- **Pending approval disappears after restart:** verify both processes use the
  same PostgreSQL URL and inspect the durable request/run rows before rerunning.
- **Frontend controls are missing:** verify the injected user identity and
  project membership, then inspect the backend response before debugging UI
  state.
