from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agentic_os.api.redaction import redact_mapping

from agentic_os.api.routers import (
    agents,
    artifacts,
    assignments,
    budgets,
    credentials,
    goals,
    governance,
    mcp_servers,
    model_profiles,
    policy_sets,
    projects,
    skills,
    state,
    task_graph,
)

API_V1_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    app = FastAPI(title="Agentic OS API", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def redacted_validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(redact_mapping(error.errors()))})

    @app.get(f"{API_V1_PREFIX}/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(model_profiles.router, prefix=API_V1_PREFIX)
    app.include_router(credentials.router, prefix=API_V1_PREFIX)
    app.include_router(policy_sets.router, prefix=API_V1_PREFIX)
    app.include_router(projects.router, prefix=API_V1_PREFIX)
    app.include_router(goals.router, prefix=API_V1_PREFIX)
    app.include_router(governance.router, prefix=API_V1_PREFIX)
    app.include_router(agents.router, prefix=API_V1_PREFIX)
    app.include_router(assignments.router, prefix=API_V1_PREFIX)
    app.include_router(skills.router, prefix=API_V1_PREFIX)
    app.include_router(mcp_servers.router, prefix=API_V1_PREFIX)
    app.include_router(budgets.router, prefix=API_V1_PREFIX)
    app.include_router(state.router, prefix=API_V1_PREFIX)
    app.include_router(task_graph.router, prefix=API_V1_PREFIX)
    app.include_router(artifacts.router, prefix=API_V1_PREFIX)

    return app
