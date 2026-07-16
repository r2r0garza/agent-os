from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from typing import Any

from sqlalchemy.orm import Session

from agentic_os.domain.models import AuditEvent, Task
from agentic_os.sandbox import (
    SandboxLifecycleEvent,
    SandboxMount,
    SandboxSpec,
    select_available_runtime,
)

DEFAULT_SANDBOX_IMAGE = "alpine:latest"


class SandboxUnavailableError(RuntimeError):
    """Raised when a task requests sandbox execution but no runtime is usable."""


def execute_task_sandbox(
    session: Session,
    task: Task,
    run_id: uuid.UUID,
    sandbox_config: dict[str, Any],
    *,
    project_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Run one task's sandbox request end to end and persist its lifecycle events.

    ``sandbox_config`` comes from the agent version's capability manifest
    (``{"sandbox": {...}}``). Every lifecycle transition is recorded as an
    ``AuditEvent`` correlated with the owning project/goal/task/run so the
    trail proves what actually ran, matching VISION.md's auditable sandbox
    lifecycle requirement.
    """
    runtime = select_available_runtime()
    if runtime is None:
        raise SandboxUnavailableError(
            "no sandbox runtime (docker or podman) is available to execute the requested sandbox"
        )

    configured_workspace_root = os.environ.get("AGENTIC_OS_SANDBOX_WORKSPACE_ROOT")
    workspace_root = None
    if configured_workspace_root:
        workspace_root = os.path.abspath(configured_workspace_root)
        os.makedirs(workspace_root, mode=0o700, exist_ok=True)
    workspace_dir = tempfile.mkdtemp(
        prefix=f"agentic-os-run-{run_id}-",
        dir=workspace_root,
    )
    spec = SandboxSpec(
        image=sandbox_config.get("image", DEFAULT_SANDBOX_IMAGE),
        command=list(sandbox_config.get("command", ["true"])),
        workspace_mount=SandboxMount(host_path=workspace_dir, container_path="/workspace"),
        env=dict(sandbox_config.get("env", {})),
        network_policy=sandbox_config.get("network_policy", "none"),
        cpu_limit=float(sandbox_config.get("cpu_limit", 1.0)),
        memory_limit_mb=int(sandbox_config.get("memory_limit_mb", 256)),
        timeout_seconds=int(sandbox_config.get("timeout_seconds", 30)),
    )

    def _record(event: SandboxLifecycleEvent) -> None:
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                run_id=run_id,
                event_type=event.event_type,
                payload={"handle_id": event.handle_id, **event.payload},
            )
        )
        session.flush()

    try:
        handle, created_event = runtime.create(spec)
        _record(created_event)
        try:
            _record(runtime.start(handle))
            result, exited_event = runtime.wait(handle)
            _record(exited_event)
        finally:
            _record(runtime.stop(handle))
            _record(runtime.cleanup(handle))
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)

    return {
        "runtime": runtime.name,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
    }
