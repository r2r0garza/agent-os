from __future__ import annotations

from agentic_os.sandbox.cli_runtime import CliSandboxRuntime


class PodmanSandboxRuntime(CliSandboxRuntime):
    """Sandbox runtime adapter backed by the Podman CLI."""

    def __init__(self) -> None:
        super().__init__("podman")
