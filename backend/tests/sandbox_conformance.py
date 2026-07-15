from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentic_os.sandbox import (
    SandboxMount,
    SandboxPolicyError,
    SandboxRuntime,
    SandboxSpec,
)

_IMAGE = "alpine:latest"


def run_conformance_suite(testcase: unittest.TestCase, runtime: SandboxRuntime) -> None:
    """Exercise the lifecycle/workspace/resource-limit/audit-event contract.

    Shared by the Docker and Podman conformance test modules so both
    providers are held to exactly the same behavior, per issue #4's
    acceptance criteria.
    """
    workspace_dir = tempfile.mkdtemp(prefix="agentic-os-sandbox-conformance-")
    try:
        spec = SandboxSpec(
            image=_IMAGE,
            command=["sh", "-c", "echo hello > /workspace/output.txt && exit 0"],
            workspace_mount=SandboxMount(host_path=workspace_dir, container_path="/workspace"),
            network_policy="none",
            cpu_limit=1.0,
            memory_limit_mb=256,
            timeout_seconds=30,
        )

        events = []
        handle, created_event = runtime.create(spec)
        events.append(created_event)
        try:
            events.append(runtime.start(handle))
            result, exited_event = runtime.wait(handle)
            events.append(exited_event)
        finally:
            events.append(runtime.stop(handle))
            events.append(runtime.cleanup(handle))

        # Lifecycle: every stage emits its own auditable event, in order.
        testcase.assertEqual(
            [event.event_type for event in events],
            [
                "sandbox.created",
                "sandbox.started",
                "sandbox.exited",
                "sandbox.stopped",
                "sandbox.cleaned_up",
            ],
        )
        for event in events:
            testcase.assertEqual(event.handle_id, handle.id)

        # Command executed and the workspace mount round-trips to the host.
        testcase.assertEqual(result.exit_code, 0)
        testcase.assertFalse(result.timed_out)
        output_path = Path(workspace_dir) / "output.txt"
        testcase.assertTrue(output_path.exists())
        testcase.assertEqual(output_path.read_text().strip(), "hello")

        # Resource-limit and safe-default policy denial cases.
        with testcase.assertRaises(SandboxPolicyError):
            runtime.create(replace(spec, privileged=True))
        with testcase.assertRaises(SandboxPolicyError):
            runtime.create(replace(spec, memory_limit_mb=999_999))
        with testcase.assertRaises(SandboxPolicyError):
            runtime.create(
                replace(
                    spec,
                    extra_mounts=(SandboxMount(host_path="/var/run/docker.sock", container_path="/var/run/docker.sock"),),
                )
            )
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)
