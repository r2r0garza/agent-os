from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

NetworkPolicy = Literal["none", "restricted"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SandboxMount:
    """A single bind mount exposed to a sandbox container."""

    host_path: str
    container_path: str
    read_only: bool = False


@dataclass(frozen=True)
class SandboxSpec:
    """Provider-neutral description of one sandbox execution request.

    ``workspace_mount`` is the only mount granted by default; ``extra_mounts``
    must be justified per VISION.md's "mount only the assigned workspace view
    and necessary tool resources" default.
    """

    image: str
    command: list[str]
    workspace_mount: SandboxMount
    env: dict[str, str] = field(default_factory=dict)
    network_policy: NetworkPolicy = "none"
    cpu_limit: float = 1.0
    memory_limit_mb: int = 512
    timeout_seconds: int = 60
    privileged: bool = False
    run_as_uid: int | None = None
    extra_mounts: tuple[SandboxMount, ...] = ()


@dataclass(frozen=True)
class SandboxLifecycleEvent:
    """One auditable sandbox lifecycle transition.

    Callers persist these as ``AuditEvent`` rows correlated with the owning
    project/goal/task/run identifiers; the sandbox layer itself never writes
    to the database.
    """

    event_type: str
    handle_id: str
    occurred_at: datetime = field(default_factory=utcnow)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SandboxHandle:
    """A live reference to a created sandbox container."""

    id: str
    runtime_name: str
    spec: SandboxSpec


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of waiting for a sandbox's command to finish."""

    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str


class SandboxRuntimeError(RuntimeError):
    """Raised when a sandbox provider fails to perform a lifecycle operation."""


class SandboxRuntime(Protocol):
    """Create/start/monitor/stop/cleanup interface implemented by each provider adapter.

    Every method returns the ``SandboxLifecycleEvent`` (or, for ``wait``, an
    additional ``SandboxResult``) so the worker can persist an auditable trail
    without depending on provider-specific details.
    """

    name: str

    def create(self, spec: SandboxSpec) -> tuple[SandboxHandle, SandboxLifecycleEvent]: ...

    def start(self, handle: SandboxHandle) -> SandboxLifecycleEvent: ...

    def wait(
        self, handle: SandboxHandle, *, timeout_seconds: int | None = None
    ) -> tuple[SandboxResult, SandboxLifecycleEvent]: ...

    def stop(self, handle: SandboxHandle) -> SandboxLifecycleEvent: ...

    def cleanup(self, handle: SandboxHandle) -> SandboxLifecycleEvent: ...
