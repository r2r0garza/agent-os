from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agentic_os.domain.capabilities import CAPABILITY_CATALOG
from agentic_os.domain.decomposition import UnsupportedWorkflowError, decompose_goal


class DecomposeGoalTests(unittest.TestCase):
    def test_research_brief_produces_dependent_capability_tagged_tasks(self) -> None:
        blueprints = decompose_goal(title="Ship the feature", description="Land it safely")

        self.assertEqual([b.client_id for b in blueprints], ["gather-research", "draft-output", "review-output"])
        self.assertEqual(blueprints[0].depends_on, ())
        self.assertEqual(blueprints[1].depends_on, ("gather-research",))
        self.assertEqual(blueprints[2].depends_on, ("draft-output",))

        for blueprint in blueprints:
            self.assertTrue(set(blueprint.required_capabilities).issubset(CAPABILITY_CATALOG))
            self.assertEqual(set(blueprint.capability_rationale), set(blueprint.required_capabilities))
            for rationale in blueprint.capability_rationale.values():
                self.assertTrue(rationale.reason.strip())
                self.assertTrue(rationale.evidence)

    def test_decomposition_is_reproducible_for_the_same_goal(self) -> None:
        first = decompose_goal(title="Same goal", description=None)
        second = decompose_goal(title="Same goal", description=None)
        self.assertEqual(first, second)

    def test_unsupported_workflow_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedWorkflowError):
            decompose_goal(title="Anything", workflow="arbitrary-unplanned-workflow")


if __name__ == "__main__":
    unittest.main()
