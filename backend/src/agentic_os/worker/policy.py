from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.domain.models import Policy

DEFAULT_DECISION = "allow"


def evaluate_policy(session: Session, *, scope_type: str, scope_id: uuid.UUID | None) -> str:
    """Resolve the governing decision for a scope, defaulting to allow.

    An explicit deny wins over approval_required, which wins over allow,
    so the most restrictive matching policy governs.
    """
    matches = list(
        session.execute(
            select(Policy).where(Policy.scope_type == scope_type, Policy.scope_id == scope_id)
        ).scalars()
    )
    if not matches:
        return DEFAULT_DECISION
    if any(policy.decision == "deny" for policy in matches):
        return "deny"
    if any(policy.decision == "approval_required" for policy in matches):
        return "approval_required"
    return "allow"
