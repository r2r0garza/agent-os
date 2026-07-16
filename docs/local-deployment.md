# Local Compose deployment

The root `compose.yaml` runs Agentic OS as separate frontend, API, worker,
PostgreSQL, and sandbox-runtime roles. Telemetry is an optional profile. The
same Compose model works with Docker Compose and Podman's Docker-compatible
socket and Compose provider.

## Topology and ports

| Service | Purpose | Published port | Health evidence |
| --- | --- | --- | --- |
| `frontend` | Next.js operator console | `3000` | HTTP response from `/` |
| `api` | FastAPI versioned API | `8000` | `GET /api/v1/health` (database, migrations, artifact root, master key) |
| `worker` | Durable task polling and execution | none | `agentic-os health check --role worker` plus a successful scheduler poll marker |
| `postgres` | durable system of record | none | `pg_isready` |
| `sandbox-runtime` | worker access to the selected container engine | none | `docker info` through the mounted socket |
| `telemetry` | optional local OTLP collector | `4317`, `4318` | collector configuration validation |

Override published ports with `AGENTIC_OS_FRONTEND_PORT`,
`AGENTIC_OS_API_PORT`, `AGENTIC_OS_OTLP_GRPC_PORT`, or
`AGENTIC_OS_OTLP_HTTP_PORT`.

## Durable state

Compose creates explicit named volumes:

- `agentic-os-postgres-data` for PostgreSQL;
- `agentic-os-artifacts` for content-addressed artifact bytes;
- `agentic-os-configuration` for deployment configuration and key material;
- `agentic-os-telemetry-data` for optional telemetry state.

Stopping the stack preserves these volumes. Do not use `compose down --volumes`
unless intentionally destroying all local state. Use the operations commands
below before upgrades or any intentional volume replacement.

## Docker

Docker Desktop or Docker Engine with the Compose plugin must be running. From
the repository root:

```bash
docker compose config --quiet
docker compose up --build --wait
docker compose ps
curl --fail http://localhost:8000/api/v1/health
curl --fail http://localhost:3000
```

The default sandbox socket is `/var/run/docker.sock`. The worker receives only
that socket and the two application volumes it needs; the dedicated
`sandbox-runtime` service reports engine readiness independently.

## Podman

Start the rootless Podman API socket, then point the Compose topology at it. On
Linux the socket commonly lives below `$XDG_RUNTIME_DIR`; on macOS obtain the
forwarded socket path with `podman machine inspect`.

```bash
systemctl --user enable --now podman.socket
export CONTAINER_ENGINE_SOCKET="$XDG_RUNTIME_DIR/podman/podman.sock"
podman compose config
podman compose up --build -d
podman compose ps
```

The containerized Docker CLI speaks Podman's Docker-compatible API through the
mounted socket. This keeps the application image and Compose definition the
same for both engines. Socket access is privileged infrastructure access: use a
dedicated local operator account and never expose the API socket over an
unprotected TCP endpoint.

## Configuration and telemetry

The topology accepts these environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | `agentic_os` | local database password |
| `AGENTIC_OS_MASTER_KEY` | empty | credential encryption key for flows that store secrets |
| `AGENTIC_OS_MASTER_KEY_FILE` | `/etc/agentic-os/master.key` | durable key file read when `AGENTIC_OS_MASTER_KEY` is unset, on the `configuration` volume |
| `AGENTIC_OS_USER_ID` | empty | actor UUID forwarded by the frontend proxy |
| `CONTAINER_ENGINE_SOCKET` | `/var/run/docker.sock` | Docker or Podman API socket |
| `AGENTIC_OS_WORKSPACE_ROOT` | `/tmp/agentic-os-workspaces` | absolute host path shared at the same path for sandbox workspaces |
| `AGENTIC_OS_TELEMETRY_DISABLED` | `true` | disables telemetry SDK export in app roles |

## Configuration validation and master-key management

The `api` service runs `agentic-os config check` before applying migrations or
starting `uvicorn`. This preflight fails closed: it exits non-zero and never
starts the API if the database URL is malformed, the artifact root is not
writable, telemetry is enabled without a valid endpoint, or no master key can
be resolved from `AGENTIC_OS_MASTER_KEY` or `AGENTIC_OS_MASTER_KEY_FILE`.
Diagnostics only ever report which check failed and why; raw key or credential
material is never printed.

Before the first `docker compose up`, generate a durable master key on the
`configuration` volume so encrypted credentials survive container restarts:

```bash
docker compose run --rm api agentic-os config generate-master-key
docker compose up --build --wait
```

This writes a new Fernet key to `/etc/agentic-os/master.key` inside the
`agentic-os-configuration` volume with `0600` permissions, refusing to
overwrite an existing key unless `--force` is passed. Run
`agentic-os config check` at any time (for example
`docker compose exec api agentic-os config check`) to verify configuration
without starting a new process.

Losing the master key permanently loses access to every credential and model
API key encrypted with it; there is no recovery path other than restoring a
backup of the key material or re-entering the affected secrets. Treat it as
seriously as the PostgreSQL data volume:

- **Backup:** copy the `agentic-os-configuration` volume (or the
  `AGENTIC_OS_MASTER_KEY` value, if used instead of the file) to encrypted,
  access-controlled storage whenever you back up `agentic-os-postgres-data`.
  A key backup without its matching encrypted database backup is useless, and
  the reverse is unrecoverable.
- **Recovery:** restore the `configuration` volume (or set
  `AGENTIC_OS_MASTER_KEY` to the backed-up value) before starting the stack
  against a restored PostgreSQL backup, so encrypted rows can still be
  decrypted.
- **Rotation:** rotating the key requires decrypting every stored credential
  and model API key with the old key and re-encrypting with the new one
  before the old key material is discarded; there is no in-place rotation
  yet, so keep the retiring key available until rotation finishes.

If neither `AGENTIC_OS_MASTER_KEY` nor a key file is configured, the
application falls back to an ephemeral in-process key for local development
convenience (for example, running the backend test suite directly). This
fallback is logged as a warning and is never used by the Compose `api`
service, since its preflight check fails closed on a missing key.

Telemetry-disabled mode is the default and has no dependency on the collector.
To run the optional local collector and enable export:

```bash
AGENTIC_OS_TELEMETRY_DISABLED=false docker compose --profile telemetry up --build --wait
```

## Shutdown and diagnosis

```bash
docker compose ps
docker compose logs api worker sandbox-runtime
docker compose down
```

Each application role restarts independently. The API runs migrations only
after PostgreSQL is healthy, the worker starts only after both API and sandbox
runtime health checks pass, and the frontend starts only after the API is
healthy.

`GET /api/v1/health` fails closed: it returns `503` with a per-dependency
breakdown (`database`, `migrations`, `artifact_root`, `master_key`) whenever
any of those checks cannot be satisfied, instead of the earlier static `ok`
response, so Compose (and any dependent service's `depends_on: condition:
service_healthy`) detects a broken dependency after startup, not only during
the initial boot sequence. `agentic-os health check --role worker` runs the
same checks plus sandbox runtime availability and is what gates the worker's
own readiness marker; run `agentic-os health check --role api|worker` manually
to inspect the same evidence outside of a container healthcheck.

## Setup, migrations, backup, restore, and upgrade

Run operational commands in the API image so the PostgreSQL client version and
application migration code match the deployment:

```bash
docker compose run --rm api agentic-os operations setup-check
docker compose run --rm api agentic-os operations migrations status
docker compose run --rm api agentic-os operations migrations apply
```

Create the backup on a bind-mounted operator directory. The command creates a
new gzip archive and refuses to replace an existing file:

```bash
mkdir -p ./local-backups
docker compose run --rm \
  -v "$PWD/local-backups:/backups" \
  api agentic-os operations backup --output /backups/agentic-os-$(date +%Y%m%dT%H%M%S).tar.gz
docker compose run --rm \
  -v "$PWD/local-backups:/backups:ro" \
  api agentic-os operations verify-backup /backups/agentic-os-YYYYMMDDTHHMMSS.tar.gz
```

The archive contains a PostgreSQL custom-format dump, all artifact bytes, a
sanitized configuration description, and SHA-256 integrity evidence for every
payload. It deliberately does **not** include master-key bytes or database
credentials. Back up the matching `agentic-os-configuration` volume (or the
`AGENTIC_OS_MASTER_KEY` secret) separately to encrypted, access-controlled
storage, and label it with the backup timestamp. Never paste key material into
command output, tickets, or logs.

Restore into a clean, isolated PostgreSQL database and artifact directory
first. The command verifies every payload before invoking `pg_restore` and
refuses an active or non-empty target unless `--confirm-overwrite` is explicit:

```bash
docker compose run --rm \
  -v "$PWD/local-backups:/backups:ro" \
  -v "$PWD/local-restore-artifacts:/restore-artifacts" \
  api agentic-os operations restore /backups/agentic-os-YYYYMMDDTHHMMSS.tar.gz \
  --target-database-url 'postgresql+psycopg://agentic_os:RESTORE_PASSWORD@restore-postgres:5432/agentic_os_restore' \
  --target-artifact-root /restore-artifacts
```

Restore the matching master key through the secret channel before starting an
API or worker against the restored state. Then run `operations setup-check`
and `operations migrations status`, inspect artifacts and acknowledged goals,
and only then switch the deployment to the restored database and artifact
root. `--confirm-overwrite` adds `pg_restore --clean --if-exists` and removes
the target artifact directory; use it only after independently verifying the
target and backup names.

Before an upgrade, stop the worker from accepting new work, create and verify a
backup, then run:

```bash
docker compose run --rm api agentic-os operations upgrade-preflight
```

Apply migrations only after preflight succeeds. If the upgrade must be rolled
back, stop application services and restore the database dump, artifact bytes,
configuration, and matching master key as one recovery set before starting the
previous application image. Database rollback through Alembic downgrade is not
the supported recovery path.

Every `setup-check`, `migrations status`, `migrations apply`, `backup`,
`restore`, and `upgrade-preflight` invocation persists a non-secret evidence
record (`operations.*` audit events) to the deployment's own database when it
is reachable, so `GET /api/v1/audit-events` shows a durable history of
maintenance actions alongside goal and task activity. Persistence is
best-effort: a preflight check run before the database exists still completes
normally, it just has no audit trail to write to yet.

### Manual backup/restore smoke test

1. Start the stack, create a goal, and upload or produce an artifact.
2. Run `operations setup-check`, create a backup, and run `verify-backup`.
3. Provision an empty PostgreSQL database and empty artifact directory that are
   not used by the running stack.
4. Run `operations restore` against those isolated targets and restore the
   matching master key separately.
5. Point a stopped API/worker pair at the restored targets, run migration
   status, start them, and verify the goal, artifact bytes, audit events, and
   budget/approval state are present.
6. For the failure check, alter a copied archive payload or choose the active
   artifact root; verification or restore must exit non-zero before mutation.
