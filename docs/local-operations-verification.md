# Local operations verification

This is the Sprint 7 (issue #45) consolidated verification: the local-first
deployment, secure configuration, setup/migration/backup/restore/upgrade
operations, restart continuity, and frontend operational views proven
together as one operator workflow, plus the automated suites that back each
piece. It supersedes running each Sprint 7 verification doc in isolation for
a full-slice check; the topic-specific docs it consolidates remain useful for
deep dives:

- [docs/local-deployment.md](local-deployment.md) — Compose topology,
  environment variables, and command reference (issue #40).
- Backend `tests/test_config_validation.py` and `tests/test_health.py` —
  configuration/master-key preflight and health evidence (issue #41).
- Backend `tests/test_operations.py` — setup/migration/backup/restore/upgrade
  commands (issue #42).
- Backend `tests/test_observability_api.py` (admin health/maintenance
  fields) and [docs/correlated-observability-verification.md](correlated-observability-verification.md)
  — deployment health and restart recovery evidence (issue #43).
- `frontend/components/observability-workspace.tsx` — the operator/admin
  local operations and recovery views (issue #44).

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
PYTHONPATH=src alembic upgrade head
PYTHONPATH=src python -m unittest discover -s tests -v
```

The full suite (190 tests as of this run) proves, among other things:

- `test_local_deployment.py` — the default Compose stack has separate
  healthy `api`/`frontend`/`postgres`/`sandbox-runtime`/`worker` roles with
  explicit `service_healthy` dependency ordering, telemetry disabled by
  default, and durable `postgres-data`/`artifacts`/`configuration` volumes;
  the `telemetry` profile adds the collector service and its own volume.
  This test is skipped with a clear message if the local `docker` CLI or
  Compose plugin is unavailable — it never fails silently.
- `test_config_validation.py` — `agentic-os config check` fails closed on a
  malformed database URL, a non-writable artifact root, telemetry enabled
  without a valid endpoint, or no resolvable master key; `config
  generate-master-key` writes a `0600` Fernet key and refuses to overwrite
  one without `--force`.
- `test_health.py` — `GET /api/v1/health` and `agentic-os health check
  --role api|worker` return a per-dependency breakdown (database,
  migrations, artifact root, master key, and — for the worker role —
  sandbox runtime availability) and fail closed (`503`/non-zero) rather than
  reporting a static `ok`.
- `test_operations.py` — `setup-check`, `migrations status/apply`,
  `backup`, `verify-backup`, `restore`, and `upgrade-preflight` round-trip a
  PostgreSQL dump and every artifact byte through a gzip archive with
  SHA-256 integrity evidence, never include master-key bytes or database
  credentials in the archive or in command invocations, refuse to restore
  into an active/non-empty target without `--confirm-overwrite`, and detect
  a tampered payload before any destructive step. `pg_dump`/`pg_restore`
  invocations are exercised through a subprocess seam so the suite does not
  require the PostgreSQL client binaries to be installed on the host running
  the tests — only inside the `api` container image, which the `Dockerfile`
  installs explicitly.
- `test_restart_recovery.py` — a real worker OS process killed mid-run
  leaves the run durably `"running"`, a restarted worker reconciles the
  interrupted attempt and completes a fresh one with its own idempotency
  key, and progress/audit/cost/artifact evidence remain fetchable afterward.
- `test_sandbox_docker.py` / `test_sandbox_podman.py` — the sandbox
  conformance suite runs against whichever of Docker/Podman is present and
  skips the other with a named reason (`'podman' executable not found on
  PATH` in this environment) instead of failing the run.
- `test_observability_api.py` — the admin health payload includes
  `deployment.checks`, `maintenance.commands`, and `maintenance.events` used
  by the frontend operations view.

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

## Manual end-to-end smoke sequence

Run from the repository root with Docker (or Podman, substituting `podman`
for `docker` and `podman compose` for `docker compose`) available:

1. **Setup.** Generate the durable master key, then bring up the stack:
   ```bash
   docker compose run --rm api agentic-os config generate-master-key
   docker compose up --build --wait
   docker compose ps
   curl --fail http://localhost:8000/api/v1/health
   curl --fail http://localhost:3000
   ```
   Confirm all five services report healthy and `GET /api/v1/health` returns
   `200` with every dependency `"ok"`.
2. **Submit a governed goal.** Through the operator console (or the
   versioned API directly, following `backend/tests/test_api.py` payloads),
   create a model profile, project, goal, skill/MCP/agent versions, a
   lifetime budget, and a pending task assigned to that agent version. Run
   the worker until the task completes or reaches a durable
   `waiting_approval`/`running` state.
3. **Restart continuity.** Stop the worker and API mid-run
   (`docker compose stop worker api`), then start them again
   (`docker compose start api worker`, or `docker compose up -d`). Confirm
   in the console's **Operator recovery evidence** panel (goal-scoped) that
   canonical records, checkpoint links, and durable run threads are present,
   and in **Admin observability health** (admin-scoped) that
   `deployment.checks`, database, queues, workers, sandbox, event stream,
   and telemetry tiles all resolve without mock-only state.
4. **Recover.** Confirm the task/run reaches a terminal or resumed state
   after restart with no duplicated `"completed"` runs for the same attempt
   — see `docs/restart-recovery-verification.md` for the exact evidence to
   check.
5. **Backup.**
   ```bash
   mkdir -p ./local-backups
   docker compose run --rm -v "$PWD/local-backups:/backups" \
     api agentic-os operations backup --output /backups/agentic-os-$(date +%Y%m%dT%H%M%S).tar.gz
   docker compose run --rm -v "$PWD/local-backups:/backups:ro" \
     api agentic-os operations verify-backup /backups/agentic-os-YYYYMMDDTHHMMSS.tar.gz
   ```
   Confirm the **Backup, restore, and upgrade commands** panel in the
   console shows the same command reference, and **Latest maintenance
   evidence** shows the new `operations.backup_created` /
   `operations.backup_verified` events after a refresh.
6. **Restore.** Provision an empty PostgreSQL database and empty artifact
   directory not used by the running stack, then:
   ```bash
   docker compose run --rm \
     -v "$PWD/local-backups:/backups:ro" \
     -v "$PWD/local-restore-artifacts:/restore-artifacts" \
     api agentic-os operations restore /backups/agentic-os-YYYYMMDDTHHMMSS.tar.gz \
     --target-database-url 'postgresql+psycopg://agentic_os:RESTORE_PASSWORD@restore-postgres:5432/agentic_os_restore' \
     --target-artifact-root /restore-artifacts
   ```
   Restore the matching master key through the secret channel, point a
   stopped API/worker pair at the restored database/artifact root, run
   `operations migrations status`, start them, and confirm the goal,
   artifacts, audit events, and budget/approval state from step 2 are all
   present.
7. **Preflight/upgrade check.**
   ```bash
   docker compose run --rm api agentic-os operations upgrade-preflight
   docker compose run --rm api agentic-os operations migrations status
   ```
   Confirm preflight succeeds only after the prior backup/verify steps, and
   that a new `operations.upgrade_preflight_passed` maintenance event appears
   in the console.
8. **Failure check.** Edit one byte inside a copied backup archive (or point
   `verify-backup`/`restore` at the live artifact root) and confirm the
   command exits non-zero before any destructive action — this is the
   fail-closed property `test_operations.py` also asserts.

## Local prerequisite gaps observed in this environment

- **Podman is not installed** on the host used for this verification run
  (`podman` not found on `PATH`). `test_sandbox_podman.py` skips with a
  named reason rather than failing, satisfying the "fail clearly when
  Docker/Podman/PostgreSQL prerequisites are unavailable" acceptance
  criterion; Docker coverage (`test_sandbox_docker.py`,
  `test_local_deployment.py`) ran and passed. Re-run the skipped Podman
  conformance test and the Podman half of the manual sequence on a host with
  Podman installed before treating Podman parity as independently verified.
- **No host-installed `psql`/`pg_dump`/`pg_restore`.** The automated
  `test_operations.py` suite exercises the backup/restore/verify contract
  through a mocked subprocess seam, so this does not block automated
  verification. The manual sequence above runs every PostgreSQL client
  command inside the `api` container image, which installs
  `postgresql-client-16` explicitly, so this gap does not affect the real
  deployment path either.
- **Full `docker compose up --build --wait` was not completed in this
  verification run.** The `agentic-os-api` and `agentic-os-worker` images
  built successfully in an earlier session on this host, but a fresh
  `docker compose build --pull` in this run stalled indefinitely at the
  `load metadata for docker.io/library/...` registry step (Docker Desktop
  proxy/registry connectivity was intermittent on this host at run time,
  even though host-level HTTPS egress worked). The Compose topology itself
  is proven by `test_local_deployment.py` (schema, health checks,
  dependency ordering, volumes), and every command the full stack runs is
  covered by the backend suite above; only the literal end-to-end container
  boot was not re-demonstrated in this pass. Re-run step 1 of the manual
  sequence on a host with reliable registry connectivity to close this gap.

## Interpreting failures

- **`test_local_deployment.py` skips:** the local `docker` CLI or Compose
  plugin is unavailable; install Docker Desktop or the Compose plugin and
  re-run.
- **Backend tests skip with a PostgreSQL message:** `AGENTIC_OS_DATABASE_URL`
  is unreachable; start the container shown above.
- **`operations restore` or `verify-backup` exits non-zero unexpectedly:**
  treat this as fail-closed working correctly unless the archive and target
  are both known-good; inspect the printed integrity/isolation error before
  retrying.
- **`GET /api/v1/health` returns `503`:** inspect the per-dependency
  breakdown in the response body rather than assuming a full outage; only
  the failing dependency needs remediation.
- **The console's operations/recovery panels stay empty after a real
  command ran:** verify the API can return the admin health and goal
  timeline endpoints directly before debugging the frontend proxy.
