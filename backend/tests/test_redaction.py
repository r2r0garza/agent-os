from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agentic_os.api.redaction import redact_mapping


class RedactMappingTests(unittest.TestCase):
    def test_preserves_non_secret_token_accounting_evidence(self) -> None:
        evidence = {
            "token_usage": {
                "status": "supported",
                "diagnostic": "usage returned",
                "prompt_tokens": 4,
                "completion_tokens": 1,
                "total_tokens": 5,
            },
            "input_cost_per_million_tokens": 1,
            "output_cost_per_million_tokens": 2,
        }

        self.assertEqual(redact_mapping(evidence), evidence)

    def test_redacts_secret_bearing_token_keys(self) -> None:
        redacted = redact_mapping(
            {
                "token": "plain-secret",
                "api_token": "api-secret",
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "csrf_token": "csrf-secret",
                "secret_token": "nested-secret",
            }
        )

        self.assertEqual(
            redacted,
            {
                "token": "[REDACTED]",
                "api_token": "[REDACTED]",
                "access_token": "[REDACTED]",
                "refresh_token": "[REDACTED]",
                "csrf_token": "[REDACTED]",
                "secret_token": "[REDACTED]",
            },
        )


if __name__ == "__main__":
    unittest.main()
