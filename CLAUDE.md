# Repository agent instructions

## Project authority

- Treat `VISION.md` as the durable product contract and north star.
- Treat GitHub milestones as ordered vertical sprints and milestone-assigned GitHub issues as the execution queue.
- Work only inside the active open milestone unless Arturo explicitly says otherwise.
- Do not invent work outside the active milestone.

## Role split: Hermes plans, Codex executes

Hermes owns planning and queue management:

- milestone creation and closure;
- issue creation and replenishment;
- labels/tags;
- dependency and blocker metadata;
- moving issues between `blocked` and `agent-ready`.

Codex/execution agents own implementation:

- select exactly one active-milestone issue labeled `agent-ready` and not `blocked`;
- implement only that issue's bounded scope;
- verify, commit, push, and close only the implemented issue when successful.

Execution agents must not create milestones, close milestones, create issues, relabel issues, remove `blocked`, add `agent-ready`, or edit dependency metadata. If no ready issue exists, stop and hand back to Hermes.

## GitHub issue workflow

- Read the selected issue body before changing code.
- Respect `## Dependencies or blockers` metadata.
- If `Blocked by:` lists any open issue, stop rather than working around the blocker.
- Treat `Blocks:` as downstream context only; do not implement downstream issues in the same run.
- Preserve priority and area labels; queue-state label changes belong to Hermes.

## Code orientation

- Start code orientation with `.code-index/manifest.json` when present.
- Check freshness with `./agentic-os index check` before relying on the index.
- Prefer `./agentic-os index explain <qualified-name>` when a relevant symbol is known.
- The index is conservative. Inspect source whenever relationships are absent, ambiguous, unresolved, or stale.
- Refresh `.code-index/` after changing tracked source with `./agentic-os index build --incremental` when available and appropriate.

## Environment and commands

- Use repository-local tooling and environments only.
- Never install into system Python or use `--break-system-packages`.
- If a virtual environment exists, activate it before project Python commands.
- Prefer existing project scripts and commands over ad-hoc tool choices.
- Frontend-specific instructions also live in `frontend/AGENTS.md`.

## Change discipline

- Preserve unrelated user changes.
- Avoid destructive git operations unless Arturo explicitly directs them.
- Keep each implementation run focused on one issue.
- Update docs only when behavior, setup, verification, API contracts, or user/operator workflow genuinely changes.

## Verification

Before finishing implementation work:

- run verification proportional to the selected issue acceptance criteria;
- run focused tests for changed behavior;
- run frontend lint/typecheck/tests for frontend changes when available;
- run backend tests for backend changes when available;
- run `./agentic-os index check` when index-relevant code changed;
- run `git diff --check` before committing.

If verification cannot run because local dependencies or services are missing, report the blocker clearly. Do not fabricate successful verification.

## Completion report

End implementation runs with:

- active milestone;
- selected issue number/title;
- implementation summary;
- verification commands and results;
- commit hash and push status, if committed;
- issue closure status;
- branch clean/dirty state;
- any handoff needed from Hermes.
