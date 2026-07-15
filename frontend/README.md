# Next.js template

This is a Next.js template with shadcn/ui.

## Foundation workflow

The operator console proxies its requests to the versioned FastAPI backend. Set
`AGENTIC_OS_API_URL` when the backend is not available at the default
`http://127.0.0.1:8000/api/v1` URL.

Manual smoke check:

1. Start PostgreSQL and the backend API, then start this app with `pnpm dev`.
2. Create a model profile, project, and goal from the console.
3. Provision the skill, test MCP server, lifetime budget, and agent version.
4. Run the repository worker against a task for that goal and confirm the console
   shows run status, tool/audit activity, cost entries, and the result artifact.
5. Reload the browser and confirm the selected persisted project/goal and their
   state are refetched from the API.
6. Stop the worker during an active run, restart it, and confirm the console shows
   the durable recoverable state before displaying the resumed history.

## Adding components

To add components to your app, run the following command:

```bash
npx shadcn@latest add button
```

This will place the ui components in the `components` directory.

## Using components

To use the components in your app, import them as follows:

```tsx
import { Button } from "@/components/ui/button";
```
