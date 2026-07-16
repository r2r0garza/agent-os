from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.domain.models import Budget, CostLedgerEntry, McpServerVersion
from agentic_os.worker.policy import evaluate_policy

_DECISION_RANK = {"deny": 0, "approval_required": 1, "allow": 2}


class BudgetExhaustedError(RuntimeError):
    """Raised when a chargeable action would exceed a hard-stop budget.

    Raised before any ``CostLedgerEntry`` records the rejected amount as
    spent, and before the caller performs the action's external side effect.
    """


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
    return session.execute(
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
        ).where(CostLedgerEntry.budget_id == budget_id, CostLedgerEntry.status != "void")
    ).scalar_one()


def reserve_action_cost(
    session: Session,
    *,
    budget: Budget | None,
    run_id: uuid.UUID,
    action_type: str,
    amount_minor_units: int,
    currency: str,
) -> CostLedgerEntry:
    """Reserve ledger capacity for one chargeable action before it runs.

    A non-chargeable action (``amount_minor_units <= 0``) always succeeds
    and is recorded as an explicit zero-cost entry. A hard-stop budget that
    would be exceeded raises ``BudgetExhaustedError`` before the caller's
    side effect executes; the rejected attempt is still recorded as a
    ``void`` ledger entry so operators can see what was blocked and why,
    without it counting toward consumed spend on a later attempt.
    """
    amount = max(int(amount_minor_units), 0)
    is_zero_cost = amount == 0

    if budget is not None and not is_zero_cost:
        if currency != budget.currency:
            session.add(
                CostLedgerEntry(
                    budget_id=budget.id,
                    run_id=run_id,
                    action_type=action_type,
                    reserved_amount_minor_units=amount,
                    actual_amount_minor_units=None,
                    currency=currency,
                    is_zero_cost=False,
                    status="void",
                )
            )
            session.flush()
            raise BudgetExhaustedError(
                f"budget {budget.id} cannot price {action_type!r} in {currency}; "
                f"budget currency is {budget.currency}"
            )

        consumed = _budget_consumed_minor_units(session, budget.id)
        if budget.enforcement_mode == "hard_stop" and consumed + amount > budget.amount_minor_units:
            session.add(
                CostLedgerEntry(
                    budget_id=budget.id,
                    run_id=run_id,
                    action_type=action_type,
                    reserved_amount_minor_units=amount,
                    actual_amount_minor_units=None,
                    currency=currency,
                    is_zero_cost=False,
                    status="void",
                )
            )
            session.flush()
            raise BudgetExhaustedError(
                f"budget {budget.id} hard stop: consumed={consumed} requested={amount} "
                f"limit={budget.amount_minor_units} {currency}"
            )

    entry = CostLedgerEntry(
        budget_id=budget.id if budget else None,
        run_id=run_id,
        action_type=action_type,
        reserved_amount_minor_units=amount,
        actual_amount_minor_units=amount,
        currency=currency,
        is_zero_cost=is_zero_cost,
        status="reconciled",
    )
    session.add(entry)
    session.flush()
    return entry
