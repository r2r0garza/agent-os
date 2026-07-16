from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.deps import get_session
from agentic_os.api.ownership import require_default_team_access
from agentic_os.domain.models import Agent, Budget

router = APIRouter(tags=["budgets"])


class BudgetCreate(BaseModel):
    currency: str
    amount_minor_units: int
    enforcement_mode: str
    warning_threshold_percent: int | None = None


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
def create_budget(agent_id: uuid.UUID, payload: BudgetCreate, session: Session = Depends(get_session)) -> Budget:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    require_default_team_access(session, agent, "agent")
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
def list_budgets(agent_id: uuid.UUID, session: Session = Depends(get_session)) -> list[Budget]:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    require_default_team_access(session, agent, "agent")
    return list(session.execute(select(Budget).where(Budget.agent_id == agent_id).order_by(Budget.created_at)).scalars())


@router.get("/budgets/{budget_id}", response_model=BudgetRead)
def get_budget(budget_id: uuid.UUID, session: Session = Depends(get_session)) -> Budget:
    budget = session.get(Budget, budget_id)
    if budget is None:
        raise HTTPException(status_code=404, detail="budget not found")
    agent = session.get(Agent, budget.agent_id)
    require_default_team_access(session, agent, "budget")
    return budget
