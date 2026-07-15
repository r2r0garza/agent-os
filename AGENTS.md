# Repository agent instructions

Start code orientation with `.code-index/manifest.json`. Check freshness with
`./agentic-os index check` before relying on the index, and prefer
`./agentic-os index explain <qualified-name>` when the symbol is known.

The index is conservative. Inspect source whenever relationships are absent,
ambiguous, or unresolved. Refresh `.code-index/` after changing tracked source
with `./agentic-os index build --incremental`.

Frontend-specific instructions also live in `frontend/AGENTS.md`.
