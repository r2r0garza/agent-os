from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agentic_os.sandbox import PodmanSandboxRuntime, runtime_available
from sandbox_conformance import run_conformance_suite


class PodmanSandboxConformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        available, reason = runtime_available("podman")
        if not available:
            self.skipTest(reason)
        self.runtime = PodmanSandboxRuntime()

    def test_lifecycle_workspace_resource_limit_and_audit_contract(self) -> None:
        run_conformance_suite(self, self.runtime)


if __name__ == "__main__":
    unittest.main()
