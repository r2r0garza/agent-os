# Next.js template

This is a Next.js template with shadcn/ui.

## Foundation workflow

The operator console proxies its requests to the versioned FastAPI backend. Set
`AGENTIC_OS_API_URL` when the backend is not available at the default
`http://127.0.0.1:8000/api/v1` URL.

For local role/access smoke checks, set `AGENTIC_OS_USER_ID` before starting the
frontend. The API proxy forwards that actor UUID as `X-Agentic-User-ID`.

Manual smoke check:

For the complete governed-configuration setup, restart, enforcement, and
evidence checklist, see
[`docs/governed-configuration-verification.md`](../docs/governed-configuration-verification.md).

For durable approval decisions, admin overrides, authorization failures, budget
evidence, recovery, and reload persistence, see
[`docs/governance-operations-verification.md`](../docs/governance-operations-verification.md).

For correlated goal/run timelines, trace and canonical evidence links, telemetry
delivery/capture states, admin health, authorization, and recovery checks, see
[`docs/correlated-observability-verification.md`](../docs/correlated-observability-verification.md).

1. Start PostgreSQL and the backend API, then start this app with `pnpm dev`.
2. Create a model profile, project, and goal from the console.
3. Provision the skill, test MCP server, lifetime budget, and agent version.
4. Run the repository worker against a task for that goal and confirm the console
   shows run status, tool/audit activity, cost entries, and the result artifact.
5. Reload the browser and confirm the selected persisted project/goal and their
   state are refetched from the API.
6. Stop the worker during an active run, restart it, and confirm the console shows
   the durable recoverable state before displaying the resumed history.

Artifact and project-knowledge smoke check:

1. Select a persisted project, upload a `.txt` or `.md` source in **Project
   knowledge**, and confirm both the source and normalized artifacts appear with
   finalized version, hash, size, content type, and ingestion metadata.
2. Select each artifact and confirm the inspector loads content and source →
   normalized lineage from the versioned backend APIs. Reload the browser and
   confirm the same artifacts and detail remain available.
3. Run a task configured with the uploaded source artifact as project knowledge.
   Select its output artifact and confirm the citation names both the source and
   normalized artifact and displays its immutable citation anchor.
4. Upload content using **Unsupported format smoke check** and confirm the source
   remains downloadable while its `unsupported` ingestion state is prominent.
5. To verify reconciliation messaging, mark or simulate an artifact version as
   non-finalized with the backend verification fixture, select it, and confirm the
   inspector explains that content is unavailable and instructs the operator to
   reconcile storage and refresh.

## Adding components

To add components to your app, run the following command:

```bash
npx shadcn@latest add button
```

This will place the ui components in the `components` directory.

## Using components

To use the components in your app, import them as follows:

```tsx
import { Button } from "@/components/ui/button"
```
