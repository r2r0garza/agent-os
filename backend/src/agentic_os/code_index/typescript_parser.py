from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

from agentic_os.code_index.contracts import PARSER_API_VERSION, IndexError, TrackedFile, span, stable_id


class TypeScriptParser:
    language = "typescript"
    api_version = PARSER_API_VERSION

    def supports(self, path: str) -> bool:
        return path.endswith((".ts", ".tsx", ".mts"))

    def parse(self, repository: Path, file: TrackedFile) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        helper = Path(__file__).with_name("typescript_parser.mjs")
        request = json.dumps({
            "repository": str(repository), "path": file.path,
            "content": base64.b64encode(file.content).decode("ascii"),
        })
        result = subprocess.run(["node", str(helper)], input=request, text=True, capture_output=True)
        if result.returncode:
            raise IndexError(result.stderr.strip() or f"cannot parse {file.path}")
        parsed = json.loads(result.stdout)
        module = parsed["moduleName"]
        module_id = stable_id(self.language, "module", module)
        symbols: list[dict[str, Any]] = [{
            "id": module_id, "language": self.language, "kind": "module", "qualified_name": module,
            "span": span(file.path, 1, 1, parsed["lineCount"], 1), "visibility": "public",
            "extensions": {"core": {"path": file.path}, "typescript": {"module": module}},
        }]
        ids = {module: module_id}
        for item in parsed["symbols"]:
            symbol_id = stable_id(self.language, item["kind"], item["qualifiedName"])
            ids[item["qualifiedName"]] = symbol_id
            record: dict[str, Any] = {
                "id": symbol_id, "language": self.language, "kind": item["kind"],
                "qualified_name": item["qualifiedName"], "span": self._span(file.path, item["location"]),
                "visibility": item["visibility"],
                "extensions": {"core": {"path": file.path}, "typescript": item["extensions"]},
            }
            if item.get("signature") is not None:
                record["signature"] = item["signature"]
            symbols.append(record)
        dependencies = []
        for item in parsed["dependencies"]:
            owner = ids.get(item["owner"])
            if owner is None:
                raise IndexError(f"parser produced unknown owner {item['owner']} in {file.path}")
            dependencies.append({
                "language": self.language, "kind": item["kind"], "source_id": owner, "target": item["target"],
                "evidence": "declared" if item["kind"] == "import" else "unresolved",
                "span": self._span(file.path, item["location"]),
                "extensions": {
                    "core": {"path": file.path, "resolution": item["resolution"]},
                    "typescript": item["extension"],
                },
            })
        return symbols, dependencies

    @staticmethod
    def _span(path: str, value: dict[str, int]) -> dict[str, Any]:
        return span(path, value["startLine"], value["startColumn"], value["endLine"], value["endColumn"])


class JavaScriptParser(TypeScriptParser):
    language = "javascript"

    def supports(self, path: str) -> bool:
        return path.endswith((".js", ".jsx", ".mjs"))
