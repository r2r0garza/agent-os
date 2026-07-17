# Governed skill/MCP capability lifecycle verification

This runbook consolidates Sprint 13 verification: versioned skill package
authoring/import/export, MCP discovery and health evidence, ownership/
visibility/install boundaries separated from agent capability grants, pinned
grant loading through the governed model harness, and the frontend workflows
that expose all of it. Each section is tied back to a numbered Sprint 13 exit
criterion.

## Automated verification

Start PostgreSQL 16 and use the repository-local backend environment:

```bash
docker run -d --name agentic-os-verify-pg \
  -e POSTGRES_USER=agentic_os -e POSTGRES_PASSWORD=agentic_os \
  -e POSTGRES_DB=agentic_os -p 5432:5432 postgres:16

cd backend
source .venv/bin/activate
export AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os
export AGENTIC_OS_MASTER_KEY=$(python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")

PYTHONPATH=src:tests python -m pytest -q \
  tests/test_skill_packages.py \
  tests/test_mcp_discovery.py \
  tests/test_capability_grants_api.py \
  tests/test_definition_visibility_api.py

PYTHONPATH=src:tests python -m pytest -q \
  tests/test_model_harness.py \
  tests/test_worker.py \
  tests/test_governance_api.py \
  tests/test_redaction.py \
  tests/test_restart_recovery.py

PYTHONPATH=src:tests python -m pytest -q tests/test_domain_migrations.py tests/test_api.py
```

The suite proves, per exit criterion:

1. **Skill package authoring/import/export** (`tests/test_skill_packages.py`,
   `docs/skill-package-lifecycle-verification.md`) — versioned packages
   persist manifests, instructions, resources, declared capabilities,
   content hashes, and provenance; malformed packages are rejected with
   HTTP 422 `detail.code = "invalid_skill_package"` before a version is
   created; export bundles are redacted of ownership, grants, credentials,
   and run-state fields.
2. **MCP discovery and health** (`tests/test_mcp_discovery.py`) —
   `test_healthy_discovery_persists_tool_evidence_and_redacts_credentials`
   persists per-tool descriptors, schema-validation status, and enablement
   defaults from a live discovery call without leaking configured
   credentials. `test_degraded_mixed_tools_records_invalid_entries_without_dropping_valid_ones`,
   `test_degraded_empty_tools_list`, `test_malformed_missing_tools_key`, and
   `test_malformed_invalid_json` prove partially or fully malformed remote
   tool lists degrade individual entries instead of trusting the whole
   response. `test_http_error_is_unreachable` and
   `test_timeout_is_unreachable_with_sanitized_diagnostics` record failure
   diagnostics without leaking secrets on the wire.
   `test_missing_url_does_not_attempt_network_call` proves discovery never
   fires against an unconfigured server.
3. **Ownership/visibility/install vs. agent grants**
   (`tests/test_capability_grants_api.py`,
   `tests/test_definition_visibility_api.py`) —
   `test_skill_resource_grant_is_validated_redacted_and_attributed` and
   `test_mcp_tool_grant_pins_descriptor_limits_and_requires_credentials`
   show a grant is a versioned, attributed record separate from the
   skill/MCP definition it points at, pinning resource paths / tool
   descriptor hashes at grant time.
   `test_skill_grant_rejects_legacy_and_unknown_resources` and
   `test_mcp_grant_rejects_disabled_missing_credential_and_revoked_access`
   prove grants fail closed against resources that do not exist, tools that
   are disabled, or servers missing required credentials.
   `test_public_mcp_install_copies_definition_without_credentials_or_authority`
   and the visibility-API install tests
   (`test_install_agent_pins_source_version_as_independent_resource`,
   `test_install_skill_pins_source_version_as_independent_resource`,
   `test_source_owner_cannot_mutate_installed_copy_and_installer_cannot_edit_source`,
   `test_installed_copy_is_immune_to_later_source_edits`) prove installing a
   public definition pins a version-independent copy that never carries
   credentials or grants, and that private/team-visible/public access rules
   are enforced independently of install/grant state.
4. **Pinned harness capability loading** (`tests/test_model_harness.py`) —
   `test_harness_snapshot_exposes_only_granted_mcp_tool_and_skill_resource`
   shows a run's configuration snapshot pins only the explicitly granted MCP
   tool subset (descriptor hash, timeout, output limit) and granted skill
   resource paths, never the full attached MCP server or skill package.
   `test_harness_tool_dispatch_fails_closed_when_granted_tool_disabled` and
   `test_harness_tool_dispatch_fails_closed_when_mcp_health_degraded` prove
   the governed tool bridge re-checks the live tool's enabled/schema-valid
   state and latest health-check status immediately before dispatch and
   rejects with `tool_disabled` / `mcp_health_degraded` reason codes if
   either has gone stale since the run was pinned, in addition to the
   existing credential/visibility/policy/budget fail-closed checks proven in
   [docs/model-harness-verification.md](model-harness-verification.md).
   `tests/test_worker.py` and `tests/test_governance_api.py` cover the same
   policy/budget/approval invariants for the deterministic execution path
   that shares configuration resolution with the harness.
   `tests/test_redaction.py` and `tests/test_restart_recovery.py` prove
   pinned grant evidence survives redaction and a worker restart without
   losing the snapshot or LangGraph thread mapping.
5. **Frontend workflows**
   (`frontend/components/capability-lifecycle-workspace.test.tsx`,
   `frontend/components/run-evidence-panel.test.tsx`) — "authors, validates,
   inspects, and exports an immutable skill package" exercises the full
   author → validate → inspect → export flow against the real API surface.
   "discovers MCP tools, saves limits, and creates explicit agent grants"
   exercises discovery, per-tool timeout/output-limit configuration, and
   agent-scoped grant creation. "covers empty, unauthorized, and retry
   states" exercises the degraded/error states discovery and grants must
   surface to an operator. "shows pinned package resources, descriptor
   hashes, and rejected tool diagnostics" in the run evidence panel renders
   granted skill resource paths, granted MCP descriptor hashes, and
   `tool.rejected` diagnostics (including `mcp_health_degraded`) sourced
   from a real run snapshot.
6. **Verification** — this document, plus the commands below, tie automated
   evidence to every exit criterion and record the manual smoke path that
   automated tests cannot exercise end to end.

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
   frontend using the root [README.md](../README.md). Set
   `AGENTIC_OS_MASTER_KEY` for a durable credential key.
2. In the operator console's capability lifecycle workspace, author or
   import a skill package version (manifest, instructions, at least one
   resource). Confirm the version shows an immutable content hash and
   validation status, then **export** it and confirm the bundle contains no
   ownership, grant, or credential fields.
3. Configure an MCP server pointing at a test MCP endpoint and run
   **discover**. Confirm discovered tools show schema-validation status and
   per-tool enable/timeout/output-limit controls, and that no discovery
   response can toggle enablement itself.
4. Create an agent version. Grant it the skill package's resources and the
   MCP server's enabled tool explicitly (grants must be created, not
   inherited from attaching the definition).
5. Persist a task assigned to that agent version and run one worker
   iteration: `python -m agentic_os worker run-once --worker-id demo-worker-1`.
6. Open the task's run evidence panel. Confirm the pinned snapshot view
   shows exactly the granted skill resource paths and MCP tool descriptor
   hashes (not the full package/server), and that a tool call renders a
   redacted `tool.invoked` entry.
7. Revoke the MCP tool grant (or disable the tool / degrade its health with
   a failing discovery run), then run a new task attempt against the same
   agent version. Confirm the call fails closed with a `tool.rejected`
   event carrying `tool_disabled` or `mcp_health_degraded`, and that no
   `tool.invoked` event exists for that attempt.
8. Repeat step 7 with a `deny` policy or an exhausted hard-stop budget
   instead, confirming `policy_denied` / `budget_exhausted` reason codes, per
   [docs/model-harness-verification.md](model-harness-verification.md).
9. Confirm restart recovery: pause a harness run mid-flight, kill the
   worker, restart it, and confirm the recovered attempt reuses the same
   pinned grant snapshot and LangGraph thread id, per
   [docs/restart-recovery-verification.md](restart-recovery-verification.md).

## Operator-facing guidance

- **Secrets and redaction.** MCP credentials and skill/MCP definition
  ownership metadata are never present in exported bundles, grant records,
  or pinned run snapshots. Discovery and health-check evidence persist
  connection diagnostics but never the credential values used to reach the
  endpoint. If a probe, export, or evidence view ever renders a credential
  value, treat it as a release blocker and rotate the credential
  immediately.
- **Provenance semantics.** A skill package version's provenance and content
  hash are immutable once created; re-authoring produces a new version
  rather than mutating history. Installing a public skill or agent pins an
  independent, version-locked copy — the source owner cannot later mutate
  an installed copy, and the installer cannot edit the source definition.
- **Fail-closed MCP behavior.** Tool descriptions and schemas returned by an
  MCP server are untrusted evidence, not policy authority: they can degrade
  a tool's discovered status but can never enable a tool, grant a
  capability, or override an installation/team/project policy decision. A
  grant only authorizes what was explicitly pinned at grant time, and the
  harness re-verifies the live tool/credential/health state immediately
  before every dispatch, so revoking a grant or degrading a server's health
  takes effect on the next call rather than only at snapshot time.

## Interpreting failures

- **`tests.test_model_harness` fails with `ModuleNotFoundError: No module
  named 'factories'`:** `tests` is missing from `PYTHONPATH`; use
  `PYTHONPATH=src:tests`, not `PYTHONPATH=src`.
- **A grant loads the full MCP server or skill package instead of the
  pinned subset:** the governed harness snapshot builder in
  `backend/src/agentic_os/worker/configuration.py` regressed; inspect
  `run.snapshot["mcp_tool_grants"]` / `run.snapshot["skill_resource_grants"]`
  against the grant records for the run's agent version.
- **A disabled tool or degraded MCP server still dispatches through the
  harness:** the pre-dispatch re-check in
  `backend/src/agentic_os/worker/tool_bridge.py` regressed; inspect
  `tool.rejected` events and `reason_code` before any `tool.invoked` row for
  the same run.
- **An installed public definition changes when its source is edited:** the
  install boundary in the visibility API regressed; installed copies must
  be independent resources, not references.
- **Export bundle or grant record contains a credential value:** stop using
  the environment immediately; this is a release blocker and the credential
  must be rotated.
- **`./agentic-os index check` reports stale after a doc-only change:**
  confirm no tracked source file changed; if the manifest still drifts, run
  `PATH=/opt/homebrew/bin:$PATH ./agentic-os index build --incremental` and
  re-check before committing.
