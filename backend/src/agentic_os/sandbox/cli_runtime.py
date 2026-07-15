from __future__ import annotations

import subprocess
import uuid

from agentic_os.sandbox.contracts import (
    SandboxHandle,
    SandboxLifecycleEvent,
    SandboxResult,
    SandboxRuntimeError,
    SandboxSpec,
)
from agentic_os.sandbox.policy import enforce_safe_defaults

_CREATE_TIMEOUT_SECONDS = 60
_CONTROL_TIMEOUT_SECONDS = 30


class CliSandboxRuntime:
    """Shared Docker/Podman adapter driven through each provider's CLI.

    Docker and Podman deliberately expose a compatible CLI surface, so one
    implementation parameterized by ``binary`` satisfies both conformance
    suites without duplicating lifecycle logic per provider.
    """

    def __init__(self, binary: str) -> None:
        self.name = binary
        self._binary = binary

    def create(self, spec: SandboxSpec) -> tuple[SandboxHandle, SandboxLifecycleEvent]:
        enforce_safe_defaults(spec)
        handle_id = f"agentic-os-sandbox-{uuid.uuid4().hex[:12]}"

        args = [
            self._binary,
            "create",
            "--name",
            handle_id,
            "--cpus",
            str(spec.cpu_limit),
            "--memory",
            f"{spec.memory_limit_mb}m",
            "--pids-limit",
            "256",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--network",
            "none" if spec.network_policy == "none" else "bridge",
        ]
        if spec.run_as_uid is not None:
            args += ["--user", str(spec.run_as_uid)]

        args += [
            "-v",
            self._mount_arg(spec.workspace_mount.host_path, spec.workspace_mount.container_path, spec.workspace_mount.read_only),
        ]
        for mount in spec.extra_mounts:
            args += ["-v", self._mount_arg(mount.host_path, mount.container_path, mount.read_only)]
        for key, value in spec.env.items():
            args += ["-e", f"{key}={value}"]

        args += [spec.image, *spec.command]

        result = self._run(args, timeout=_CREATE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise SandboxRuntimeError(f"{self._binary} create failed: {result.stderr.strip()}")

        handle = SandboxHandle(id=handle_id, runtime_name=self.name, spec=spec)
        event = SandboxLifecycleEvent(
            event_type="sandbox.created",
            handle_id=handle_id,
            payload={
                "runtime": self.name,
                "image": spec.image,
                "network_policy": spec.network_policy,
                "cpu_limit": spec.cpu_limit,
                "memory_limit_mb": spec.memory_limit_mb,
            },
        )
        return handle, event

    def start(self, handle: SandboxHandle) -> SandboxLifecycleEvent:
        result = self._run([self._binary, "start", handle.id], timeout=_CONTROL_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise SandboxRuntimeError(f"{self._binary} start failed: {result.stderr.strip()}")
        return SandboxLifecycleEvent(
            event_type="sandbox.started", handle_id=handle.id, payload={"runtime": self.name}
        )

    def wait(
        self, handle: SandboxHandle, *, timeout_seconds: int | None = None
    ) -> tuple[SandboxResult, SandboxLifecycleEvent]:
        timeout = timeout_seconds if timeout_seconds is not None else handle.spec.timeout_seconds
        timed_out = False
        exit_code: int | None = None
        try:
            result = self._run([self._binary, "wait", handle.id], timeout=timeout)
            if result.returncode == 0 and result.stdout.strip().isdigit():
                exit_code = int(result.stdout.strip())
        except subprocess.TimeoutExpired:
            timed_out = True
            self._run([self._binary, "kill", handle.id], timeout=_CONTROL_TIMEOUT_SECONDS)

        logs = self._run([self._binary, "logs", handle.id], timeout=_CONTROL_TIMEOUT_SECONDS)
        sandbox_result = SandboxResult(
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=logs.stdout,
            stderr=logs.stderr,
        )
        event = SandboxLifecycleEvent(
            event_type="sandbox.exited",
            handle_id=handle.id,
            payload={"runtime": self.name, "exit_code": exit_code, "timed_out": timed_out},
        )
        return sandbox_result, event

    def stop(self, handle: SandboxHandle) -> SandboxLifecycleEvent:
        self._run([self._binary, "stop", handle.id], timeout=_CONTROL_TIMEOUT_SECONDS)
        return SandboxLifecycleEvent(
            event_type="sandbox.stopped", handle_id=handle.id, payload={"runtime": self.name}
        )

    def cleanup(self, handle: SandboxHandle) -> SandboxLifecycleEvent:
        self._run([self._binary, "rm", "-f", handle.id], timeout=_CONTROL_TIMEOUT_SECONDS)
        return SandboxLifecycleEvent(
            event_type="sandbox.cleaned_up", handle_id=handle.id, payload={"runtime": self.name}
        )

    @staticmethod
    def _mount_arg(host_path: str, container_path: str, read_only: bool) -> str:
        suffix = ":ro" if read_only else ""
        return f"{host_path}:{container_path}{suffix}"

    @staticmethod
    def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
