from __future__ import annotations

from pathlib import Path

from agentic_os.sandbox.contracts import SandboxMount, SandboxSpec

MAX_CPU_LIMIT = 4.0
MAX_MEMORY_LIMIT_MB = 4096
MAX_TIMEOUT_SECONDS = 900

_DENIED_HOST_PATHS = (Path("/"), Path.home())
_DENIED_MOUNT_SUBSTRINGS = (
    "docker.sock",
    "podman.sock",
    "/var/run/docker",
    "/var/run/podman",
    "/run/docker",
    "/run/podman",
)


class SandboxPolicyError(RuntimeError):
    """Raised when a requested sandbox spec violates the safe-default policy."""


def enforce_safe_defaults(spec: SandboxSpec) -> SandboxSpec:
    """Validate a sandbox spec against the non-negotiable safe defaults.

    Returns the spec unchanged when it passes; raises ``SandboxPolicyError``
    otherwise. This is the single choke point every runtime adapter must call
    before creating a container, so no adapter can silently grant privileged
    execution, host socket access, or unbounded resources.
    """
    if spec.privileged:
        raise SandboxPolicyError("privileged containers are not permitted")

    if spec.network_policy not in ("none", "restricted"):
        raise SandboxPolicyError(f"unsupported network policy {spec.network_policy!r}")

    if not (0 < spec.cpu_limit <= MAX_CPU_LIMIT):
        raise SandboxPolicyError(
            f"cpu_limit must be in (0, {MAX_CPU_LIMIT}]; got {spec.cpu_limit}"
        )
    if not (0 < spec.memory_limit_mb <= MAX_MEMORY_LIMIT_MB):
        raise SandboxPolicyError(
            f"memory_limit_mb must be in (0, {MAX_MEMORY_LIMIT_MB}]; got {spec.memory_limit_mb}"
        )
    if not (0 < spec.timeout_seconds <= MAX_TIMEOUT_SECONDS):
        raise SandboxPolicyError(
            f"timeout_seconds must be in (0, {MAX_TIMEOUT_SECONDS}]; got {spec.timeout_seconds}"
        )

    _validate_mount(spec.workspace_mount)
    for mount in spec.extra_mounts:
        _validate_mount(mount)

    return spec


def _validate_mount(mount: SandboxMount) -> None:
    for needle in _DENIED_MOUNT_SUBSTRINGS:
        if needle in mount.host_path or needle in mount.container_path:
            raise SandboxPolicyError(f"mount referencing {needle!r} is not permitted")

    resolved = Path(mount.host_path).resolve()
    if resolved in _DENIED_HOST_PATHS:
        raise SandboxPolicyError(f"mounting {resolved} is not permitted")
