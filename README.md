# Agentic OS

Agentic OS is a local-first, self-hostable environment for durable, governed
agent work. See [VISION.md](VISION.md) for the product and architecture direction.

## Restart recovery verification

See [docs/restart-recovery-verification.md](docs/restart-recovery-verification.md)
for how to run the automated and manual demonstrations that a deliberate
mid-run worker process kill resumes from the last committed PostgreSQL
boundary without losing or duplicating acknowledged work.

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
