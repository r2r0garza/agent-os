# Team VM deployment verification

This is the Sprint 10 (issue #66) consolidated verification: team/remote
deployment preflight, backup/restore/upgrade operations, independently
scaled worker recovery, and the admin frontend operations views proven
together against each Sprint 10 exit criterion. It supersedes running each
Sprint 10 topic doc in isolation for a full-slice check; the topic-specific
docs it consolidates remain useful for deep dives:

- [docs/team-vm-deployment.md](team-vm-deployment.md) — topology, TLS/proxy
  edge, ports, durable volumes, restart ordering, and worker-scaling model
  (issues #62-#65 implement against this topology).
- Backend `tests/test_config_validation.py` (`TeamModeConfigCheckTests`,
  `TeamModePreflightTests`) — remote/team configuration, TLS public-origin,
  and secret-key validation (issue #62).
- Backend `tests/test_operations.py` (`test_backup_without_output_uses_backup_root`,
  `test_backup_without_output_rejects_remote_backup_root`,
  `test_team_mode_mentions_tls_certificate`) — team-mode setup, migration,
  backup, restore, and upgrade-preflight commands (issue #63).
- Backend `tests/test_scheduler.py`
  (`test_concurrent_worker_processes_claim_disjoint_tasks_without_duplication`,
  `test_run_scheduler_once_records_worker_heartbeat`) and
  `tests/test_observability_api.py`
  (`test_admin_health_reports_recovering_when_a_live_worker_can_reclaim_a_stale_lease`,
  `test_admin_health_reports_degraded_when_a_known_worker_stops_heartbeating`)
  — independently scaled workers and durable recovery health evidence
  (issue #64).
- `frontend/components/observability-workspace.tsx` and its test file —
  worker capacity/heartbeat counts, the `recovering` health status, and
  actionable maintenance-evidence summaries in the admin console (issue
  #65).

## Automated verification

Start a local PostgreSQL 16 instance and use the repository-local backend
environment (this run reused an existing `agentic-os-verify-pg` container
from a prior verification session rather than starting a new one):

```bash
docker run -d --name agentic-os-verify-pg \
  -e POSTGRES_USER=agentic_os -e POSTGRES_PASSWORD=agentic_os -e POSTGRES_DB=agentic_os \
  -p 5432:5432 postgres:16

cd backend
source .venv/bin/activate
export AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os
PYTHONPATH=src alembic upgrade head
PYTHONPATH=src python -m unittest discover -s tests -v
```

The full suite (284 tests as of this run, 1 skipped) proves, among other
things:

- `test_config_validation.py` — `AGENTIC_OS_DEPLOYMENT_MODE=team` requires
  `AGENTIC_OS_PUBLIC_ORIGIN` to be a valid `https://` origin and fails
  closed without it; forces mandatory master-key resolution (no ephemeral
  in-process fallback) in team mode; rejects `AGENTIC_OS_MASTER_KEY_FILE`
  and `AGENTIC_OS_ARTIFACT_ROOT` values containing a `scheme://` marker
  (object storage is not a valid location for the master key); adds
  PostgreSQL client-tool availability (`pg_dump`, `pg_restore`,
  `pg_isready`) to preflight only in team mode; and `agentic-os config
  check --json` emits structured `{"name", "ok", "detail"}` evidence with
  no raw key or credential material, matching the local `config check`
  redaction guarantee.
- `test_operations.py` — `operations backup` without an explicit `--output`
  uses `AGENTIC_OS_BACKUP_ROOT` to compute a timestamped path, fails closed
  if neither is set, and rejects a `AGENTIC_OS_BACKUP_ROOT` that looks like
  a remote/object-storage URI; `operations upgrade-preflight` in team mode
  calls out backing up the proxy's TLS certificate/private key alongside
  the database, artifact, configuration, and master-key set.
- `test_scheduler.py` — `test_concurrent_worker_processes_claim_disjoint_tasks_without_duplication`
  starts two independent `agentic-os worker run-once` OS processes (not
  in-process threads) against the same durable database and asserts
  neither claims the same task twice nor leaves a task stuck, proving the
  PostgreSQL-level fencing/advisory-lock claim design generalizes to
  independently scaled team-VM worker instances; `test_run_scheduler_once_records_worker_heartbeat`
  proves each poll cycle durably records a `worker.heartbeat` audit event
  carrying worker id and configured concurrency.
- `test_observability_api.py` — `GET /api/v1/admin/observability/health`
  aggregates worker heartbeats into `workers.capacity`,
  `workers.live_worker_ids`, `workers.missing_worker_ids`, and distinguishes
  `healthy`, `degraded` (a known worker stopped heartbeating), `recovering`
  (a stale lease exists but a live worker can reclaim it), `stale`, and
  `unavailable` states; `test_project_goal_task_and_run_timelines_redact_sensitive_evidence`
  and `test_project_ownership_and_admin_role_boundaries` prove evidence
  redaction and admin-only authorization hold for the same endpoints the
  team-deployment health view now surfaces.
- `test_local_deployment.py` — the Compose topology `team-vm-deployment.md`
  extends (same five roles, same durable volumes, same
  `service_healthy` dependency chain) is still valid; the team VM changes
  only where volumes point and adds a TLS edge, not the roles themselves.
- `test_restart_recovery.py` — a real worker OS process killed mid-run
  leaves the run durably `"running"`, and a restarted worker reconciles the
  interrupted attempt and completes a fresh one; see
  [docs/restart-recovery-verification.md](restart-recovery-verification.md)
  for the full walkthrough. This is the **service-restart** level of the
  durability contract; see the "Restart, recovery, and backup/restore
  levels" section below for how it differs from host-equivalent restart and
  backup/restore.
- `test_sandbox_docker.py` / `test_sandbox_podman.py` — the sandbox
  conformance suite runs against whichever of Docker/Podman is present.

Run frontend verification separately:

```bash
cd frontend
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

`pnpm test` includes `observability-workspace.test.tsx`, which asserts the
admin console renders worker capacity/live-heartbeat counts, the
`recovering` status, and per-command maintenance-evidence summaries (not
truncated raw JSON) sourced from real API responses.

Finally, from the repository root:

```bash
./agentic-os index build --incremental
./agentic-os index check
git diff --check
```

## Manual VM-like smoke sequence

Sprint 10 does not introduce a second Compose file or a literal multi-host
VM; `docs/team-vm-deployment.md` documents team mode as environment-variable
and TLS-edge changes layered on the same application roles `compose.yaml`
already defines. The manual sequence below therefore exercises the same
`agentic-os` CLI and API surface an operator would run on a real team VM,
against a local PostgreSQL instance standing in for the VM's durable
database:

1. **VM-like preflight, fail closed.** Generate a team-mode master key and
   artifact root in an isolated scratch directory, then run `config check`
   *without* `AGENTIC_OS_PUBLIC_ORIGIN` set:
   ```bash
   export AGENTIC_OS_DEPLOYMENT_MODE=team
   export AGENTIC_OS_ARTIFACT_ROOT=/path/to/scratch/artifacts
   export AGENTIC_OS_MASTER_KEY_FILE=/path/to/scratch/config/master.key
   export AGENTIC_OS_BACKUP_ROOT=/path/to/scratch/backups
   agentic-os config generate-master-key
   agentic-os config check --json
   ```
   Confirm the command exits non-zero and the JSON evidence names
   `public_origin` as the failing check — this run observed exactly that,
   plus a `postgres_tools` failure because this host has no installed
   `pg_dump`/`pg_restore`/`pg_isready` (see "Local prerequisite gaps"
   below).
2. **TLS/proxy origin check.** Set
   `AGENTIC_OS_PUBLIC_ORIGIN=https://team.example.com` and re-run
   `config check --json`; confirm `public_origin` now reports `ok: true`
   with the parsed hostname, and that the remaining failure is only the
   host-level `postgres_tools` gap.
3. **Secret-key material rejection.** Re-run `config check --json` with
   `AGENTIC_OS_MASTER_KEY_FILE=s3://bucket/master.key`; confirm the command
   fails closed with a `master_key` check explaining the master key must be
   a local/mounted POSIX path, never object storage — this run observed
   exactly that message and a non-zero exit before any startup step.
4. **Migrate/start.** With a valid team-mode configuration,
   `agentic-os operations migrations status` reports the applied head
   revision without requiring the PostgreSQL client tools (only
   `psycopg`), matching the "migrate, then serve" ordering
   `team-vm-deployment.md` documents for the `api` role's startup sequence.
5. **Governed goal execution.** Through the versioned API (see
   `backend/tests/test_api.py` for exact payloads), create a model
   profile, project, goal, skill/MCP/agent versions, a lifetime budget, and
   a pending task assigned to that agent version, then run the worker
   until the task completes — the same governed-execution path proven by
   `test_worker.py`, unaffected by team mode.
6. **Multiple workers.** Start two `agentic-os worker run-once` processes
   concurrently against the same database (as
   `test_concurrent_worker_processes_claim_disjoint_tasks_without_duplication`
   does programmatically); confirm via `GET
   /api/v1/admin/observability/health` that `workers.capacity` and
   `workers.live_worker_ids` reflect both instances and neither process
   reports a duplicate claim.
7. **Service restart.** Stop and restart the worker/API processes
   (`kill` then re-run the CLI, or `docker compose stop worker api` /
   `docker compose start api worker` if running the containerized stack)
   while a run is in flight; confirm the interrupted run stays durably
   `"running"`, a restarted worker reconciles it, and
   `workers.status` transitions through `recovering` before returning to
   `healthy`, per `test_admin_health_reports_recovering_when_a_live_worker_can_reclaim_a_stale_lease`.
8. **Backup.** `agentic-os operations backup` (no `--output`) writes a
   timestamped archive under `AGENTIC_OS_BACKUP_ROOT`; `agentic-os
   operations verify-backup <path>` confirms its SHA-256 integrity. Back up
   the master-key file and the proxy's TLS certificate/private key
   separately, per `team-vm-deployment.md`'s backup guidance — neither is
   ever included in the application archive.
9. **Restore into a clean environment.** Restore the archive into an
   isolated database and artifact directory
   (`agentic-os operations restore <path> --target-database-url ...
   --target-artifact-root ...`), restore the matching master key and TLS
   material through their own channels, then confirm the goal, artifacts,
   audit events, and budget state from step 5 are all present — this is
   the durable-state-survives-restore evidence the acceptance criteria
   require.
10. **Upgrade preflight.** `agentic-os operations upgrade-preflight` runs
    `setup-check` (including the team-only TLS/backup/PostgreSQL-tool
    checks) and reports migration status before any destructive step;
    confirm it only succeeds after steps 8-9 demonstrate a working
    backup/restore path, and that its `rollback` guidance names the TLS
    certificate/private key as a separate backup target in team mode.

## Restart, recovery, and backup/restore levels

Per `VISION.md`'s durability contract, this verification distinguishes
three levels rather than treating "restart" as one undifferentiated test:

- **Service restart** (steps 6-7 above, and `test_restart_recovery.py`):
  the worker/API OS process is killed and restarted while durable volumes
  and the PostgreSQL instance stay untouched. This is the level this
  verification run directly exercised end to end.
- **Host-equivalent restart**: every application service (including
  PostgreSQL) stops and restarts against the same durable volumes, as
  `docker compose stop`/`start` on the whole stack or a VM reboot would
  do. `test_local_deployment.py` proves the Compose dependency-ordering
  contract this level relies on (`postgres` healthy before `api`,
  `sandbox-runtime` healthy before `worker`, `api` healthy before
  `frontend`/`worker`); this run did not reboot the PostgreSQL container
  itself, so host-equivalent restart is proven at the topology/ordering
  level but not re-demonstrated as a live full-stack stop/start in this
  pass.
- **Backup/restore**: durable state survives being serialized to an
  archive and restored into a materially different (clean) database and
  artifact directory, not just a restarted process against the same
  volumes. Steps 8-9 above exercise this directly against the local
  PostgreSQL instance, and `test_operations.py` exercises the same
  contract (including tamper detection) through a subprocess seam that
  does not require the PostgreSQL client binaries on the test host.

## Local prerequisite gaps observed in this environment

- **No host-installed `psql`/`pg_dump`/`pg_restore`.** `agentic-os config
  check --json` correctly reports `postgres_tools: false` in team mode on
  this host, and the automated `test_operations.py` suite exercises the
  backup/restore/verify contract through a mocked subprocess seam, so this
  does not block automated verification. On a real team VM, these tools
  ship inside the `api` container image (`Dockerfile` installs
  `postgresql-client-16` explicitly), so this gap does not affect the real
  deployment path — it only means step 8-9 of the manual sequence above
  could not invoke the real `pg_dump`/`pg_restore` binaries on this
  particular host and instead rely on the container-image installation and
  the mocked-seam test coverage for that specific gap. This is the same
  gap `docs/local-operations-verification.md` recorded for Sprint 7.
- **Podman is not installed** on this host; `test_sandbox_podman.py` skips
  with a named reason. Docker coverage (`test_sandbox_docker.py`,
  `test_local_deployment.py`) ran and passed. Re-run the skipped Podman
  conformance test on a host with Podman installed before treating Podman
  parity as independently verified for the team VM.
- **No literal multi-host VM or TLS proxy was provisioned for this
  verification run.** `team-vm-deployment.md` is explicit that Sprint 10 is
  a topology/behavior slice layered on the existing single-Compose-stack
  roles, not a second deployment architecture; the manual sequence above
  therefore exercises the same CLI/API surface a real team VM would run,
  against a local stand-in database, rather than a live TLS-terminated
  reverse proxy. `AGENTIC_OS_PUBLIC_ORIGIN` validation (step 2 above)
  proves the configuration-level TLS-origin contract; it does not prove a
  live certificate/proxy chain, which has no code path in this repository
  to verify beyond the documented operator responsibility in
  `team-vm-deployment.md`.

## Interpreting failures

- **`config check --json` reports `public_origin: false` in team mode:**
  expected and correct until `AGENTIC_OS_PUBLIC_ORIGIN` is set to a valid
  `https://` origin; this is the fail-closed behavior issue #62 added, not
  a regression.
- **`config check --json` reports `postgres_tools: false`:** either the
  host genuinely lacks `pg_dump`/`pg_restore`/`pg_isready` (see the gap
  above) or the deployment is missing them from its image; on a real team
  VM this must resolve inside the `api` container before `operations
  backup`/`restore` will succeed.
- **`workers.status` stays `unavailable` after starting a worker:** no
  worker has ever heartbeated for a backlog; confirm the worker process is
  actually polling (`agentic-os worker run-once` logs) before assuming a
  regression.
- **`workers.status` stays `stale` instead of moving through
  `recovering`:** no live worker is present to reclaim the expired lease;
  start a second/replacement worker process and re-check.
- **`operations restore` or `verify-backup` exits non-zero unexpectedly:**
  treat this as fail-closed working correctly unless the archive and
  target are both known-good; inspect the printed integrity/isolation
  error before retrying.
- **The console's admin observability panel shows `capacity: 0` after
  workers ran:** verify `GET /api/v1/admin/observability/health` returns
  the expected fields directly before debugging the frontend proxy.
