from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agentic_os.worker.sandbox_execution import execute_task_sandbox


class SandboxWorkspaceRootTest(unittest.TestCase):
    def test_configured_workspace_root_is_created_and_used(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "shared-workspaces"
            task = mock.Mock(goal_id=None)
            task.id = mock.sentinel.task_id
            runtime = mock.Mock()
            runtime.create.side_effect = RuntimeError("stop after workspace allocation")

            with (
                mock.patch.dict(
                    os.environ,
                    {"AGENTIC_OS_SANDBOX_WORKSPACE_ROOT": str(root)},
                ),
                mock.patch(
                    "agentic_os.worker.sandbox_execution.select_available_runtime",
                    return_value=runtime,
                ),
                self.assertRaisesRegex(RuntimeError, "stop after workspace allocation"),
            ):
                execute_task_sandbox(
                    mock.Mock(),
                    task,
                    mock.sentinel.run_id,
                    {},
                    project_id=None,
                )

            workspace_path = Path(runtime.create.call_args.args[0].workspace_mount.host_path)
            self.assertEqual(workspace_path.parent, root)
            self.assertTrue(root.is_dir())
            self.assertFalse(workspace_path.exists())


if __name__ == "__main__":
    unittest.main()
