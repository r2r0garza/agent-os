from __future__ import annotations

import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.deps import get_session
from agentic_os.domain.models import Budget, Goal, Policy, Task, TaskDependency

router = APIRouter(tags=["task-graph"])

RESOURCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]*$")
VALID_RESOURCE_INTENTS = {"read", "write"}


class ResourceIntentEntry(BaseModel):
    resource_key: str
    intent: str

    @field_validator("resource_key")
    @classmethod
    def _validate_resource_key(cls, value: str) -> str:
        if (
            not value
            or value.startswith("/")
            or ".." in value.split("/")
            or not RESOURCE_KEY_PATTERN.match(value)
        ):
            raise ValueError(f"invalid project-relative resource key: {value!r}")
        return value

    @field_validator("intent")
    @classmethod
    def _validate_intent(cls, value: str) -> str:
        if value not in VALID_RESOURCE_INTENTS:
            raise ValueError(f"invalid resource intent {value!r}; expected one of {sorted(VALID_RESOURCE_INTENTS)}")
        return value


class ExpectedOutputEntry(BaseModel):
    name: str
    kind: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("expected output name must not be empty")
        return value


class TaskGraphNodeCreate(BaseModel):
    client_id: str
    title: str
    description: str | None = None
    required_capabilities: dict = {}
    expected_outputs: list[ExpectedOutputEntry] = []
    resource_intent: list[ResourceIntentEntry] = []
    policy_ids: list[uuid.UUID] = []
    budget_id: uuid.UUID | None = None
    depends_on: list[str] = []

    @field_validator("client_id")
    @classmethod
    def _validate_client_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("client_id must not be empty")
        return value

    @field_validator("required_capabilities")
    @classmethod
    def _validate_required_capabilities(cls, value: dict) -> dict:
        for key in value:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("required_capabilities keys must be non-empty capability names")
        return value


class TaskGraphCreate(BaseModel):
    tasks: list[TaskGraphNodeCreate]


class TaskGraphNodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    goal_id: uuid.UUID
    title: str
    description: str | None
    status: str
    required_capabilities: dict
    expected_outputs: list
    resource_intent: list
    policy_ids: list
    budget_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class TaskDependencyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: uuid.UUID
    depends_on_task_id: uuid.UUID


class TaskGraphRead(BaseModel):
    tasks: list[TaskGraphNodeRead]
    dependencies: list[TaskDependencyRead]


def _load_graph(session: Session, goal_id: uuid.UUID) -> TaskGraphRead:
    tasks = list(session.execute(select(Task).where(Task.goal_id == goal_id).order_by(Task.created_at)).scalars())
    task_ids = [task.id for task in tasks]
    dependencies: list[TaskDependency] = []
    if task_ids:
        dependencies = list(
            session.execute(select(TaskDependency).where(TaskDependency.task_id.in_(task_ids))).scalars()
        )
    return TaskGraphRead(
        tasks=[TaskGraphNodeRead.model_validate(task) for task in tasks],
        dependencies=[TaskDependencyRead.model_validate(dependency) for dependency in dependencies],
    )


def _detect_cycle(adjacency: dict[uuid.UUID, set[uuid.UUID]]) -> list[uuid.UUID] | None:
    """DFS-based cycle detection; returns the cycle path (as node ids) if one exists."""
    UNVISITED, IN_PROGRESS, DONE = 0, 1, 2
    state: dict[uuid.UUID, int] = {}
    path: list[uuid.UUID] = []

    def visit(node: uuid.UUID) -> list[uuid.UUID] | None:
        state[node] = IN_PROGRESS
        path.append(node)
        for neighbor in adjacency.get(node, ()):
            neighbor_state = state.get(neighbor, UNVISITED)
            if neighbor_state == IN_PROGRESS:
                cycle_start = path.index(neighbor)
                return path[cycle_start:] + [neighbor]
            if neighbor_state == UNVISITED:
                found = visit(neighbor)
                if found is not None:
                    return found
        path.pop()
        state[node] = DONE
        return None

    for node in list(adjacency):
        if state.get(node, UNVISITED) == UNVISITED:
            found = visit(node)
            if found is not None:
                return found
    return None


@router.post("/goals/{goal_id}/task-graph", response_model=TaskGraphRead, status_code=201)
def create_task_graph(
    goal_id: uuid.UUID, payload: TaskGraphCreate, session: Session = Depends(get_session)
) -> TaskGraphRead:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    if not payload.tasks:
        raise HTTPException(status_code=422, detail="tasks must not be empty")

    client_ids = [node.client_id for node in payload.tasks]
    if len(set(client_ids)) != len(client_ids):
        raise HTTPException(status_code=422, detail="client_id values must be unique within a task graph submission")

    existing_task_ids = set(session.execute(select(Task.id).where(Task.goal_id == goal_id)).scalars())
    client_id_to_new_id = {node.client_id: uuid.uuid4() for node in payload.tasks}

    resolved_dependencies: list[tuple[uuid.UUID, uuid.UUID]] = []
    for node in payload.tasks:
        task_id = client_id_to_new_id[node.client_id]
        for dependency_ref in node.depends_on:
            if dependency_ref in client_id_to_new_id:
                depends_on_id = client_id_to_new_id[dependency_ref]
            else:
                try:
                    depends_on_id = uuid.UUID(dependency_ref)
                except ValueError as error:
                    raise HTTPException(
                        status_code=422,
                        detail=f"task {node.client_id!r} depends on unknown reference {dependency_ref!r}",
                    ) from error
                if depends_on_id not in existing_task_ids:
                    raise HTTPException(
                        status_code=422,
                        detail=f"task {node.client_id!r} depends on unknown task {dependency_ref!r}",
                    )
            if depends_on_id == task_id:
                raise HTTPException(status_code=422, detail=f"task {node.client_id!r} cannot depend on itself")
            resolved_dependencies.append((task_id, depends_on_id))

    for node in payload.tasks:
        if node.budget_id is not None and session.get(Budget, node.budget_id) is None:
            raise HTTPException(status_code=422, detail=f"budget {node.budget_id} not found")
        for policy_id in node.policy_ids:
            if session.get(Policy, policy_id) is None:
                raise HTTPException(status_code=422, detail=f"policy {policy_id} not found")

    adjacency: dict[uuid.UUID, set[uuid.UUID]] = {}
    if existing_task_ids:
        for task_id, depends_on_id in session.execute(
            select(TaskDependency.task_id, TaskDependency.depends_on_task_id).where(
                TaskDependency.task_id.in_(existing_task_ids)
            )
        ):
            adjacency.setdefault(task_id, set()).add(depends_on_id)
    for task_id, depends_on_id in resolved_dependencies:
        adjacency.setdefault(task_id, set()).add(depends_on_id)

    cycle = _detect_cycle(adjacency)
    if cycle is not None:
        raise HTTPException(
            status_code=422, detail=f"task graph contains a dependency cycle: {[str(node) for node in cycle]}"
        )

    for node in payload.tasks:
        session.add(
            Task(
                id=client_id_to_new_id[node.client_id],
                goal_id=goal_id,
                title=node.title,
                description=node.description,
                required_capabilities=node.required_capabilities,
                expected_outputs=[entry.model_dump() for entry in node.expected_outputs],
                resource_intent=[entry.model_dump() for entry in node.resource_intent],
                policy_ids=[str(policy_id) for policy_id in node.policy_ids],
                budget_id=node.budget_id,
            )
        )
    session.flush()

    for task_id, depends_on_id in resolved_dependencies:
        session.add(TaskDependency(task_id=task_id, depends_on_task_id=depends_on_id))
    session.flush()

    return _load_graph(session, goal_id)


@router.get("/goals/{goal_id}/task-graph", response_model=TaskGraphRead)
def get_task_graph(goal_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskGraphRead:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return _load_graph(session, goal_id)
