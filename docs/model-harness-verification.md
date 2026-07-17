# Model-backed harness verification

This runbook consolidates Sprint 12 verification: model profile capability
probing, task execution through a pinned Deep Agents/LangGraph-style harness
against an OpenAI-compatible endpoint, the governed tool bridge, versioned
evidence APIs, frontend probing/run-evidence workflows, and restart recovery.
Each section is tied back to a numbered Sprint 12 exit criterion.

## Automated verification

Start PostgreSQL 16 and use the repository-local backend environment:

```bash
docker run -d --name agentic-os-verify-pg \
  -e POSTGRES_USER=agentic_os -e POSTGRES_PASSWORD=agentic_os \
  -e POSTGRES_DB=agentic_os -p 5432:5432 postgres:16

cd backend
source .venv/bin/activate
export AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os
PYTHONPATH=src:tests python -m unittest \
  tests.test_model_profile_probing \
  tests.test_model_harness \
  tests.test_worker \
  tests.test_governance_api \
  tests.test_redaction \
  tests.test_restart_recovery -v
```

`tests/test_model_harness.py` imports the shared `tests/factories.py` helpers,
so `tests` must be on `PYTHONPATH` alongside `src` (`PYTHONPATH=src:tests`),
unlike modules that only exercise a single package.

The suite proves, per exit criterion:

1. **Capability probing** (`tests/test_model_profile_probing.py`) — a fake
   OpenAI-compatible endpoint records `supported`/`unsupported` evidence for
   streaming, tool calls, structured output, and token usage; a transient
   timeout is retried and recorded as `retry_timeout: supported`; a final
   timeout or malformed response yields a sanitized `failed` status; secrets
   (API key, custom header, query-string token) never appear in the
   persisted probe result even though the fake server confirms they were
   sent on the wire.
2. **Pinned harness execution** (`tests/test_model_harness.py`) — a worker
   executes a task through `execute_model_harness`/`run_task_worker_once`
   against a fake endpoint, records `harness.invocation_started`,
   `harness.invocation_completed`, and `harness.output_recorded` audit
   events, and maps every attempt of one task to the same deterministic
   thread id (`thread_id_for_task`). `test_unsupported_required_capability_fails_before_side_effects`
   proves a required-but-unsupported probe capability fails the run before
   any network call. `test_timeout_is_retried_and_then_succeeds` and
   `test_final_timeout_raises_harness_execution_error` cover retry and
   exhausted-retry behavior.
3. **Governed tool bridge** (`tests/test_model_harness.py`) —
   `test_harness_uses_governed_tool_bridge_with_pinned_skill_resources` shows
   a model-issued tool call is dispatched only through the pinned snapshot's
   enabled tools and skill resources, with tool arguments and results
   redacted in the audit trail.
   `test_harness_truncates_tool_output_and_ignores_untrusted_schema_fields`
   shows external MCP descriptor fields cannot smuggle policy instructions
   or unbounded output past the configured `output_limit_bytes`.
   `test_harness_tool_call_denied_by_policy_fails_before_dispatch` and
   `test_harness_tool_call_budget_hard_stop_blocks_before_dispatch` prove a
   denied policy or an exhausted hard-stop budget rejects the call before
   any `tool.invoked` side effect, recording `tool.rejected` with
   `reason_code` `policy_denied`/`budget_exhausted` and a `void` cost-ledger
   entry. `tests/test_worker.py` covers the same policy/budget/approval
   invariants for the deterministic (non-harness) execution path that the
   harness shares configuration resolution with.
4. **Evidence APIs and redaction** (`tests/test_redaction.py`,
   `tests/test_model_profile_probing.py`, `tests/test_model_harness.py`) —
   the redaction helper preserves legitimate accounting keys (e.g.
   `token_usage`) while still redacting secret-bearing keys; probe and
   harness audit/observability records expose status, request metadata, and
   token/cost evidence without leaking credentials.
5. **Frontend evidence** (`frontend/components/governance-workspace.test.tsx`,
   `frontend/components/run-evidence-panel.test.tsx`) — the model profile
   version card exercises the probe workflow end to end (unprobed →
   supported/unsupported/unknown/stale states, per-capability and pricing
   evidence, actionable diagnostics), and the run evidence panel renders
   model invocation started/completed/failed states, capability check
   failures, token usage, and tool rounds sourced from the real APIs.
6. **Restart recovery** —
   `test_restart_recovery_reuses_pinned_snapshot_and_thread_id` in
   `tests/test_model_harness.py` simulates a worker crash after a harness
   run is pinned but before the model call returns; the restarted worker
   reconciles the interrupted attempt and completes a second attempt reusing
   the same configuration snapshot and LangGraph thread id.
   `tests/test_restart_recovery.py` covers the equivalent process-kill
   demonstration for the deterministic path.

Run frontend and repository checks separately:

```bash
cd frontend
pnpm lint
pnpm typecheck
pnpm test

cd ..
PATH=/opt/homebrew/bin:$PATH ./agentic-os index check
git diff --check
```

## Manual smoke walkthrough

1. Start PostgreSQL, apply `alembic upgrade head`, then start FastAPI and the
   frontend using the root README. Set `AGENTIC_OS_MASTER_KEY` for a durable
   credential key.
2. In the operator console, create a model profile pointing at any
   OpenAI-compatible endpoint you control (a local fake or a real BYOK
   provider). Click **Probe** on the created version. Confirm the card shows
   per-capability status (streaming, tool calls, structured output, token
   usage) and pricing evidence, and that no API key or header value is ever
   rendered.
3. Create a project, goal, skill (+version), MCP server (+version) with a
   test tool, and an agent version whose `capability_manifest` sets
   `harness.required_capabilities` and references the probed model profile
   version. Grant `enabled_tools` for the MCP tool.
4. Persist a task assigned to that agent version (until a task-creation UI
   exists, use the fixtures in `backend/tests/test_model_harness.py` as the
   exact domain/API example) and run one worker iteration:
   `python -m agentic_os worker run-once --worker-id demo-worker-1`.
5. Open the task's run evidence panel. Confirm it shows
   `harness.invocation_started`/`harness.invocation_completed`, the model
   identifier, token usage, tool rounds, and — if the model issued a tool
   call — the redacted `tool.invoked` arguments/result.
6. Repeat with a required capability the profile's last probe marked
   `unsupported`. Confirm the run fails closed with
   `harness.capability_check_failed` and no model call is made.
7. Repeat with a `deny` policy on the attached MCP server, or a hard-stop
   budget below the tool's price. Confirm `tool.rejected` (`policy_denied`
   or `budget_exhausted`) appears and no `tool.invoked` event exists.
8. Start the worker with
   `AGENTIC_OS_WORKER_TEST_PAUSE_AFTER_RUN_STARTED_SECONDS=30` and a short
   lease on a new harness task; once it logs the pause, kill it
   (`kill -9 <pid>`). After the lease expires, start a fresh worker
   (`python -m agentic_os worker run-once --worker-id demo-worker-2`).
   Confirm the recovered run reuses the same LangGraph thread id and
   configuration snapshot as the interrupted attempt, and that both remain
   visible in the run evidence panel.

The process-kill mechanics are shared with
[docs/restart-recovery-verification.md](restart-recovery-verification.md);
policy/budget/approval mechanics are shared with
[docs/durable-approvals-budget-verification.md](durable-approvals-budget-verification.md).

## Interpreting failures

- **`tests.test_model_harness` fails with `ModuleNotFoundError: No module
  named 'factories'`:** `tests` is missing from `PYTHONPATH`; use
  `PYTHONPATH=src:tests`, not `PYTHONPATH=src`.
- **Model harness tests fail with `connection_error` for tests that don't
  intentionally exercise a timeout:** an earlier test in the same run raised
  before committing/rolling back its fixtures, leaving an orphaned claimable
  task whose fake server has already shut down; a later test's
  `run_task_worker_once` claims that stale task instead of its own. Run the
  failing test alone to confirm, then inspect the actually-failing test for
  an unhandled exception between task creation and its own worker call.
- **A denied policy or exhausted budget still invokes a tool through the
  harness:** the governed tool bridge's fail-closed check regressed; inspect
  `tool.rejected` audit events and `reason_code` before a `tool.invoked` row
  for the same run.
- **Probe result contains a credential value:** stop using the environment
  immediately; probe redaction is a release blocker and the credential must
  be rotated.
- **Restarted harness run gets a new LangGraph thread id:** restart-safe
  thread mapping has regressed; compare `thread_id_for_task` across both
  `Run` rows for the task.
- **Frontend probe/evidence panels stay empty:** confirm the backend probe
  and harness audit/observability endpoints return data for the run before
  debugging the frontend proxy.
