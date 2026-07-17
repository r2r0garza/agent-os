from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agentic_os.model_profiles.probing import ProbeSettings, probe_model_profile


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    scenario = "success"
    requests: list[dict] = []
    base_attempts = 0

    def log_message(self, *_: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(
            {
                "path": self.path,
                "payload": payload,
                "authorization": self.headers.get("authorization"),
                "custom_secret": self.headers.get("x-api-key"),
            }
        )

        is_feature_probe = any(
            key in payload for key in ("response_format", "tools", "stream")
        )
        if not is_feature_probe:
            type(self).base_attempts += 1
            if self.scenario == "timeout_once" and type(self).base_attempts == 1:
                time.sleep(0.12)
            if self.scenario == "timeout":
                time.sleep(0.12)
            if self.scenario == "malformed":
                self._send(200, b'{"choices":[]}')
                return

        if self.scenario == "unsupported" and is_feature_probe:
            self._send(400, b'{"error":{"message":"unsupported"}}')
            return

        if payload.get("stream"):
            self._send(
                200,
                b'data: {"choices":[{"delta":{"content":"probe"}}]}\n\ndata: [DONE]\n\n',
                content_type="text/event-stream",
            )
            return
        if "tools" in payload:
            body = {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_probe",
                                    "type": "function",
                                    "function": {
                                        "name": "probe_capability",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            self._send_json(body)
            return
        if "response_format" in payload:
            self._send_json(
                {"choices": [{"message": {"content": '{"probe": true}'}}]}
            )
            return
        body = {"choices": [{"message": {"content": "probe"}}]}
        if self.scenario != "unsupported":
            body["usage"] = {
                "prompt_tokens": 4,
                "completion_tokens": 1,
                "total_tokens": 5,
            }
        self._send_json(body)

    def _send_json(self, body: dict) -> None:
        self._send(200, json.dumps(body).encode())

    def _send(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str = "application/json",
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@contextmanager
def fake_openai_server(scenario: str) -> Iterator[tuple[str, type[_FakeOpenAIHandler]]]:
    class Handler(_FakeOpenAIHandler):
        pass

    Handler.scenario = scenario
    Handler.requests = []
    Handler.base_attempts = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ModelProfileProbeTests(unittest.TestCase):
    pricing = {
        "currency": "USD",
        "input_cost_per_million_tokens": 1.0,
        "output_cost_per_million_tokens": 2.0,
    }

    def _probe(self, base_url: str, **overrides: object) -> dict:
        arguments = {
            "base_url": base_url,
            "model_identifier": "fake-model",
            "api_key": "sk-never-return",
            "configured_headers": {"X-API-Key": "header-never-return"},
            "pricing_metadata": self.pricing,
            "settings": ProbeSettings(timeout_seconds=0.05, max_attempts=2),
        }
        arguments.update(overrides)
        return probe_model_profile(**arguments)

    def test_success_records_supported_capabilities_and_redacted_metadata(self) -> None:
        with fake_openai_server("success") as (base_url, handler):
            result = self._probe(f"{base_url}?access_token=query-never-return")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["capability_evidence"]["streaming"]["status"], "supported")
        self.assertEqual(result["capability_evidence"]["tool_calls"]["status"], "supported")
        self.assertEqual(
            result["capability_evidence"]["structured_output"]["status"], "supported"
        )
        self.assertEqual(result["capability_evidence"]["token_usage"]["status"], "supported")
        self.assertEqual(result["pricing_evidence"]["status"], "valid")
        self.assertEqual(result["request_metadata"]["endpoint"], base_url)
        serialized = json.dumps(result, default=str)
        self.assertNotIn("sk-never-return", serialized)
        self.assertNotIn("header-never-return", serialized)
        self.assertNotIn("query-never-return", serialized)
        self.assertIn("access_token=query-never-return", handler.requests[0]["path"])
        self.assertEqual(handler.requests[0]["authorization"], "Bearer sk-never-return")
        self.assertEqual(handler.requests[0]["custom_secret"], "header-never-return")

    def test_unsupported_capabilities_and_unpriced_model_are_explicit(self) -> None:
        with fake_openai_server("unsupported") as (base_url, _):
            result = self._probe(base_url, pricing_metadata={})

        self.assertEqual(result["status"], "degraded")
        for capability in ("streaming", "tool_calls", "structured_output"):
            self.assertEqual(
                result["capability_evidence"][capability]["status"], "unsupported"
            )
        self.assertEqual(result["capability_evidence"]["token_usage"]["status"], "unsupported")
        self.assertEqual(result["pricing_evidence"]["status"], "error")
        self.assertEqual(
            result["pricing_evidence"]["failures"][0]["code"],
            "unpriced_metered_action",
        )

    def test_timeout_is_retried_and_success_is_recorded(self) -> None:
        with fake_openai_server("timeout_once") as (base_url, handler):
            result = self._probe(base_url)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["request_metadata"]["attempts"], 2)
        self.assertEqual(
            result["capability_evidence"]["retry_timeout"]["status"], "supported"
        )
        self.assertGreaterEqual(handler.base_attempts, 2)

    def test_final_timeout_has_sanitized_failure(self) -> None:
        with fake_openai_server("timeout") as (base_url, _):
            result = self._probe(base_url)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["diagnostics"], [{"code": "timeout", "phase": "base"}])
        self.assertNotIn("sk-never-return", json.dumps(result, default=str))

    def test_malformed_response_has_sanitized_failure(self) -> None:
        with fake_openai_server("malformed") as (base_url, _):
            result = self._probe(base_url)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["diagnostics"],
            [{"code": "malformed_response", "phase": "base"}],
        )


if __name__ == "__main__":
    unittest.main()
