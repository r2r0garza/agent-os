from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from agentic_os.api.deps import get_session
from agentic_os.domain.assignment import assign_task
from agentic_os.domain.models import Task

router = APIRouter(prefix="/tasks", tags=["assignments"])


class AssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: uuid.UUID
    status: str
    selected_agent_version_id: uuid.UUID | None
    candidates: list
    rationale: dict
    updated_at: datetime | None


def _read(task: Task) -> AssignmentRead:
    return AssignmentRead(
        task_id=task.id,
        status=task.assignment_status,
        selected_agent_version_id=task.assigned_agent_version_id,
        candidates=task.assignment_candidates,
        rationale=task.assignment_rationale,
        updated_at=task.assignment_updated_at,
    )


@router.post("/{task_id}/assignment", response_model=AssignmentRead)
def create_assignment(task_id: uuid.UUID, session: Session = Depends(get_session)) -> AssignmentRead:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _read(assign_task(session, task))


@router.get("/{task_id}/assignment", response_model=AssignmentRead)
def get_assignment(task_id: uuid.UUID, session: Session = Depends(get_session)) -> AssignmentRead:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _read(task)
