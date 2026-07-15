from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from agentic_os.code_index.contracts import IndexError, TrackedFile, configuration, validate_path


def discover(repository: Path) -> list[TrackedFile]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "-z"], cwd=repository, check=True, capture_output=True
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise IndexError(f"cannot list Git-tracked files: {error}") from error
    config = configuration()
    paths: list[str] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            path = validate_path(raw.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise IndexError("tracked path is not UTF-8") from error
        parts = path.split("/")
        if any(part in config["exclude_parts"] for part in parts):
            continue
        if not any(path.endswith(suffix) for suffix in config["include_suffixes"]):
            continue
        if path.endswith(".py"):
            roots = config["python_source_roots"]
        elif path.endswith((".ts", ".tsx", ".mts")):
            roots = config["typescript_source_roots"]
        else:
            roots = config["javascript_source_roots"]
        if not any(path == root or path.startswith(f"{root}/") for root in roots):
            continue
        paths.append(path)

    files: list[TrackedFile] = []
    for path in sorted(paths):
        absolute = repository / path
        try:
            stat = absolute.lstat()
            if stat.st_size > config["max_file_size"]:
                continue
            if absolute.is_symlink():
                resolved = absolute.resolve(strict=False)
                if not resolved.is_relative_to(repository.resolve()):
                    raise IndexError(f"tracked symlink escapes repository: {path}")
                raise IndexError(f"tracked source symlinks are not supported: {path}")
            else:
                content = absolute.read_bytes()
        except OSError as error:
            raise IndexError(f"cannot read tracked file {path}: {error}") from error
        files.append(TrackedFile(path, len(content), hashlib.sha256(content).hexdigest(), content))
    return files
