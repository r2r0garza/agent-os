from __future__ import annotations

from fastapi import FastAPI

from agentic_os.api.routers import agents, budgets, goals, mcp_servers, model_profiles, projects, skills, state

API_V1_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    app = FastAPI(title="Agentic OS API", version="0.1.0")

    @app.get(f"{API_V1_PREFIX}/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(model_profiles.router, prefix=API_V1_PREFIX)
    app.include_router(projects.router, prefix=API_V1_PREFIX)
    app.include_router(goals.router, prefix=API_V1_PREFIX)
    app.include_router(agents.router, prefix=API_V1_PREFIX)
    app.include_router(skills.router, prefix=API_V1_PREFIX)
    app.include_router(mcp_servers.router, prefix=API_V1_PREFIX)
    app.include_router(budgets.router, prefix=API_V1_PREFIX)
    app.include_router(state.router, prefix=API_V1_PREFIX)

    return app
