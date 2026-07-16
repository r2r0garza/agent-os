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
| `AGENTIC_OS_USER_ID` | empty | actor UUID forwarded by the frontend proxy |
| `CONTAINER_ENGINE_SOCKET` | `/var/run/docker.sock` | Docker or Podman API socket |
| `AGENTIC_OS_WORKSPACE_ROOT` | `/tmp/agentic-os-workspaces` | absolute host path shared at the same path for sandbox workspaces |
| `AGENTIC_OS_TELEMETRY_DISABLED` | `true` | disables telemetry SDK export in app roles |

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
