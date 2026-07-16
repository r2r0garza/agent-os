from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from agentic_os.domain.models import (
    AdminOverride,
    Budget,
    BudgetReservation,
    CostLedgerEntry,
    McpServerVersion,
    User,
)
from agentic_os.worker.policy import evaluate_policy

_DECISION_RANK = {"deny": 0, "approval_required": 1, "allow": 2}


class BudgetExhaustedError(RuntimeError):
    """Raised when a chargeable action would exceed a hard-stop budget.

    Raised before any ``CostLedgerEntry`` records the rejected amount as
    spent, and before the caller performs the action's external side effect.
    """


@dataclass(frozen=True)
class BudgetLimit:
    """Immutable budget fields copied into a run configuration snapshot."""

    id: uuid.UUID
    currency: str
    amount_minor_units: int
    enforcement_mode: str
    warning_threshold_percent: int | None = None


@dataclass(frozen=True)
class BudgetActionContext:
    """Durable ownership and attribution links for one metered action."""

    team_id: uuid.UUID
    project_id: uuid.UUID
    goal_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    agent_version_id: uuid.UUID
    requested_by: uuid.UUID | None


@dataclass(frozen=True)
class CostReservation:
    """The durable records and policy evidence created before dispatch."""

    reservation: BudgetReservation | None
    ledger_entry: CostLedgerEntry
    warning_triggered: bool
    override: AdminOverride | None


def combine_decisions(decisions: list[str]) -> str:
    """Resolve the governing decision across layered policy scopes.

    ``deny`` beats ``approval_required`` beats ``allow`` so a more permissive
    scope can never weaken a restrictive one, matching VISION.md's policy
    decision order.
    """
    if not decisions:
        return "allow"
    return min(decisions, key=lambda decision: _DECISION_RANK[decision])


def evaluate_action_policy(
    session: Session,
    *,
    agent_id: uuid.UUID,
    mcp_server_id: uuid.UUID | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Evaluate every policy scope relevant to one governed run action.

    Combines the owning agent's scope with the global ``tool`` scope (any
    standing policy restricting built-in tool execution generally) and,
    when the action is backed by an attached MCP server, that server's
    scope. The result is deterministic from persisted policy rows plus the
    supplied identifiers, so retries and restarts re-derive the same
    decision from the same request context.
    """
    evaluations: list[dict[str, Any]] = [
        {
            "scope_type": "agent",
            "scope_id": str(agent_id),
            "decision": evaluate_policy(session, scope_type="agent", scope_id=agent_id),
        },
        {
            "scope_type": "tool",
            "scope_id": None,
            "decision": evaluate_policy(session, scope_type="tool", scope_id=None),
        },
    ]
    if mcp_server_id is not None:
        evaluations.append(
            {
                "scope_type": "mcp_server",
                "scope_id": str(mcp_server_id),
                "decision": evaluate_policy(session, scope_type="mcp_server", scope_id=mcp_server_id),
            }
        )
    decision = combine_decisions([item["decision"] for item in evaluations])
    return decision, evaluations


def resolve_tool_pricing(
    mcp_server_version: McpServerVersion | None,
    tool_name: str,
    *,
    default_currency: str,
) -> tuple[int, str]:
    """Look up a tool's declared per-call price from its MCP server version.

    Pricing is optional, versioned metadata living alongside the tool's
    descriptor in ``connection_config["tools"]``. A tool with no pricing
    entry, or one not marked ``chargeable``, is non-chargeable: it still
    emits a zero-cost ledger entry elsewhere so the audit trail can
    distinguish "free" from "not measured", per VISION.md.
    """
    if mcp_server_version is None:
        return 0, default_currency
    for descriptor in (mcp_server_version.connection_config or {}).get("tools", []):
        if descriptor.get("name") != tool_name:
            continue
        pricing = descriptor.get("pricing") or {}
        if not pricing.get("chargeable"):
            return 0, default_currency
        return int(pricing.get("amount_minor_units", 0)), pricing.get("currency", default_currency)
    return 0, default_currency


def _budget_consumed_minor_units(session: Session, budget_id: uuid.UUID) -> int:
    reconciled = session.execute(
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        CostLedgerEntry.actual_amount_minor_units,
                        CostLedgerEntry.reserved_amount_minor_units,
                    )
                ),
                0,
            )
        ).where(CostLedgerEntry.budget_id == budget_id, CostLedgerEntry.status == "reconciled")
    ).scalar_one()
    active = session.execute(
        select(func.coalesce(func.sum(BudgetReservation.amount_minor_units), 0)).where(
            BudgetReservation.budget_id == budget_id,
            BudgetReservation.status == "active",
        )
    ).scalar_one()
    return int(reconciled) + int(active)


def _matching_budget_override(
    session: Session,
    *,
    context: BudgetActionContext,
    permission: str,
) -> AdminOverride | None:
    """Return an active, admin-authored override granting one budget permission."""
    now = datetime.now(timezone.utc)
    candidates = session.execute(
        select(AdminOverride)
        .join(User, User.id == AdminOverride.created_by)
        .where(
            AdminOverride.team_id == context.team_id,
            AdminOverride.starts_at <= now,
            AdminOverride.expires_at > now,
            AdminOverride.reason != "",
            User.role == "admin",
            or_(
                (AdminOverride.scope_type == "project")
                & (AdminOverride.scope_id == context.project_id),
                (AdminOverride.scope_type == "goal")
                & (AdminOverride.scope_id == context.goal_id),
                (AdminOverride.scope_type == "task")
                & (AdminOverride.scope_id == context.task_id),
                (AdminOverride.scope_type == "run")
                & (AdminOverride.scope_id == context.run_id),
            ),
        )
        .order_by(AdminOverride.created_at.desc())
    ).scalars()
    for candidate in candidates:
        budget_policy = (candidate.context or {}).get("budget")
        if isinstance(budget_policy, dict) and budget_policy.get(permission) is True:
            return candidate
    return None


def _override_evidence(override: AdminOverride | None) -> dict[str, Any] | None:
    if override is None:
        return None
    return {
        "id": str(override.id),
        "actor_id": str(override.created_by),
        "scope_type": override.scope_type,
        "scope_id": str(override.scope_id),
        "reason": override.reason,
        "expires_at": override.expires_at.isoformat(),
        "evaluated_policy_version_ids": list(override.evaluated_policy_version_ids or []),
    }


def reserve_action_cost(
    session: Session,
    *,
    budget: Budget | BudgetLimit | None,
    context: BudgetActionContext,
    action_type: str,
    amount_minor_units: int | None,
    currency: str,
    pricing_evidence: dict[str, Any] | None = None,
) -> CostReservation:
    """Reserve ledger capacity for one chargeable action before it runs.

    ``amount_minor_units=None`` means the action is metered but unpriced.
    A non-chargeable action (an explicit zero) always succeeds and is
    recorded as an explicit zero-cost entry. A hard-stop budget that
    would be exceeded raises ``BudgetExhaustedError`` before the caller's
    side effect executes. The budget row is locked until the surrounding
    transaction ends, preventing concurrent workers from both passing a
    stale capacity check. Rejected attempts remain visible as rejected
    reservations and void ledger entries.
    """
    is_unpriced = amount_minor_units is None
    amount = 0 if is_unpriced else max(int(amount_minor_units), 0)
    is_zero_cost = not is_unpriced and amount == 0
    pricing = dict(pricing_evidence or {})
    override: AdminOverride | None = None
    warning_triggered = False

    if budget is not None:
        # Lock the durable budget row even though enforcement uses immutable
        # snapshot values. This serializes capacity checks across workers.
        if session.get(Budget, budget.id, with_for_update=True) is None:
            raise BudgetExhaustedError(f"budget {budget.id} no longer exists")

        permission: str | None = None
        rejection_reason: str | None = None
        if is_unpriced and budget.enforcement_mode == "hard_stop":
            permission = "allow_unpriced"
            rejection_reason = (
                f"budget {budget.id} hard stop rejects unpriced metered action {action_type!r}"
            )
        elif not is_zero_cost and currency != budget.currency:
            permission = "allow_unpriced"
            rejection_reason = (
                f"budget {budget.id} cannot price {action_type!r} in {currency}; "
                f"budget currency is {budget.currency}"
            )

        consumed = _budget_consumed_minor_units(session, budget.id)
        projected = consumed + amount
        if (
            rejection_reason is None
            and budget.enforcement_mode == "hard_stop"
            and projected > budget.amount_minor_units
        ):
            permission = "allow_over_limit"
            rejection_reason = (
                f"budget {budget.id} hard stop: consumed={consumed} requested={amount} "
                f"limit={budget.amount_minor_units} {budget.currency}"
            )

        if rejection_reason is not None and permission is not None:
            override = _matching_budget_override(
                session, context=context, permission=permission
            )

        threshold = getattr(budget, "warning_threshold_percent", None)
        warning_triggered = bool(
            threshold is not None
            and budget.amount_minor_units >= 0
            and projected * 100 >= budget.amount_minor_units * threshold
        )

        status = "active" if rejection_reason is None or override is not None else "rejected"
        reservation = BudgetReservation(
            budget_id=budget.id,
            team_id=context.team_id,
            project_id=context.project_id,
            goal_id=context.goal_id,
            task_id=context.task_id,
            run_id=context.run_id,
            agent_version_id=context.agent_version_id,
            requested_by=context.requested_by,
            action_type=action_type,
            amount_minor_units=amount,
            currency=currency,
            status=status,
            is_unpriced=is_unpriced,
            warning_triggered=warning_triggered,
            hard_stop_triggered=rejection_reason is not None,
            pricing_evidence={
                **pricing,
                "consumed_before_minor_units": consumed,
                "projected_minor_units": projected,
                "budget_limit_minor_units": budget.amount_minor_units,
                "override": _override_evidence(override),
                "rejection_reason": rejection_reason,
            },
            policy_version_ids=(
                list(override.evaluated_policy_version_ids or []) if override else []
            ),
        )
        session.add(reservation)
        session.flush()
    else:
        reservation = None
        rejection_reason = None

    entry = CostLedgerEntry(
        budget_id=budget.id if budget else None,
        run_id=context.run_id,
        reservation_id=reservation.id if reservation else None,
        team_id=context.team_id,
        project_id=context.project_id,
        goal_id=context.goal_id,
        task_id=context.task_id,
        agent_version_id=context.agent_version_id,
        actor_id=context.requested_by,
        action_type=action_type,
        reserved_amount_minor_units=amount,
        actual_amount_minor_units=None,
        currency=currency,
        is_zero_cost=is_zero_cost,
        is_unpriced=is_unpriced,
        warning_triggered=warning_triggered,
        hard_stop_triggered=rejection_reason is not None,
        evidence={
            **pricing,
            "override": _override_evidence(override),
            "rejection_reason": rejection_reason,
        },
        status="void" if rejection_reason is not None and override is None else "reserved",
    )
    session.add(entry)
    session.flush()
    if rejection_reason is not None and override is None:
        raise BudgetExhaustedError(rejection_reason)
    return CostReservation(reservation, entry, warning_triggered, override)


def reconcile_action_cost(
    session: Session,
    cost: CostReservation,
    *,
    actual_amount_minor_units: int,
    evidence: dict[str, Any] | None = None,
) -> CostLedgerEntry:
    """Reconcile a successful action against its pessimistic reservation."""
    actual = max(int(actual_amount_minor_units), 0)
    entry = cost.ledger_entry
    entry.actual_amount_minor_units = actual
    entry.status = "reconciled"
    entry.evidence = {**(entry.evidence or {}), **(evidence or {}), "outcome": "succeeded"}
    if cost.reservation is not None:
        cost.reservation.status = "reconciled"
        cost.reservation.reconciled_at = datetime.now(timezone.utc)
    session.flush()
    return entry


def release_action_cost(
    session: Session,
    cost: CostReservation,
    *,
    reason: str,
) -> None:
    """Release capacity when dispatch definitively failed without a side effect."""
    cost.ledger_entry.status = "void"
    cost.ledger_entry.evidence = {
        **(cost.ledger_entry.evidence or {}),
        "outcome": "not_executed",
        "reason": reason,
    }
    if cost.reservation is not None:
        cost.reservation.status = "released"
        cost.reservation.reconciled_at = datetime.now(timezone.utc)
    session.flush()


def mark_action_cost_uncertain(
    session: Session,
    cost: CostReservation,
    *,
    reason: str,
) -> None:
    """Keep pessimistic capacity reserved when an external outcome is unknown."""
    cost.ledger_entry.evidence = {
        **(cost.ledger_entry.evidence or {}),
        "outcome": "uncertain_external_side_effect",
        "reason": reason,
    }
    if cost.reservation is not None:
        cost.reservation.pricing_evidence = {
            **(cost.reservation.pricing_evidence or {}),
            "outcome": "uncertain_external_side_effect",
            "reason": reason,
        }
    session.flush()
