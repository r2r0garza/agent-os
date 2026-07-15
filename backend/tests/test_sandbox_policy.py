from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agentic_os.sandbox import SandboxMount, SandboxPolicyError, SandboxSpec, enforce_safe_defaults


def _spec(**overrides) -> SandboxSpec:
    base = SandboxSpec(
        image="alpine:latest",
        command=["true"],
        workspace_mount=SandboxMount(host_path="/tmp/agentic-os-workspace", container_path="/workspace"),
    )
    return replace(base, **overrides)


class SandboxPolicyTest(unittest.TestCase):
    def test_default_spec_passes(self) -> None:
        spec = _spec()
        self.assertIs(enforce_safe_defaults(spec), spec)

    def test_privileged_is_denied(self) -> None:
        with self.assertRaises(SandboxPolicyError):
            enforce_safe_defaults(_spec(privileged=True))

    def test_excessive_cpu_is_denied(self) -> None:
        with self.assertRaises(SandboxPolicyError):
            enforce_safe_defaults(_spec(cpu_limit=100.0))

    def test_zero_cpu_is_denied(self) -> None:
        with self.assertRaises(SandboxPolicyError):
            enforce_safe_defaults(_spec(cpu_limit=0))

    def test_excessive_memory_is_denied(self) -> None:
        with self.assertRaises(SandboxPolicyError):
            enforce_safe_defaults(_spec(memory_limit_mb=999_999))

    def test_excessive_timeout_is_denied(self) -> None:
        with self.assertRaises(SandboxPolicyError):
            enforce_safe_defaults(_spec(timeout_seconds=10_000))

    def test_unsupported_network_policy_is_denied(self) -> None:
        with self.assertRaises(SandboxPolicyError):
            enforce_safe_defaults(_spec(network_policy="host"))

    def test_root_mount_is_denied(self) -> None:
        with self.assertRaises(SandboxPolicyError):
            enforce_safe_defaults(
                _spec(workspace_mount=SandboxMount(host_path="/", container_path="/workspace"))
            )

    def test_docker_socket_mount_is_denied(self) -> None:
        with self.assertRaises(SandboxPolicyError):
            enforce_safe_defaults(
                _spec(
                    extra_mounts=(
                        SandboxMount(
                            host_path="/var/run/docker.sock", container_path="/var/run/docker.sock"
                        ),
                    )
                )
            )

    def test_podman_socket_mount_is_denied(self) -> None:
        with self.assertRaises(SandboxPolicyError):
            enforce_safe_defaults(
                _spec(
                    extra_mounts=(
                        SandboxMount(
                            host_path="/run/podman/podman.sock", container_path="/run/podman.sock"
                        ),
                    )
                )
            )


if __name__ == "__main__":
    unittest.main()
