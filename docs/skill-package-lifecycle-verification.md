# Skill package lifecycle verification

Sprint 13 adds immutable, versioned skill packages while preserving the legacy
`content_ref` version-creation contract.

## API workflow

1. Create a skill with `POST /api/v1/skills`.
2. Author or import a package with `POST /api/v1/skills/{skill_id}/versions`.
   A package supplies:
   - a manifest with non-empty `name` and `description`;
   - non-empty instructions;
   - optional UTF-8 resources with normalized project-relative POSIX paths;
   - optional unique declared capability names;
   - optional provenance metadata.
3. Inspect immutable package, validation, provenance, and content-hash evidence
   with `GET /api/v1/skills/{skill_id}/versions/{version_number}`.
4. Export a shareable, redacted bundle with
   `GET /api/v1/skills/{skill_id}/versions/{version_number}/export`.

Invalid package requests return HTTP 422 with
`detail.code = "invalid_skill_package"` and structured diagnostics. Rejected
packages do not create a usable version. Resources are limited to 100 entries,
256 KiB each, and 1 MiB combined; instructions are limited to 256 KiB.

Export bundles omit ownership, grants, credentials, run state, and external
content references. Credential, grant, secret, and run-state fields are removed
from structured package metadata before hashing and persistence, and are
removed again when producing exports.

## Verification

From `backend/` with PostgreSQL available:

```bash
.venv/bin/pytest -q tests/test_skill_packages.py
.venv/bin/pytest -q tests/test_definition_visibility_api.py tests/test_api.py
.venv/bin/pytest -q tests/test_domain_migrations.py
```
