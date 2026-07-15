from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from agentic_os.code_index.contracts import PARSER_API_VERSION, IndexError, TrackedFile, span, stable_id


def module_name(path: str) -> str:
    value = path.removeprefix("backend/src/").removeprefix("backend/tests/")
    value = value[:-3].replace("/", ".")
    return value.removesuffix(".__init__")


def dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def node_span(path: str, node: ast.AST) -> dict[str, Any]:
    return span(path, node.lineno, node.col_offset + 1, node.end_lineno or node.lineno, max(1, node.end_col_offset or node.col_offset + 1))


class PythonParser:
    language = "python"
    api_version = PARSER_API_VERSION

    def supports(self, path: str) -> bool:
        return path.endswith(".py")

    def parse(self, repository: Path, file: TrackedFile) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        del repository
        try:
            source = file.content.decode("utf-8")
            tree = ast.parse(source, filename=file.path)
        except (UnicodeDecodeError, SyntaxError) as error:
            raise IndexError(f"cannot parse {file.path}: {error}") from error
        module = module_name(file.path)
        module_id = stable_id(self.language, "module", module)
        line_count = max(1, source.count("\n") + 1)
        symbols: list[dict[str, Any]] = [{
            "id": module_id, "language": self.language, "kind": "module", "qualified_name": module,
            "span": span(file.path, 1, 1, line_count, 1),
            "visibility": "private" if module.rsplit(".", 1)[-1].startswith("_") else "public",
            "extensions": {"core": {"path": file.path}, "python": {"package": module.rpartition(".")[0]}},
        }]
        dependencies: list[dict[str, Any]] = []

        def add_dependency(kind: str, owner: str, target: str, node: ast.AST, details: dict[str, Any]) -> None:
            dependencies.append({
                "language": self.language, "kind": kind, "source_id": owner, "target": target,
                "evidence": "declared" if kind == "import" else "unresolved", "span": node_span(file.path, node),
                "extensions": {"core": {"path": file.path, "resolution": details}, "python": {}},
            })

        def visit_body(body: list[ast.stmt], parent_qn: str, owner_id: str, class_qn: str | None = None) -> None:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = "class" if isinstance(node, ast.ClassDef) else ("method" if class_qn else "function")
                    qn = f"{parent_qn}.{node.name}"
                    symbol_id = stable_id(self.language, kind, qn)
                    extension: dict[str, Any] = {
                        "decorators": [value for item in node.decorator_list if (value := dotted(item.func if isinstance(item, ast.Call) else item))],
                    }
                    if isinstance(node, ast.ClassDef):
                        extension["bases"] = [value for item in node.bases if (value := dotted(item))]
                    else:
                        extension["async"] = isinstance(node, ast.AsyncFunctionDef)
                        extension["annotations"] = {
                            arg.arg: ast.unparse(arg.annotation) for arg in node.args.args if arg.annotation is not None
                        }
                        routes = []
                        for decorator in node.decorator_list:
                            if isinstance(decorator, ast.Call) and (name := dotted(decorator.func)) and name.rsplit(".", 1)[-1] in {"get", "post", "put", "patch", "delete"}:
                                if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
                                    routes.append({"method": name.rsplit(".", 1)[-1].upper(), "path": decorator.args[0].value})
                        if routes:
                            extension["routes"] = routes
                    record: dict[str, Any] = {
                        "id": symbol_id, "language": self.language, "kind": kind, "qualified_name": qn,
                        "span": node_span(file.path, node),
                        "visibility": "private" if node.name.startswith("_") else "public",
                        "extensions": {"core": {"path": file.path}, "python": extension},
                    }
                    if not isinstance(node, ast.ClassDef):
                        record["signature"] = ast.unparse(node.args)
                    symbols.append(record)
                    visit_calls(node.body, symbol_id, qn, qn.rpartition(".")[0] if kind == "method" else class_qn)
                    visit_body(node.body, qn, symbol_id, qn if kind == "class" else class_qn)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        add_dependency("import", owner_id, alias.name, node, {"form": "import", "module": alias.name, "alias": alias.asname or alias.name.split(".")[0]})
                elif isinstance(node, ast.ImportFrom):
                    base = "." * node.level + (node.module or "")
                    for alias in node.names:
                        target = f"{base}.{alias.name}" if base else alias.name
                        add_dependency("import", owner_id, target, node, {"form": "from", "module": base, "name": alias.name, "alias": alias.asname or alias.name})

        def visit_calls(body: list[ast.stmt], owner_id: str, owner_qn: str, class_qn: str | None) -> None:
            class CallVisitor(ast.NodeVisitor):
                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                    return None

                def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                    return None

                def visit_ClassDef(self, node: ast.ClassDef) -> None:
                    return None

                def visit_Call(self, node: ast.Call) -> None:
                    target = dotted(node.func)
                    if target is None:
                        self.generic_visit(node)
                        return
                    if target in {"print", "len", "str", "int", "bool", "list", "dict", "set", "tuple", "range", "super"}:
                        self.generic_visit(node)
                        return
                    add_dependency("call", owner_id, target, node, {"module": module, "owner": owner_qn, "class": class_qn, "syntactic_target": target})
                    self.generic_visit(node)

            visitor = CallVisitor()
            for statement in body:
                if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visitor.visit(statement)

        visit_calls(tree.body, module_id, module, None)
        visit_body(tree.body, module, module_id)
        return symbols, dependencies
