# Governance operations verification

This checklist verifies the Sprint 5 approval queue, admin override workflow,
budget evidence, authorization states, and reload behavior against the real
versioned backend API. It intentionally does not use mock frontend state.

## Setup

1. Start PostgreSQL and the backend API using the repository-local development
   workflow.
2. From `frontend/`, run `pnpm dev` and open the operator console.
3. Select a project with a decomposed task graph and an agent version configured
   for `consequential` or `every_tool_call` approval mode and a warning or
   hard-stop budget.
4. Run the worker until a tool action creates a durable approval request and the
   run enters `waiting_approval`.

## Regular-user approval path

1. Load the console as a regular user who is a member of the selected project.
   Set `AGENTIC_OS_USER_ID` to that user's UUID before starting `pnpm dev`, or
   have the deployment inject `X-Agentic-User-ID`; the Next.js API proxy forwards
   either identity to FastAPI.
2. Confirm **Pending approvals** shows the action type, redacted action preview,
   policy evidence, expiry, and run identifier.
3. Enter an optional reason and approve the request. Confirm the success message,
   the request moving to **Resolved history**, and the task/run state refreshing.
4. Create another request and deny it. Confirm the request is marked `denied`, the
   gated action does not run, and the failed run appears in recovery state.
5. Allow a request to pass its expiry and resolve it through the backend expiry
   flow. Confirm `expired` is visibly distinct from pending and approved states.
6. Attempt to load a project with a regular user who lacks project membership.
   Confirm the console shows the backend's unauthorized error and a retry action.

## Admin override path

1. Load the same project as an admin and confirm **Admin overrides** exposes the
   project, goal, task, and run scopes currently present in the console.
2. Create a time-bound override with a concrete reason. Confirm it appears in
   override history with scope, expiry, and active state.
3. Confirm **Budget & policy evidence** includes
   `governance.admin_override_created`, and that a later governed action records
   `budget.override_applied` when the override is used.
4. Load the page as a regular project member. Confirm approval controls remain
   available but override creation is replaced by the explicit
   `Admin role required` state.

## Budget, policy, and recovery evidence

1. Run a priced action and confirm its reservation amount/status and reconciled
   ledger amount/status are shown both in project governance and the run evidence
   panel.
2. Cross a warning threshold and confirm the reservation is labeled and the
   `budget.warning_threshold` event appears.
3. Exhaust a hard-stop budget and confirm the reservation/ledger uses the
   destructive state, `budget.exhausted` appears, and no gated side effect occurs.
4. Attempt an unpriced metered action under a hard budget and confirm `unpriced`
   is shown rather than a misleading zero-cost value.
5. Stop and restart the worker with an approval pending. Confirm the pending
   request and `waiting approval` run survive, then resolve and resume it.

## Persistence and reload

After each approval or override mutation, reload the browser. Confirm pending and
resolved requests, override history, blocked/recovery state, reservations, ledger
entries, and policy events are refetched from the backend and remain unchanged.

## Frontend checks

From `frontend/`, run:

```bash
pnpm lint
pnpm typecheck
pnpm build
```
