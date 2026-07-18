from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.domain.assignment import match_capabilities
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AgentVersionMcpServer,
    AgentVersionSkill,
    Budget,
    CostLedgerEntry,
    Goal,
    McpServerHealthCheck,
    McpServerTool,
    McpServerVersion,
    ModelProfile,
    ModelProfileVersion,
    Project,
    SkillVersion,
)
from agentic_os.worker.policy import evaluate_policy


def latest_team_agent_versions(
    session: Session, team_id: uuid.UUID
) -> list[tuple[Agent, AgentVersion]]:
    """Return one latest version per team agent in a deterministic order."""
    agents = list(
        session.execute(
            select(Agent)
            .where(Agent.team_id == team_id)
            .order_by(Agent.created_at, Agent.id)
        ).scalars()
    )
    versions: list[tuple[Agent, AgentVersion]] = []
    for agent in agents:
        version = session.execute(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent.id)
            .order_by(AgentVersion.version_number.desc(), AgentVersion.id)
            .limit(1)
        ).scalar_one_or_none()
        if version is not None:
            versions.append((agent, version))
    return versions


def evaluate_planning_candidate(
    session: Session,
    *,
    project: Project,
    goal: Goal,
    agent: Agent,
    version: AgentVersion,
    required_capabilities: dict[str, bool],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate explicit capability and governance evidence for one agent version."""
    declared_capabilities, skill_evidence = _declared_capabilities(session, version)
    matched, missing = match_capabilities(
        required_capabilities,
        {"capabilities": sorted(declared_capabilities)},
    )
    rejection_reasons = [f"missing_capability:{name}" for name in missing]

    policy_evidence = _policy_evidence(
        session,
        project=project,
        goal=goal,
        agent=agent,
    )
    for item in policy_evidence:
        if item["decision"] in {"deny", "approval_required"}:
            rejection_reasons.append(
                f"{item['scope_type']}_policy_{item['decision']}:{item['scope_id'] or 'installation'}"
            )

    budget, budget_source, budget_reasons, consumed = _budget_evidence(
        session,
        agent=agent,
        version=version,
        constraints=constraints,
    )
    rejection_reasons.extend(budget_reasons)

    tool_evidence = []
    for tool_name in sorted(_string_set(constraints.get("required_tools"))):
        evidence, reasons = _tool_evidence(
            session,
            version=version,
            tool_name=tool_name,
        )
        tool_evidence.append(evidence)
        rejection_reasons.extend(reasons)

    model_capabilities, model_evidence = _model_evidence(session, version)
    for capability_name in sorted(
        _string_set(constraints.get("required_model_capabilities"))
    ):
        if not model_capabilities.get(capability_name):
            rejection_reasons.append(f"model_incompatible:{capability_name}")

    rejection_reasons = list(dict.fromkeys(rejection_reasons))
    return {
        "agent_version_id": version.id,
        "eligible": not rejection_reasons,
        "matched_capabilities": matched,
        "missing_capabilities": missing,
        "rejection_reasons": rejection_reasons,
        "evidence": {
            "strategy": "explicit-capabilities-and-governed-grants",
            "agent_id": str(agent.id),
            "agent_version_number": version.version_number,
            "declared_capabilities": sorted(declared_capabilities),
            "skill_grants": skill_evidence,
            "mcp_tools": tool_evidence,
            "model": model_evidence,
            "budget": {
                "id": str(budget.id) if budget else None,
                "source": budget_source,
                "amount_minor_units": budget.amount_minor_units if budget else None,
                "consumed_minor_units": consumed,
                "enforcement_mode": budget.enforcement_mode if budget else None,
            },
            "policies": policy_evidence,
        },
    }


def rank_candidate_evaluations(
    evaluations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank candidates deterministically and record the tie-breaking rationale."""
    ranked = sorted(
        evaluations,
        key=lambda item: (
            not item["eligible"],
            -len(item["matched_capabilities"]),
            str(item["agent_version_id"]),
        ),
    )
    for rank, item in enumerate(ranked, start=1):
        item["evidence"]["selection_rank"] = rank
        item["evidence"]["tie_breaker"] = (
            "eligible first, then greatest explicit capability coverage, "
            "then agent version UUID"
        )
    return ranked


def _declared_capabilities(
    session: Session, version: AgentVersion
) -> tuple[set[str], list[dict[str, Any]]]:
    manifest_capabilities = _string_set(
        (version.capability_manifest or {}).get("capabilities")
    )
    evidence: list[dict[str, Any]] = []
    rows = session.execute(
        select(AgentVersionSkill, SkillVersion)
        .join(SkillVersion, SkillVersion.id == AgentVersionSkill.skill_version_id)
        .where(AgentVersionSkill.agent_version_id == version.id)
        .order_by(AgentVersionSkill.created_at, AgentVersionSkill.id)
    ).all()
    for attachment, skill_version in rows:
        enabled = (attachment.attachment_config or {}).get("enabled", True) is not False
        capabilities = sorted(_string_set(skill_version.declared_capabilities))
        evidence.append(
            {
                "skill_version_id": str(skill_version.id),
                "enabled": enabled,
                "declared_capabilities": capabilities,
            }
        )
        if enabled:
            manifest_capabilities.update(capabilities)
    return manifest_capabilities, evidence


def _policy_evidence(
    session: Session,
    *,
    project: Project,
    goal: Goal,
    agent: Agent,
) -> list[dict[str, Any]]:
    scopes = (
        ("installation", None),
        ("team", project.team_id),
        ("project", project.id),
        ("goal", goal.id),
        ("agent", agent.id),
    )
    return [
        {
            "scope_type": scope_type,
            "scope_id": str(scope_id) if scope_id else None,
            "decision": evaluate_policy(
                session, scope_type=scope_type, scope_id=scope_id
            ),
        }
        for scope_type, scope_id in scopes
    ]


def _budget_evidence(
    session: Session,
    *,
    agent: Agent,
    version: AgentVersion,
    constraints: dict[str, Any],
) -> tuple[Budget | None, str | None, list[str], int]:
    reasons: list[str] = []
    raw_budget_id = constraints.get("budget_id")
    budget_source = "constraint" if raw_budget_id else (
        "agent_default" if version.default_budget_id else None
    )
    budget_id = raw_budget_id or version.default_budget_id
    budget = None
    if budget_id:
        try:
            budget = session.get(Budget, uuid.UUID(str(budget_id)))
        except (TypeError, ValueError):
            budget = None
        if budget is None:
            reasons.append(f"budget_not_found:{budget_id}")
        elif budget.agent_id != agent.id:
            reasons.append(f"budget_belongs_to_other_agent:{budget.id}")
    elif constraints.get("require_budget"):
        reasons.append("budget_required")

    consumed = 0
    if budget is not None:
        consumed = int(
            session.execute(
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
                ).where(
                    CostLedgerEntry.budget_id == budget.id,
                    CostLedgerEntry.status != "void",
                )
            ).scalar_one()
        )
        if (
            budget.enforcement_mode == "hard_stop"
            and consumed >= budget.amount_minor_units
        ):
            reasons.append(f"budget_exhausted:{budget.id}")
    return budget, budget_source, reasons, consumed


def _tool_evidence(
    session: Session,
    *,
    version: AgentVersion,
    tool_name: str,
) -> tuple[dict[str, Any], list[str]]:
    rows = session.execute(
        select(AgentVersionMcpServer, McpServerVersion, McpServerTool)
        .join(
            McpServerVersion,
            McpServerVersion.id == AgentVersionMcpServer.mcp_server_version_id,
        )
        .join(
            McpServerTool,
            McpServerTool.mcp_server_version_id == McpServerVersion.id,
        )
        .where(
            AgentVersionMcpServer.agent_version_id == version.id,
            McpServerTool.tool_name == tool_name,
        )
        .order_by(AgentVersionMcpServer.created_at, AgentVersionMcpServer.id)
    ).all()
    if not rows:
        return {"tool_name": tool_name, "status": "missing"}, [
            f"mcp_tool_disabled_or_missing:{tool_name}"
        ]

    degraded = False
    for attachment, server_version, tool in rows:
        attachment_enabled = (
            (attachment.attachment_config or {}).get("enabled", True) is not False
        )
        if not attachment_enabled or not tool.enabled or not tool.schema_valid:
            continue
        latest_health = session.execute(
            select(McpServerHealthCheck)
            .where(
                McpServerHealthCheck.mcp_server_version_id == server_version.id
            )
            .order_by(
                McpServerHealthCheck.checked_at.desc(),
                McpServerHealthCheck.created_at.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if latest_health is None or latest_health.status != "healthy":
            degraded = True
            continue
        mcp_policy = evaluate_policy(
            session,
            scope_type="mcp_server",
            scope_id=server_version.mcp_server_id,
        )
        tool_policy = evaluate_policy(
            session, scope_type="tool", scope_id=tool.id
        )
        restrictive = next(
            (
                (scope, decision)
                for scope, decision in (
                    ("mcp_server", mcp_policy),
                    ("tool", tool_policy),
                )
                if decision in {"deny", "approval_required"}
            ),
            None,
        )
        evidence = {
            "tool_name": tool_name,
            "status": "healthy" if restrictive is None else "policy_restricted",
            "mcp_server_version_id": str(server_version.id),
            "health_status": latest_health.status,
            "mcp_policy_decision": mcp_policy,
            "tool_policy_decision": tool_policy,
        }
        if restrictive is not None:
            scope, decision = restrictive
            return evidence, [f"{scope}_policy_{decision}:{tool_name}"]
        return evidence, []

    if degraded:
        return {"tool_name": tool_name, "status": "degraded"}, [
            f"mcp_health_degraded:{tool_name}"
        ]
    return {"tool_name": tool_name, "status": "disabled_or_invalid"}, [
        f"mcp_tool_disabled_or_missing:{tool_name}"
    ]


def _model_evidence(
    session: Session, version: AgentVersion
) -> tuple[dict[str, Any], dict[str, Any]]:
    model: ModelProfileVersion | ModelProfile | None = None
    source = None
    if version.model_profile_version_id:
        model = session.get(ModelProfileVersion, version.model_profile_version_id)
        source = "pinned_version"
    elif version.model_profile_id:
        model = session.get(ModelProfile, version.model_profile_id)
        source = "legacy_profile"
    capabilities = dict(model.capability_metadata or {}) if model else {}
    return capabilities, {
        "source": source,
        "id": str(model.id) if model else None,
        "supported_capabilities": sorted(
            key for key, value in capabilities.items() if value
        ),
    }


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item.strip() for item in value if isinstance(item, str) and item.strip()}
