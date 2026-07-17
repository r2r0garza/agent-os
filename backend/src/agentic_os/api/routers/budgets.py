from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.deps import get_session
from agentic_os.api.authorization import current_actor, require_team_access
from agentic_os.domain.models import Agent, Budget, User

router = APIRouter(tags=["budgets"])


class BudgetCreate(BaseModel):
    currency: str = Field(min_length=3, max_length=8)
    amount_minor_units: int = Field(ge=0)
    enforcement_mode: Literal["warning", "hard_stop"]
    warning_threshold_percent: int | None = Field(default=None, ge=1, le=100)


class BudgetUpdate(BaseModel):
    amount_minor_units: int | None = Field(default=None, ge=0)
    enforcement_mode: Literal["warning", "hard_stop"] | None = None
    warning_threshold_percent: int | None = Field(default=None, ge=1, le=100)


class BudgetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    currency: str
    amount_minor_units: int
    enforcement_mode: str
    warning_threshold_percent: int | None
    created_at: datetime
    updated_at: datetime


@router.post("/agents/{agent_id}/budgets", response_model=BudgetRead, status_code=201)
def create_budget(
    agent_id: uuid.UUID,
    payload: BudgetCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Budget:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    require_team_access(session, actor, agent.team_id, action="budget.create", resource_type="agent")
    budget = Budget(
        agent_id=agent_id,
        currency=payload.currency,
        amount_minor_units=payload.amount_minor_units,
        enforcement_mode=payload.enforcement_mode,
        warning_threshold_percent=payload.warning_threshold_percent,
    )
    session.add(budget)
    session.flush()
    session.refresh(budget)
    return budget


@router.get("/agents/{agent_id}/budgets", response_model=list[BudgetRead])
def list_budgets(
    agent_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[Budget]:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    require_team_access(session, actor, agent.team_id, action="budget.list", resource_type="agent")
    return list(
        session.execute(
            select(Budget).where(Budget.agent_id == agent_id).order_by(Budget.created_at)
        ).scalars()
    )


@router.get("/budgets/{budget_id}", response_model=BudgetRead)
def get_budget(
    budget_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Budget:
    budget = session.get(Budget, budget_id)
    if budget is None:
        raise HTTPException(status_code=404, detail="budget not found")
    agent = session.get(Agent, budget.agent_id)
    require_team_access(session, actor, agent.team_id, action="budget.read", resource_type="budget")
    return budget


@router.patch("/budgets/{budget_id}", response_model=BudgetRead)
def update_budget(
    budget_id: uuid.UUID,
    payload: BudgetUpdate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Budget:
    budget = session.get(Budget, budget_id)
    if budget is None:
        raise HTTPException(status_code=404, detail="budget not found")
    agent = session.get(Agent, budget.agent_id)
    require_team_access(session, actor, agent.team_id, action="budget.update", resource_type="budget")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(budget, field, value)
    session.flush()
    session.refresh(budget)
    return budget
