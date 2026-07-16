from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
    ).returncode == 0


def _compose_config(*args: str) -> dict:
    result = subprocess.run(
        ["docker", "compose", *args, "config", "--format", "json"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


@unittest.skipUnless(_docker_compose_available(), "Docker Compose is required for topology validation")
class LocalDeploymentTopologyTest(unittest.TestCase):
    def test_default_stack_has_separate_healthy_roles_and_durable_volumes(self) -> None:
        config = _compose_config()
        services = config["services"]

        self.assertEqual(
            set(services),
            {"api", "frontend", "postgres", "sandbox-runtime", "worker"},
        )
        for service in services.values():
            self.assertIn("healthcheck", service)

        self.assertEqual(
            set(config["volumes"]),
            {"artifacts", "configuration", "postgres-data"},
        )
        self.assertEqual(services["api"]["depends_on"]["postgres"]["condition"], "service_healthy")
        self.assertEqual(services["frontend"]["depends_on"]["api"]["condition"], "service_healthy")
        self.assertEqual(services["worker"]["depends_on"]["api"]["condition"], "service_healthy")
        self.assertEqual(
            services["worker"]["depends_on"]["sandbox-runtime"]["condition"],
            "service_healthy",
        )
        self.assertEqual(services["api"]["environment"]["OTEL_SDK_DISABLED"], "true")

    def test_telemetry_profile_adds_collector_and_its_durable_volume(self) -> None:
        config = _compose_config("--profile", "telemetry")

        self.assertIn("telemetry", config["services"])
        self.assertIn("healthcheck", config["services"]["telemetry"])
        self.assertIn("telemetry-data", config["volumes"])


if __name__ == "__main__":
    unittest.main()
