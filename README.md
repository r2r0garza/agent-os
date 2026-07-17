# Agentic OS

Agentic OS is a local-first, self-hostable environment for durable, governed
agent work. See [VISION.md](VISION.md) for the product and architecture direction.

## Local development

Agentic OS currently has two app surfaces:

- `backend/` — Python 3.12 + FastAPI API and worker code.
- `frontend/` — Next.js/shadcn operator console.

Run backend and frontend commands in separate terminal sessions.

For the Compose-compatible Docker and Podman deployment, including its service
topology, named volumes, health checks, sandbox socket, and optional telemetry
profile, see [docs/local-deployment.md](docs/local-deployment.md).

### Backend setup

Create and activate a repository-local virtual environment, then install the
backend package with development dependencies:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

The backend defaults to PostgreSQL at:

```text
postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os
```

Override that for local development by setting `DATABASE_URL` when needed.
Some API flows that store encrypted credentials also require
`AGENTIC_OS_MASTER_KEY`.

Finalized artifact bytes use a content-addressed local durable directory. Set
`AGENTIC_OS_ARTIFACT_ROOT` to the mounted durable-volume path for the deployment;
otherwise the backend uses `~/.local/share/agentic-os/artifacts`.

Start the FastAPI development server from `backend/` with the virtual
environment active:

```bash
uvicorn agentic_os.api.app:create_app --factory --reload --host 127.0.0.1 --port 8000
```

The API is rooted under `/api/v1`; the health check is available at:

```text
http://127.0.0.1:8000/api/v1/health
```

### Frontend setup

Install frontend dependencies:

```bash
cd frontend
pnpm install
```

Start the Next.js development server from `frontend/`:

```bash
pnpm dev
```

The operator console proxies API requests to
`http://127.0.0.1:8000/api/v1` by default. If the backend is running elsewhere,
set `AGENTIC_OS_API_URL` before starting the frontend:

```bash
AGENTIC_OS_API_URL=http://127.0.0.1:8000/api/v1 pnpm dev
```

## Restart recovery verification

See [docs/restart-recovery-verification.md](docs/restart-recovery-verification.md)
for how to run the automated and manual demonstrations that a deliberate
mid-run worker process kill resumes from the last committed PostgreSQL
boundary without losing or duplicating acknowledged work.

## Governed agent configuration verification

See [docs/governed-configuration-verification.md](docs/governed-configuration-verification.md)
for the Sprint 4 automated suite and manual demonstration covering versioned
agent configuration, credential redaction, policy and budget enforcement,
pinned worker snapshots, frontend evidence, and restart continuity.

### MCP definition and credential scopes

MCP server versions contain shareable, redacted connection and tool metadata.
Credentials are granted separately through revocable team, project, or agent
attachments under
`/api/v1/mcp-servers/{server_id}/versions/{version_number}/attachments`.
Making a team-owned MCP definition `team` or `public` never grants its owner's
credential. A consuming scope must attach its own accessible credential, and
workers re-check the definition and attachment immediately before each MCP tool
side effect so visibility changes or credential revocation fail closed.

## Durable approvals and budget governance verification

See [docs/durable-approvals-budget-verification.md](docs/durable-approvals-budget-verification.md)
for the Sprint 5 automated suite and manual demonstration covering approval
interrupts, approve/deny/expire decisions, restart persistence, budget warnings
and hard stops, scoped admin overrides, frontend workflows, and evidence review.

## Multi-agent scheduling, conflict, and restart-recovery verification

See [docs/multi-agent-verification.md](docs/multi-agent-verification.md)
for how to run the automated and manual demonstrations that dependent
tasks wait, independent safe tasks run concurrently, a conflicting
resource-key pair is safely serialized, and a mid-run restart while
several multi-agent tasks are in flight recovers without losing or
duplicating acknowledged work.

## Goal lifecycle and steering verification

See
[docs/goal-lifecycle-steering-verification.md](docs/goal-lifecycle-steering-verification.md)
for the Sprint 9 automated verification and manual smoke workflow covering
pause, steering revisions, resume, cooperative and forced cancellation,
API/worker restart, backup/restore, and durable evidence inspection.

## Local operations verification

See [docs/local-operations-verification.md](docs/local-operations-verification.md)
for the Sprint 7 consolidated verification: the automated suite covering
Compose topology, configuration/master-key preflight, health evidence,
setup/migration/backup/restore/upgrade operations, and restart continuity,
plus the manual end-to-end smoke sequence (setup → submit governed goal →
restart → recover → backup → restore → preflight/upgrade) and the frontend
operational views that surface it.

## Team VM deployment topology

See [docs/team-vm-deployment.md](docs/team-vm-deployment.md) for the Sprint 10
cloud-VM/team deployment topology: how the local Compose roles map onto a
shared team VM, the TLS/proxy edge, ports, durable volume/secret boundaries,
restart ordering, and independently scaled workers that subsequent Sprint 10
issues implement against.

## Team VM deployment verification

See [docs/team-vm-verification.md](docs/team-vm-verification.md) for the
Sprint 10 consolidated verification: the automated suite covering team-mode
configuration/TLS-origin preflight, backup/restore/upgrade operations,
independently scaled worker recovery, and the admin console's deployment
health view, plus the manual VM-like smoke sequence (preflight → migrate →
governed goal → multiple workers → service restart → backup → restore →
upgrade preflight) tied back to each Sprint 10 exit criterion.

## Deterministic code index

The committed `.code-index/` covers tracked Python backend and TypeScript/TSX or
JavaScript frontend source. It extracts declarations, imports, calls, and conservative
relationships. Literal frontend HTTP calls such as `fetch("/api/goals")` are
connected to a unique matching FastAPI route when the method and path prove the
relationship.

Run commands from the repository root:

```bash
./agentic-os index build
./agentic-os index build --incremental
./agentic-os index check
./agentic-os index explain app.page.loadGoals
```

The incremental build reuses records for unchanged files, then reruns global
resolution and produces the same bytes as a clean build. `index check` performs
a clean build in a temporary directory and does not modify the worktree.

The optional pre-commit hook refreshes incrementally. It never stages files; if
the generated artifacts changed, stage them and retry the commit.

Static resolution intentionally leaves dependency-injected callables, arbitrary
receiver dispatch, reflection, and ambiguous declarations unresolved. Incoming
call results use only edges with a unique resolved symbol ID. Inspect source when
an edge is missing or unresolved.
