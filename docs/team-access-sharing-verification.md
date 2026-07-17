# Team access and resource sharing verification

This checklist verifies the Sprint 8 team membership, project access grants,
agent/skill visibility and install flows, and MCP credential scoping against
the real versioned backend API. It intentionally does not use mock frontend
state, and it covers the restart/backup durability of grants and audit
evidence required by Sprint 8 exit criterion 6.

## Setup

1. Start PostgreSQL and the backend API using the repository-local development
   workflow (`docker compose up postgres api` or an equivalent local run).
2. From `frontend/`, run `pnpm dev` and open the operator console. The **Team
   membership**, **Project access grants**, **Agent & skill visibility**, and
   **MCP server credential scoping** cards live in the console's Access
   section (`components/access-workspace.tsx`).
3. Set `AGENTIC_OS_USER_ID` to an admin user's UUID (or inject
   `X-Agentic-User-ID`) to start as an admin; you will switch identities below
   to exercise regular-user and outsider paths.

## Admin creates/invites a team member

1. As an admin, open **Team membership** and confirm the installation's teams
   are listed with each member's role.
2. Add a new user to a team with the `member` role. Confirm the membership
   appears immediately with the granting admin's attribution.
3. Reload the console and confirm the new membership is refetched from the
   backend rather than held only in local state.

## Project owner grants project access

1. Switch identity to the project's creating user (its owner). Open **Project
   access grants**, select the new teammate, and grant project access.
2. Confirm the grant appears with `granted_by` set to the owner, and that the
   `project.member.granted` audit event is visible in the project's audit
   evidence.
3. As the newly granted teammate, confirm the project, its goals, tasks, runs,
   and artifacts all become visible (access is inherited, not granted
   independently per resource).
4. As a user who is a team member but has not been granted project access,
   confirm the project remains a 404 rather than a visible-but-read-only view
   (access fails closed).
5. Revoke the grant as the owner. Confirm the teammate's access to the project
   and its inherited resources is removed and the `project.member.revoked`
   event appears.

## User installs a public/team-visible definition

1. As the owner, open **Agent & skill visibility**, create an agent or skill,
   and set its visibility to `public` (or `team` to test the narrower case).
2. As a user on a different team, confirm a `public` definition is visible and
   listed cross-team, a `team` definition is visible by direct link but
   unlisted cross-team, and a `private` definition is invisible outside its
   home team.
3. Confirm cross-team read access never grants edit rights: patch, new
   version, and delete attempts from outside the home team are rejected.
4. Install the public definition from the installing team's identity. Confirm
   the install pins the source version into a new, independently governed
   definition owned by the installing team, and that neither the source owner
   can mutate the installed copy nor the installer can edit the source.

## Scoped MCP credential is attached

1. As the owner, open **MCP server credential scoping**, create an MCP server
   definition (definition metadata may be public), then create a credential
   scoped to a team, project, or agent and attach it to a server version.
2. Confirm the server/version detail never returns credential material,
   `credential_configured` reflects attachment state, and only actors with
   access to the credential's scope can attach it — attaching a credential
   from an outside scope is rejected even when the server definition itself is
   public.
3. Confirm the audit trail records `mcp.attachment.created` with
   `credential_material_redacted: true` and without the credential value.

## Denied/revoked access behaves safely

1. Confirm a denied read (outsider requesting a private project or
   definition) returns a 404 with no resource identifiers leaked in the error
   body, and that the backend records a redacted `authorization.decision`
   deny event (`redaction_evidence.resource_identifier_redacted: true`).
2. Revoke an MCP attachment. Confirm `mcp.attachment.revoked` is recorded, the
   attachment is marked `revoked` rather than deleted, and any run that would
   use it can no longer resolve the credential.
3. Confirm an admin can still read everything above regardless of team/project
   membership, and that admin-only views (installation-wide teams, users,
   observability) reject non-admin actors with 403.

## Restart preserves evidence

1. Run `pytest backend/tests/test_access_sharing_durability.py` — it seeds a
   project grant, an installed agent, an MCP credential attachment, a
   revocation, and a denied-access audit event, then rebuilds the backend app
   against the same PostgreSQL database (simulating a process restart) and
   confirms every grant, the installed definition's independent lineage, the
   redacted credential state, and the audit trail are unchanged.
2. Manually: stop and restart the API process (and the worker, if running)
   with the same `AGENTIC_OS_DATABASE_URL`. Reload the console and confirm
   team memberships, project grants, installed definitions, MCP attachments,
   and audit history are identical to before the restart.

## Backup/restore preserves evidence

1. `backend/tests/test_operations.py::MaintenanceEvidenceTests` already proves
   `create_backup`/`restore_backup` record durable, queryable
   `operations.backup_created` and `operations.restore_completed` audit
   events against the live database, and that no secret material appears in
   backup output or child-process arguments.
2. Because `pg_dump`/`pg_restore` dump and restore the entire database schema,
   the Sprint 8 tables (team membership, project access, installed-definition
   lineage, MCP server/version/attachment, credential ciphertext, and audit
   events) are included in every backup/restore by construction — there is no
   selective table exclusion in the backup path.
3. To confirm this manually with real `pg_dump`/`pg_restore` binaries (not
   available in every sandboxed execution environment — if missing, treat
   this step as a documented blocker rather than skipping it silently):
   run through the sections above to create grants, installed definitions,
   and MCP attachments; run `agentic-os operations backup --output <path>`
   against the live database; restore it with
   `agentic-os operations restore <path> --target-database-url <url>
   --target-artifact-root <dir>` into an isolated target database; and
   confirm the same rows and audit events are queryable from the restored
   target.

## Frontend checks

From `frontend/`, run:

```bash
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

`components/access-workspace.test.tsx` covers the console's team/project
grant and MCP scoping workflows against a mocked API client; the manual steps
above exercise the same UI against the real backend.

## Interpreting failures

- **Grant or membership missing after reload:** verify the console and
  backend share the same `AGENTIC_OS_DATABASE_URL`; inspect the durable
  `project.member.granted`/`revoked` events before assuming a UI regression.
- **Installed definition tracks source edits:** the install lineage broke its
  version pin; inspect `agent_installations`/`skill_installations` and the
  installed row's `agent_version_id`/`skill_version_id`.
- **Credential material appears in a response or audit payload:** treat as a
  governance release blocker; inspect `redact_mapping` usage and the
  attachment/version read models before retrying.
- **Denied request leaks a resource identifier:** inspect
  `redaction_evidence.resource_identifier_redacted` on the recorded
  `authorization.decision` event.
- **Data differs after restart or restore:** confirm both processes point at
  the same PostgreSQL URL and rerun
  `backend/tests/test_access_sharing_durability.py` to isolate whether the
  regression is in persistence or in the API/frontend read path.
