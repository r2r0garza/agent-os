from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agentic_os.sandbox import SandboxHandle, SandboxLifecycleEvent, SandboxResult
from agentic_os.worker.sandbox_execution import SandboxControlInterrupt, execute_task_sandbox


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

    def test_live_sandbox_is_stopped_when_forced_control_is_observed(self) -> None:
        stopped = threading.Event()
        runtime = mock.Mock(name="runtime")
        runtime.name = "test-runtime"
        handle = SandboxHandle(
            id="sandbox-control",
            runtime_name=runtime.name,
            spec=mock.sentinel.spec,
        )
        runtime.create.return_value = (
            handle,
            SandboxLifecycleEvent("sandbox.created", handle.id),
        )
        runtime.start.return_value = SandboxLifecycleEvent("sandbox.started", handle.id)
        runtime.stop.side_effect = lambda _: (
            stopped.set()
            or SandboxLifecycleEvent("sandbox.stopped", handle.id)
        )
        runtime.cleanup.return_value = SandboxLifecycleEvent("sandbox.cleaned_up", handle.id)

        def wait(_):
            stopped.wait(timeout=2)
            return (
                SandboxResult(
                    exit_code=None,
                    timed_out=False,
                    stdout="",
                    stderr="",
                ),
                SandboxLifecycleEvent("sandbox.exited", handle.id),
            )

        runtime.wait.side_effect = wait
        checks = iter([None, None, ("cancel", True)])

        with (
            mock.patch(
                "agentic_os.worker.sandbox_execution.select_available_runtime",
                return_value=runtime,
            ),
            self.assertRaises(SandboxControlInterrupt) as raised,
        ):
            execute_task_sandbox(
                mock.Mock(),
                mock.Mock(id=mock.sentinel.task_id, goal_id=None),
                mock.sentinel.run_id,
                {},
                project_id=None,
                control_check=lambda: next(checks, ("cancel", True)),
            )

        self.assertEqual(raised.exception.action, "cancel")
        self.assertTrue(raised.exception.forced)
        runtime.stop.assert_called_once_with(handle)
        runtime.cleanup.assert_called_once_with(handle)


if __name__ == "__main__":
    unittest.main()
