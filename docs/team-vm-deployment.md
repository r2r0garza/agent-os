# Team VM deployment topology

Sprint 10 extends the [local Compose deployment](local-deployment.md) to a
single cloud VM that a trusted small team shares. This document defines the
topology, TLS/proxy responsibilities, ports, durable volumes, restart
ordering, and security assumptions that Sprint 10 issues (#62-#66) implement
against, and documents the resulting configuration validation (#62) and
setup/migration/backup/restore/upgrade operator commands (#63) as those land.
It does not add frontend views (#65) or worker-scaling behavior (#64)
themselves; those remain separate, dependent issues.

## Scope and non-goals

This is a **documentation and topology-definition slice**. It reuses the
existing Agentic OS application roles, domain model, and durable stores
exactly as the local deployment defines them (see
[local-deployment.md](local-deployment.md) and `compose.yaml`). It does not:

- introduce a second deployment architecture, a different persistence model,
  or new application roles;
- implement preflight, setup, backup/restore, upgrade, or worker-scaling
  commands (Sprint 10 issues #62-#64 implement those against this topology);
- implement frontend/admin operational health views (#65);
- provide Kubernetes, multi-region, autoscaling, or managed-cloud
  control-plane automation. A team VM is one host (or one host plus a managed
  PostgreSQL/object-storage endpoint) running the same Compose-modeled roles,
  not an orchestrated cluster.

## Roles reused from the local topology

The team VM runs the same roles as `compose.yaml`, unchanged in responsibility:

| Role | Local Compose service | Team VM change |
| --- | --- | --- |
| Frontend | `frontend` | Same image/process; no longer publishes a host port directly, sits behind the TLS edge instead |
| API | `api` | Same image/process; same `/api/v1/health` contract; no longer publishes a host port directly |
| Worker | `worker` | Same image/process; team VM allows more than one worker instance (see "Independently scaled workers") |
| PostgreSQL | `postgres` | Same engine and schema; durable volume becomes a cloud block volume (or a managed PostgreSQL endpoint reachable over a private network) |
| Artifact storage | `artifacts` volume | Same content-addressed layout; durable volume becomes a cloud block volume or an S3-compatible bucket per `VISION.md`'s object-storage abstraction |
| Sandbox runtime | `sandbox-runtime` | Same Docker/Podman-compatible socket contract; the controller-only access rule from `VISION.md` applies unchanged |
| Telemetry | `telemetry` (optional profile) | Same optional OTLP collector; disabled by default, as in local |

Nothing here changes `AGENTIC_OS_DATABASE_URL`, `AGENTIC_OS_ARTIFACT_ROOT`,
`AGENTIC_OS_MASTER_KEY`/`AGENTIC_OS_MASTER_KEY_FILE`, or
`CONTAINER_ENGINE_SOCKET` semantics from
[local-deployment.md](local-deployment.md#configuration-and-telemetry); the
team VM only changes where those values point (durable cloud volumes/managed
services instead of local Docker volumes) and adds a TLS edge in front of the
`frontend` and `api` ports.

## Network boundary and TLS edge

A reverse proxy (nginx, Caddy, or an equivalent TLS terminator) is the only
service exposed to the public internet:

- The proxy terminates TLS using a certificate for the team's domain
  (operator-supplied certificate or an ACME-issued one) and forwards plaintext
  HTTP to `frontend` (port `3000`) and `api` (port `8000`) over the private
  VM-internal network.
- `frontend`, `api`, `worker`, `postgres`, `sandbox-runtime`, and `telemetry`
  bind only to the VM-internal network (a Compose/Podman network or
  `127.0.0.1`/private-interface bindings) and are never published on a public
  interface. This mirrors the existing rule that only the sandbox controller
  may reach the container engine API; the team VM adds "only the proxy may
  reach application ports from outside the host" as a parallel boundary.
- The proxy forwards `/api/*` to the `api` service and everything else to
  `frontend`, matching the existing `frontend` Next.js API proxy route
  (`frontend/app/api/agentic/[...path]/route.ts`) that already forwards to
  `AGENTIC_OS_API_URL`.
- Health endpoints (`GET /api/v1/health` and the worker's readiness marker)
  remain reachable only from inside the VM-internal network or through an
  operator SSH tunnel; they are not proxied to the public internet.
- Operators reach PostgreSQL, the artifact volume, and the container engine
  socket only through SSH/VM access, never through the public proxy.

This preserves the local deployment's assumption that only `frontend` and
`api` are network-reachable application entry points, and adds the TLS edge as
the single new public-facing component.

## Ports

| Service | Bind | Purpose |
| --- | --- | --- |
| proxy | `0.0.0.0:443` (public), `0.0.0.0:80` (redirect to 443 or ACME challenge) | TLS termination, the only public entry point |
| `frontend` | private network `3000` | Next.js operator console (same as local) |
| `api` | private network `8000` | FastAPI versioned API and `/api/v1/health` (same as local) |
| `worker` (one or more) | none | durable task polling; no inbound port, same as local |
| `postgres` | private network `5432`, or a managed PostgreSQL endpoint reachable only from the VM's private network/VPC | durable system of record |
| `sandbox-runtime` | private network only (Unix socket, not TCP) | worker access to the container engine, same contract as local |
| `telemetry` (optional) | private network `4317`/`4318` | optional OTLP collector, same as local |

`AGENTIC_OS_FRONTEND_PORT`, `AGENTIC_OS_API_PORT`,
`AGENTIC_OS_OTLP_GRPC_PORT`, and `AGENTIC_OS_OTLP_HTTP_PORT` keep their local
meaning as the internal ports the proxy forwards to; they are not published to
the public interface on the team VM.

## Durable volumes and persistent paths

The team VM keeps the same four durable stores `compose.yaml` names, backed by
cloud-durable storage instead of a host-local Docker volume:

| Local volume | Team VM equivalent | Persistent path inside the role |
| --- | --- | --- |
| `agentic-os-postgres-data` | Attached cloud block volume, or a managed PostgreSQL instance's own durable storage | PostgreSQL data directory |
| `agentic-os-artifacts` | Attached cloud block volume, or an S3-compatible bucket per `VISION.md`'s object-storage abstraction | `/var/lib/agentic-os/artifacts` |
| `agentic-os-configuration` | Attached cloud block volume (never object storage, since it holds the master-key file) | `/etc/agentic-os`, including `AGENTIC_OS_MASTER_KEY_FILE` |
| `agentic-os-telemetry-data` | Attached cloud block volume (only if the optional `telemetry` profile is enabled) | collector working state |

Rules that carry over unchanged from local deployment:

- The `configuration` volume/path is the one place the master key lives at
  rest; it must never be an S3-compatible bucket, since the key material
  needs POSIX file permissions (`0600`) rather than object-storage ACLs.
- The `artifacts` path may move to an S3-compatible backend (per
  `VISION.md`'s "local durable volume initially, S3-compatible backend when
  deployed for a team"); PostgreSQL remains authoritative for artifact
  metadata, hashes, and lineage regardless of which artifact backend is
  active.
- Stopping or restarting VM services must never implicitly delete these
  volumes; only an explicit operator action (Sprint 10 issue #63's
  operations commands) does so.

## Restart ordering and startup dependencies

Restart ordering matches the local deployment's `depends_on`/healthcheck
chain, applied at the VM/process-supervisor level (systemd, Compose on the
VM, or an equivalent):

1. `postgres` starts and must pass `pg_isready` before `api` starts.
2. `api` runs `agentic-os config check`, then applies migrations, then starts
   serving; `GET /api/v1/health` must report all dependencies healthy before
   `worker` or `frontend` start.
3. `sandbox-runtime` must report `docker info` success before any `worker`
   starts.
4. Each `worker` starts only after both `api` health and `sandbox-runtime`
   health pass, matching the local `depends_on: condition: service_healthy`
   chain.
5. `frontend` starts only after `api` health passes.
6. The proxy starts independently and simply retries upstream connections
   until `frontend`/`api` become reachable; it does not gate other services.

A host restart (VM reboot) must bring services back in this same order from
the durable volumes; this is the process-restart-with-durable-volumes level of
the durability contract in `VISION.md`, and is the level Sprint 10 issue #66
verifies end to end.

## Independently scaled workers

The local deployment already models `worker` as its own Compose service, so
the team VM does not need a new role — it needs to allow more than one
instance of the same role:

- Each `worker` instance keeps its own `AGENTIC_OS_WORKER_ID` (as `compose.yaml`
  already sets for the single local worker), so lease and heartbeat state
  (`agentic_os.worker.leases`) can distinguish instances.
- All `worker` instances share the same `postgres` system of record, the same
  `artifacts` store, and the same `configuration` volume (master key), so
  scaling workers never creates a second source of truth.
- All `worker` instances reach the sandbox runtime through the same
  controller contract (`sandbox-runtime` service or host-level engine socket);
  the controller-only access rule from `VISION.md` applies per worker
  instance, not just once for the VM.
- Worker count is an operational scaling decision (how many `worker` processes
  the VM or process supervisor runs), not a topology change; Sprint 10 issue
  #64 implements the recovery-health evidence that proves multiple workers
  recover leased work correctly after restart.

## Security assumptions and secrets

Security assumptions the team VM adds on top of the local deployment's
existing ones:

- The proxy's TLS private key is the only new secret class introduced by this
  topology; it is stored with restrictive file permissions on the
  `configuration` volume or the proxy's own configuration directory, never in
  the application's `configuration` volume alongside the master key.
- SSH access to the VM is the operator's administrative boundary; only
  operators with SSH access can reach PostgreSQL, the artifact volume,
  `docker`/`podman` CLI, and the master-key file directly. This is the cloud
  equivalent of the local rule that "socket access is privileged
  infrastructure access."
- The container engine API (Docker or Podman) must not be exposed on a TCP
  port reachable from outside the VM's private network; the team VM keeps the
  local deployment's Unix-socket-only access pattern.
- Firewall/security-group rules must only allow inbound `443`/`80` (proxy) and
  operator SSH from the internet; every other port in the "Ports" table above
  is private-network-only.
- What must be preserved for backup and restore, matching and extending
  [local-deployment.md](local-deployment.md#configuration-and-telemetry):
  - the PostgreSQL data (via `agentic-os operations backup`, once #63 exists);
  - the artifact bytes (same command);
  - the `configuration` volume's master-key file, backed up separately from
    the application backup archive exactly as local deployment already
    requires, since the archive deliberately excludes key material;
  - the proxy's TLS certificate and private key, so the edge can be restored
    without re-issuing certificates;
  - any environment/secret values injected only at VM-provisioning time
    (e.g., `POSTGRES_PASSWORD`, database connection strings for a managed
    PostgreSQL instance) that are not stored inside the application volumes.
- Losing the master key has the same unrecoverable consequence documented in
  [local-deployment.md](local-deployment.md#configuration-and-telemetry): there
  is no in-place recovery other than restoring a backed-up key alongside its
  matching encrypted database backup.

## Local-first compatibility

Local Compose/Podman deployment remains the reference base and is unaffected
by this topology:

- The same container images, environment variable names, health checks, and
  Alembic migrations run on the team VM as run locally; only the network
  edge, volume backend, and worker count change.
- `docker compose config --quiet` (or the Podman equivalent) against a
  team-VM-flavored Compose override remains the way to validate topology
  changes before applying them, matching the local verification pattern in
  [local-deployment.md](local-deployment.md#docker).
- Nothing in this topology requires a different database schema, a different
  artifact metadata model, or a different domain model; it satisfies
  `VISION.md`'s requirement that moving from local to team deployment "must
  not require changing domain models or abandoning stored work."

## Remote configuration and secret-key validation (#62)

`backend/src/agentic_os/config.py` extends the local preflight (`agentic-os
config check`) with team-mode checks so an unsafe team VM deployment fails
closed before startup or upgrade, matching the local deployment's existing
fail-closed pattern:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTIC_OS_DEPLOYMENT_MODE` | `local` | `local` or `team`; `team` enables the checks below and forces the master key to be required (no ephemeral in-process fallback) |
| `AGENTIC_OS_PUBLIC_ORIGIN` | unset | The proxy's TLS-terminated public origin (for example `https://team.example.com`); required and must be `https://` with a hostname in `team` mode |
| `AGENTIC_OS_BACKUP_ROOT` | unset | Optional durable local/mounted directory operators point `operations backup --output` at; validated for writability and rejected if it looks like a remote/object-storage URI |

Additional fail-closed behavior added for team mode:

- `AGENTIC_OS_MASTER_KEY_FILE` and `AGENTIC_OS_ARTIFACT_ROOT` are rejected
  outright if they contain a `scheme://` marker (for example `s3://...`),
  since the master key must live on a POSIX-permissioned durable volume and
  artifact object-storage backends are not implemented yet — this matches the
  "configuration volume can never be object storage" rule above.
- `agentic-os config check` in `team` mode also validates PostgreSQL
  client-tool availability (`pg_dump`, `pg_restore`, `pg_isready`) as part of
  preflight, not only during `operations setup-check`.
- `agentic-os config check --json` prints the same evidence as structured
  JSON (`[{"name", "ok", "detail"}, ...]`) for operations commands and the
  future admin frontend view (#65) to consume; it never includes raw key or
  credential material, matching the existing text report's redaction
  guarantee.

Existing local deployment defaults are unaffected: `AGENTIC_OS_DEPLOYMENT_MODE`
unset (or `local`) skips every team-only check and preserves prior `config
check` behavior exactly.

## Operator commands (#63)

The same `agentic-os operations` and `agentic-os config` commands documented
in [local-deployment.md](local-deployment.md#setup-migrations-backup-restore-and-upgrade)
prepare, preflight, migrate, back up, restore, and upgrade-check a team VM
deployment. Nothing here introduces a second command surface: team mode only
changes which environment variables are set and which checks `run_preflight`
adds, per [config.py](../backend/src/agentic_os/config.py) and the
["Remote configuration and secret-key validation" section](#remote-configuration-and-secret-key-validation-62)
above.

Set team-mode environment once per VM (systemd `EnvironmentFile`, a Compose
`.env` file, or the process supervisor's own mechanism), then run commands
over SSH or through the process supervisor exactly as locally:

```bash
export AGENTIC_OS_DEPLOYMENT_MODE=team
export AGENTIC_OS_PUBLIC_ORIGIN=https://team.example.com
export AGENTIC_OS_BACKUP_ROOT=/var/backups/agentic-os
agentic-os config check          # fails closed on TLS origin, master key, backup destination
agentic-os operations setup-check
agentic-os operations migrations status
agentic-os operations migrations apply
```

### Backup and restore

`AGENTIC_OS_BACKUP_ROOT` is validated for writability by `config check` in
team mode; `operations backup` now consumes it directly so operators do not
have to compute a path by hand. Passing `--output` explicitly still works and
takes priority:

```bash
agentic-os operations backup
# -> writes /var/backups/agentic-os/agentic-os-<UTC timestamp>.tar.gz
agentic-os operations verify-backup /var/backups/agentic-os/agentic-os-20260101T000000Z.tar.gz
```

The archive contains the same PostgreSQL dump, artifact bytes, sanitized
configuration description (including whether telemetry export is disabled),
and SHA-256 integrity evidence as the local deployment; it never includes
master-key bytes or database credentials. On a team VM, back up these three
things separately, to encrypted access-controlled storage, alongside every
archive:

- the `configuration` volume's master-key file (same as local deployment);
- the proxy's TLS certificate and private key (new for the team VM; the proxy
  is not an application role, so its key material is never part of the
  application backup archive);
- any provisioning-time secret not stored inside the application volumes
  (for example a managed PostgreSQL connection string), per the "Security
  assumptions and secrets" section above.

Restore into an isolated database and artifact directory first, exactly as
[local-deployment.md](local-deployment.md#setup-migrations-backup-restore-and-upgrade)
documents, then restore the matching master key and TLS material through
their own channels before pointing the VM's services at the restored state:

```bash
agentic-os operations restore /var/backups/agentic-os/agentic-os-20260101T000000Z.tar.gz \
  --target-database-url "$RESTORE_DATABASE_URL" \
  --target-artifact-root /var/lib/agentic-os/restore-artifacts
```

### Upgrade preflight and rollback

```bash
agentic-os operations upgrade-preflight
```

This runs `setup-check` (including the team-mode TLS/backup/PostgreSQL-tool
checks) and reports migration status before any destructive step; a
non-`team` deployment mode is rejected earlier by `config check` if
`AGENTIC_OS_DEPLOYMENT_MODE` is unset when team-only environment variables are
present, so upgrade-preflight never reports success on an unvalidated
configuration. Only apply migrations after preflight succeeds. The command's
`rollback` field in team mode explicitly calls out backing up and restoring
the proxy's TLS certificate and private key alongside the database, artifact,
configuration, and master-key set; database rollback through an Alembic
downgrade is not the supported recovery path on the team VM either.

### Service lifecycle (manual service-manager or shell integration)

This topology intentionally names no cloud provider or orchestrator. Operators
start, stop, and restart the roles from the ["Restart ordering and startup
dependencies"](#restart-ordering-and-startup-dependencies) section above using
whatever process supervisor already manages the VM (systemd, a process
manager, or Compose/Podman running directly on the VM). A systemd-flavored
sketch, using target units to express the same ordering as the local
`depends_on` chain:

```ini
# /etc/systemd/system/agentic-os-api.service
[Unit]
Description=Agentic OS API
After=agentic-os-postgres.service
Requires=agentic-os-postgres.service

[Service]
EnvironmentFile=/etc/agentic-os/team.env
ExecStartPre=/usr/local/bin/agentic-os config check
ExecStartPre=/usr/local/bin/agentic-os operations migrations apply
ExecStart=/usr/local/bin/uvicorn agentic_os.app:app --host 127.0.0.1 --port 8000
Restart=on-failure

# /etc/systemd/system/agentic-os-worker.service
[Unit]
Description=Agentic OS worker
After=agentic-os-api.service agentic-os-sandbox-runtime.service
Requires=agentic-os-api.service agentic-os-sandbox-runtime.service

[Service]
EnvironmentFile=/etc/agentic-os/team.env
ExecStart=/usr/local/bin/agentic-os worker run-once --workers 1
Restart=on-failure
```

Operators equally may keep running Compose/Podman directly on the VM (a
`docker compose -f compose.yaml -f compose.team.yaml up -d` style override
that removes published host ports and points volumes at the durable paths in
the "Durable volumes and persistent paths" section) instead of systemd units;
either integration point satisfies this topology as long as it preserves the
restart order above and never publishes application ports outside the proxy.

Stopping services (`systemctl stop agentic-os-worker agentic-os-api` or
`docker compose stop`) never deletes the durable volumes; only an explicit
`operations backup`/`restore` or manual volume deletion does.

## What subsequent issues build on this

- **#62** validates remote configuration, TLS/proxy settings, and secret-key
  material against the assumptions above (private-network-only application
  ports, master key never in object storage, TLS key stored separately).
- **#63** adds the setup, migration, backup, restore, and upgrade operations
  commands that operate against the durable volumes and restart ordering
  defined here.
- **#64** implements independently scaled `worker` instances and durable
  recovery health evidence using the worker-scaling model above.
- **#65** exposes team-deployment health, TLS/config/preflight problems, and
  worker capacity in the frontend/admin surface, backed by real APIs rather
  than mock state.
- **#66** verifies the full team VM deployment, backup/restore, upgrade,
  scaling, and restart recovery end to end against this topology.
