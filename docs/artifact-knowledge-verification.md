# Durable artifact ingestion, lineage, and reconciliation verification

This documents the end-to-end verification of the Sprint 3 vertical slice:
staged object-storage write/finalize/reconcile behavior (including failure
modes), document ingestion, worker knowledge consumption with cited output
artifacts, and API-level artifact/lineage inspection (Sprint 3 exit
criterion 6).

## Automated harness

Coverage is spread across the backend suite rather than one file, matching
how each capability was implemented:

- `backend/tests/test_artifact_storage.py` — `LocalArtifactStorage`
  staging/finalization idempotency, hash/size mismatch rejection, orphaned
  staged-content cleanup, reconciliation of missing finalized content (with
  an `artifact.reconciliation_status_changed` audit event), reconciliation
  restoring metadata when finalized content reappears, blob de-duplication
  on retry, and cleanup of finalized content left behind by a rolled-back
  transaction.
- `backend/tests/test_artifact_ingestion.py` — deterministic Markdown
  normalization with heading spans and lineage, plain-text normalization,
  invalid UTF-8 marked `failed` without losing the source artifact, and
  missing source content surfaced as needing reconciliation.
- `backend/tests/test_api.py::ArtifactApiTests` — upload persists metadata
  and a finalized version, cross-project goal/task/run references are
  rejected (`test_upload_rejects_goal_from_a_different_project`), 404s for
  unknown artifacts, immutable version listing, content retrieval (200 when
  finalized, 409 plus an `artifact.retrieval_blocked` audit event when not),
  unsupported-format source preservation, parent/child lineage reporting,
  and kind filtering.
- `backend/tests/test_worker.py` —
  `test_worker_consumes_project_knowledge_and_publishes_cited_output` runs a
  real (non-mocked) worker attempt that reads a task's
  `knowledge_artifact_ids`, records `artifact.knowledge_consumed`, publishes
  an immutable `output` artifact whose content embeds citations back to the
  source/normalized artifacts and byte spans, and records
  `artifact.citations_recorded`; `test_worker_fails_safely_when_knowledge_artifact_is_missing`
  covers the missing-knowledge failure path;
  `test_knowledge_citations_persist_across_interrupted_retry` proves
  citations survive a crash/restart mid-run;
  `test_worker_cannot_complete_when_finalized_artifact_content_disappears`
  covers a run that depends on artifact content lost out from under it.
- `backend/tests/test_domain_migrations.py` — schema-level constraints
  backing artifact/blob/citation tables.

Run the full backend suite against a local PostgreSQL 16 instance:

```bash
docker run -d --name agentic-os-verify-pg \
  -e POSTGRES_USER=agentic_os -e POSTGRES_PASSWORD=agentic_os -e POSTGRES_DB=agentic_os \
  -p 5432:5432 postgres:16
# or: podman run -d --name agentic-os-verify-pg ... postgres:16

cd backend
source .venv/bin/activate  # or your project virtualenv
AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
  PYTHONPATH=src python -m pytest tests/ -v
```

As of this verification: 107 passed, 1 skipped (the Podman sandbox
conformance test skips with a clear message when Podman is not installed —
an environment guard, not a failure).

Frontend checks (no automated frontend test runner is configured in this
repository; lint and typecheck are the available automated checks):

```bash
cd frontend
npm run lint
npm run typecheck
```

Both pass with zero findings. Artifact inspection UI lives in
`frontend/components/artifact-workspace.tsx`, wired into
`frontend/components/operator-workspace.tsx`; it was verified manually (see
below) rather than through an automated frontend test, since no such harness
exists yet in this repository.

## Manual walkthrough

To observe the full knowledge-to-cited-output-to-reconciliation flow by
hand:

1. Start PostgreSQL as above and apply migrations, then start the API:
   ```bash
   cd backend
   AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
     alembic upgrade head
   AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
     uvicorn agentic_os.api.app:create_app --factory --host 127.0.0.1 --port 8010
   ```
2. Create a project and goal, then upload a Markdown source artifact as
   project knowledge:
   ```bash
   PROJECT=$(curl -s -X POST http://127.0.0.1:8010/api/v1/projects \
     -H 'content-type: application/json' -d '{"name":"Verify Sprint 3 Manual"}')
   PROJECT_ID=$(echo "$PROJECT" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
   GOAL=$(curl -s -X POST http://127.0.0.1:8010/api/v1/projects/$PROJECT_ID/goals \
     -H 'content-type: application/json' -d '{"title":"Summarize onboarding notes"}')
   GOAL_ID=$(echo "$GOAL" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
   SOURCE=$(curl -s -X POST http://127.0.0.1:8010/api/v1/projects/$PROJECT_ID/artifacts \
     -H 'content-type: application/json' -d '{
       "name": "onboarding.md",
       "content": "# Onboarding\n\nNew operators should read the vision doc first.\n\n## Setup\n\nRun migrations before starting the API.",
       "content_type": "text/markdown",
       "goal_id": "'"$GOAL_ID"'"
     }')
   SOURCE_ID=$(echo "$SOURCE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
   ```
   Confirm normalization: `GET /api/v1/artifacts/$SOURCE_ID/normalized`
   returns a `kind: "normalized"` artifact whose `ingestion_metadata`
   includes deterministic heading/document byte spans and links back to
   `$SOURCE_ID` via `parent_artifact_id`.
3. Wire a task to that knowledge and run it through the real worker.
   `knowledge_artifact_ids` is not yet exposed on the task-graph API, so set
   it the same way `backend/tests/test_worker.py::_build_ready_task_with_knowledge`
   does — through a short script against the domain layer (agent/skill/MCP
   version, then a `Task` with `assigned_agent_version_id` and
   `knowledge_artifact_ids=[str(SOURCE_ID)]`) — then run the CLI as a real
   OS process:
   ```bash
   AGENTIC_OS_DATABASE_URL=postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os \
     PYTHONPATH=src python -m agentic_os worker run-once --worker-id manual-demo-worker
   ```
4. Inspect the cited output artifact through the API:
   ```bash
   curl -s "http://127.0.0.1:8010/api/v1/projects/$PROJECT_ID/artifacts?task_id=$TASK_ID&kind=output"
   curl -s "http://127.0.0.1:8010/api/v1/artifacts/$OUTPUT_ID/content"
   ```
   The output artifact's JSON content embeds a `citations` array pointing
   back to `$SOURCE_ID` and its normalized artifact, with the same
   `source_byte_span`/`source_line_span` recorded during ingestion.
5. Simulate a staged-content failure: delete the output artifact's finalized
   blob directly from disk (its path is
   `LocalArtifactStorage.path_for_ref(version.storage_ref)`, rooted at
   `AGENTIC_OS_ARTIFACT_ROOT` or `~/.local/share/agentic-os/artifacts` by
   default), then retry `GET /api/v1/artifacts/$OUTPUT_ID/content` — it
   returns `409` and records an `artifact.retrieval_blocked` audit event.
6. Run `agentic_os.artifacts.reconcile_artifact_storage(session, storage)`
   (there is no CLI subcommand for this yet) — it marks the version/blob
   `"missing"` and records `artifact.reconciliation_status_changed`. Restore
   the original bytes (`storage.stage(...)` with the version's recorded
   hash/size, then `storage.finalize(...)`) and reconcile again — the
   version/blob return to `"finalized"` and content retrieval returns `200`
   again.

This exact sequence (upload → normalize → run agent task using the
knowledge → inspect cited output artifact → simulate staged-content
failure → reconcile) was run manually against a local PostgreSQL and the
real worker CLI as part of closing this issue; every step behaved as
described above.

## Interpreting failures

- **A backend test module skips**: PostgreSQL is not reachable at
  `AGENTIC_OS_DATABASE_URL` (defaults to
  `postgresql+psycopg://agentic_os:agentic_os@localhost:5432/agentic_os`).
  Start the container above and re-run.
- **`GET .../normalized` returns 409 with `ingestion_status: "unsupported"`**:
  expected for content types outside plain text/Markdown; the source
  artifact's bytes remain retrievable via `GET .../content` unchanged.
- **`GET .../content` returns 409 for a version whose `storage_state` is
  still `"finalized"` in the API response**: the on-disk blob is missing or
  hash/size-mismatched; this is the expected staged-content failure mode
  and should be followed by reconciliation, not treated as data loss — the
  metadata already recorded the finalized hash needed to verify restored
  content.
- **`reconcile_artifact_storage` reports `missing > 0` and it wasn't
  expected**: on-disk content was deleted, moved, or corrupted outside the
  storage abstraction; check `AGENTIC_OS_ARTIFACT_ROOT` points at the same
  directory the worker/API process used to write it.
- **`reconcile_artifact_storage` reports `restored == 0` after content was
  put back**: the restored bytes' hash/size did not match the version's
  recorded `content_hash`/`size_bytes` — reconciliation intentionally will
  not mark content `"finalized"` again unless it verifies byte-for-byte.
- **The worker fails a task with `KnowledgeUnavailableError`**: a task's
  `knowledge_artifact_ids` referenced an artifact whose content is not
  retrievable (deleted, wrong project, or never finalized); the task is
  marked `"failed"` rather than silently omitting the citation.
