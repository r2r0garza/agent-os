# Local Compose deployment

The root `compose.yaml` runs Agentic OS as separate frontend, API, worker,
PostgreSQL, and sandbox-runtime roles. Telemetry is an optional profile. The
same Compose model works with Docker Compose and Podman's Docker-compatible
socket and Compose provider.

## Topology and ports

| Service | Purpose | Published port | Health evidence |
| --- | --- | --- | --- |
| `frontend` | Next.js operator console | `3000` | HTTP response from `/` |
| `api` | FastAPI versioned API | `8000` | `GET /api/v1/health` |
| `worker` | Durable task polling and execution | none | successful scheduler poll marker |
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
unless intentionally destroying all local state. Backup, restore, master-key,
and upgrade procedures are delivered by later Sprint 7 issues.

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
