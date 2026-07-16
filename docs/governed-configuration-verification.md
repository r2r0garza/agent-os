# Governed agent configuration verification

This runbook verifies the Sprint 4 governed-configuration vertical slice from
versioned API setup through worker execution, enforcement evidence, frontend
inspection, and restart continuity.

## Automated verification

Start a local PostgreSQL 16 instance and use the repository-local backend
environment:

```bash
docker run -d --name agentic-os-verify-pg \
  -e POSTGRES_USER=agentic_os -e POSTGRES_PASSWORD=agentic_os \
  -e POSTGRES_DB=agentic_os -p 5432:5432 postgres:16

cd backend
source .venv/bin/activate
export AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os
export AGENTIC_OS_MASTER_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
PYTHONPATH=src python -m unittest \
  tests.test_domain_migrations \
  tests.test_api \
  tests.test_worker \
  tests.test_restart_recovery -v
```

The suite proves:

- migrations persist versioned agents, models, skills, MCP servers, policy
  sets, credentials, budgets, and immutable run configuration snapshots;
- API responses redact credentials and sensitive configuration while enforcing
  team/project ownership;
- policy denial and approval interrupts happen before tool side effects;
- hard budgets reject over-limit and unpriced metered calls before dispatch,
  warning budgets record threshold evidence while allowing dispatch, and
  concurrent reservations cannot overspend a hard cap;
- workers execute from pinned model, skill, MCP, policy, and budget versions;
- retries and a real worker-process kill reuse one snapshot despite later
  configuration changes;
- recovered runs expose policy, tool, cost, artifact, and audit evidence.

Run frontend verification separately:

```bash
cd frontend
pnpm lint
pnpm typecheck
pnpm build
```

Finally, from the repository root:

```bash
./agentic-os index check
git diff --check
```

## Manual local demonstration

1. Start PostgreSQL, run `alembic upgrade head`, set
   `AGENTIC_OS_MASTER_KEY`, then start the FastAPI server and frontend as
   described in the root README.
2. In the operator console, create a model profile. Confirm the saved view says
   the credential is configured but never displays the API key.
3. Create a project and goal. Provision a versioned skill, priced test MCP
   server/tool, lifetime budget, policy set, and agent version. Record the
   displayed version identifiers.
4. Persist a pending task assigned to that agent version. Until a task-creation
   UI is added, use the task setup in `backend/tests/test_restart_recovery.py`
   as the exact API/domain example.
5. Start a worker with
   `AGENTIC_OS_WORKER_TEST_PAUSE_AFTER_RUN_STARTED_SECONDS=30` and a short
   lease. When it reports the durable pause, open the run's **View pinned
   snapshot & evidence** panel, then kill the worker process.
6. Confirm the console/API keeps the first attempt in an inspectable `running`
   state. After the lease expires, start a new worker and refresh the console.
7. Confirm the first attempt becomes failed/interrupted and the second
   completes. Both attempts must show the same configuration snapshot ID and
   the recorded model, skill, MCP, policy, budget, agent-version, and tool
   identifiers.
8. Inspect the completed attempt. It must show `policy.decision`,
   `tool.invoked`, a reconciled cost-ledger entry, and the output artifact.
9. Exercise enforcement with a copied task: attach a deny policy and confirm no
   `tool.invoked` event exists; then configure a priced tool above a hard budget
   and confirm `budget.exhausted` exists with no tool invocation.
10. Inspect `budget_reservations` and `cost_ledger_entries`: successful calls
    move from reserved to reconciled, while a timed-out call remains reserved
    with `uncertain_external_side_effect` evidence for operator reconciliation.

## Budget override contract

An admin override affects budget enforcement only when its project, goal, task,
or run scope contains the action; its actor is still an admin; its reason is
non-empty; and its start/expiry window is active. The override `context` must
grant the specific exception explicitly:

```json
{
  "budget": {
    "allow_over_limit": true,
    "allow_unpriced": false
  }
}
```

`allow_over_limit` permits a scoped action beyond a hard cap.
`allow_unpriced` permits a scoped metered action without comparable pricing.
Applied overrides are copied into reservation and ledger evidence and emit a
`budget.override_applied` audit event with actor, reason, scope, and expiry.

## Interpreting failures

- **Backend tests skip:** PostgreSQL is unreachable at
  `AGENTIC_OS_DATABASE_URL`.
- **Credential tests fail or a secret appears:** stop using the environment;
  redaction is a release blocker and the exposed credential must be rotated.
- **A denied or over-budget run invokes a tool:** pre-side-effect governance has
  regressed; inspect `policy.decision`, `budget.exhausted`, and cost reservations.
- **Recovered attempts have different snapshot IDs:** retry continuity has
  regressed; inspect `run_configuration_snapshots` and `run.interrupted` events.
- **The evidence panel is empty:** verify the API can return run-filtered audit
  and ledger records before debugging the frontend proxy.
- **Frontend build fails while lint/typecheck pass:** inspect Next.js build
  output and confirm the configured backend URL is not required at build time.
