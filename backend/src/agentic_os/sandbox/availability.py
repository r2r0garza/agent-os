from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentic_os.sandbox.contracts import SandboxRuntime


def runtime_available(binary: str) -> tuple[bool, str]:
    """Probe whether a container CLI is installed and its daemon is reachable.

    Returns ``(True, "")`` when usable, or ``(False, reason)`` with a reason
    suitable for surfacing as an explicit test-skip message.
    """
    if shutil.which(binary) is None:
        return False, f"{binary!r} executable not found on PATH"
    try:
        result = subprocess.run(
            [binary, "info"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"{binary} info could not be run: {error}"
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "").strip().splitlines()
        detail = reason[0] if reason else f"exit code {result.returncode}"
        return False, f"{binary} daemon is not available: {detail}"
    return True, ""


def select_available_runtime() -> "SandboxRuntime | None":
    """Return the first usable provider adapter, preferring Docker over Podman."""
    from agentic_os.sandbox.docker_runtime import DockerSandboxRuntime
    from agentic_os.sandbox.podman_runtime import PodmanSandboxRuntime

    for binary, runtime_cls in (("docker", DockerSandboxRuntime), ("podman", PodmanSandboxRuntime)):
        available, _ = runtime_available(binary)
        if available:
            return runtime_cls()
    return None
