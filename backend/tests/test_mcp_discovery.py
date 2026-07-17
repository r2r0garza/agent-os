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

from agentic_os.mcp.discovery import DiscoverySettings, discover_mcp_tools


class _FakeMcpHandler(BaseHTTPRequestHandler):
    scenario = "healthy"
    requests: list[dict] = []

    def log_message(self, *_: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(
            {
                "payload": payload,
                "authorization": self.headers.get("authorization"),
            }
        )

        if self.scenario == "timeout":
            time.sleep(0.12)

        if self.scenario == "healthy":
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo tool",
                                "inputSchema": {"type": "object", "properties": {}},
                            }
                        ]
                    },
                }
            )
        elif self.scenario == "degraded_mixed":
            self._send_json(
                {
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo tool",
                                "inputSchema": {"type": "object"},
                            },
                            {
                                "name": "bad_schema",
                                "description": "Bad schema tool",
                                "inputSchema": {"type": "string"},
                            },
                            {"description": "No name at all"},
                        ]
                    }
                }
            )
        elif self.scenario == "degraded_empty":
            self._send_json({"result": {"tools": []}})
        elif self.scenario == "malformed_missing_tools":
            self._send_json({"result": {}})
        elif self.scenario == "malformed_json":
            self._send(200, b"not json")
        elif self.scenario == "http_error":
            self._send(500, b"{}")

    def _send_json(self, body: dict) -> None:
        self._send(200, json.dumps(body).encode())

    def _send(self, status: int, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@contextmanager
def fake_mcp_server(scenario: str) -> Iterator[tuple[str, type[_FakeMcpHandler]]]:
    class Handler(_FakeMcpHandler):
        pass

    Handler.scenario = scenario
    Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class McpDiscoveryTests(unittest.TestCase):
    def _discover(self, url: str | None, **overrides: object) -> dict:
        arguments = {
            "url": url,
            "headers": {"X-Extra": "never-return"},
            "credential_value": "shhh-credential",
            "settings": DiscoverySettings(timeout_seconds=0.05, max_attempts=2),
        }
        arguments.update(overrides)
        return discover_mcp_tools(**arguments)

    def test_healthy_discovery_persists_tool_evidence_and_redacts_credentials(self) -> None:
        with fake_mcp_server("healthy") as (url, handler):
            result = self._discover(url)

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["tool_count"], 1)
        self.assertEqual(result["tools"][0]["name"], "echo")
        self.assertTrue(result["tools"][0]["schema_valid"])
        self.assertIn("descriptor_hash", result["tools"][0])
        self.assertEqual(result["diagnostics"], [])
        serialized = json.dumps(result, default=str)
        self.assertNotIn("shhh-credential", serialized)
        self.assertNotIn("never-return", serialized)
        self.assertEqual(handler.requests[0]["authorization"], "Bearer shhh-credential")
        self.assertEqual(
            sorted(result["request_metadata"]["header_names"]),
            ["Authorization", "Content-Type", "X-Extra"],
        )

    def test_degraded_mixed_tools_records_invalid_entries_without_dropping_valid_ones(self) -> None:
        with fake_mcp_server("degraded_mixed") as (url, _handler):
            result = self._discover(url)

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["tool_count"], 2)
        names = {tool["name"] for tool in result["tools"]}
        self.assertEqual(names, {"echo", "bad_schema"})
        codes = {entry["code"] for entry in result["diagnostics"]}
        self.assertIn("invalid_tool_schema", codes)
        self.assertIn("invalid_tool_descriptor", codes)

    def test_degraded_empty_tools_list(self) -> None:
        with fake_mcp_server("degraded_empty") as (url, _handler):
            result = self._discover(url)

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["tool_count"], 0)
        self.assertEqual(result["tools"], [])

    def test_malformed_missing_tools_key(self) -> None:
        with fake_mcp_server("malformed_missing_tools") as (url, _handler):
            result = self._discover(url)

        self.assertEqual(result["status"], "malformed")
        self.assertEqual(result["diagnostics"], [{"code": "missing_tools_list", "phase": "response"}])

    def test_malformed_invalid_json(self) -> None:
        with fake_mcp_server("malformed_json") as (url, _handler):
            result = self._discover(url)

        self.assertEqual(result["status"], "malformed")
        self.assertEqual(result["diagnostics"], [{"code": "invalid_json", "phase": "response"}])

    def test_http_error_is_unreachable(self) -> None:
        with fake_mcp_server("http_error") as (url, _handler):
            result = self._discover(url)

        self.assertEqual(result["status"], "unreachable")
        self.assertEqual(result["diagnostics"][0]["code"], "http_error")
        self.assertEqual(result["diagnostics"][0]["http_status"], 500)

    def test_timeout_is_unreachable_with_sanitized_diagnostics(self) -> None:
        with fake_mcp_server("timeout") as (url, _handler):
            result = self._discover(url)

        self.assertEqual(result["status"], "unreachable")
        self.assertEqual(result["diagnostics"], [{"code": "timeout", "phase": "request"}])
        self.assertNotIn("shhh-credential", json.dumps(result, default=str))

    def test_missing_url_does_not_attempt_network_call(self) -> None:
        result = self._discover(None)

        self.assertEqual(result["status"], "unreachable")
        self.assertEqual(result["diagnostics"], [{"code": "missing_url", "phase": "configuration"}])
        self.assertIsNone(result["request_metadata"]["endpoint"])


if __name__ == "__main__":
    unittest.main()
