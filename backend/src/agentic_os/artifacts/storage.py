from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ArtifactStorageError(RuntimeError):
    """Raised when local artifact content cannot be stored or read safely."""


class ContentVerificationError(ArtifactStorageError):
    """Raised when bytes do not match their declared immutable identity."""


@dataclass(frozen=True)
class StagedContent:
    content_hash: str
    size_bytes: int
    staged_path: Path | None
    storage_ref: str | None = None

    @property
    def is_finalized(self) -> bool:
        return self.storage_ref is not None


class ArtifactStorage(Protocol):
    def stage(
        self,
        content: bytes,
        *,
        expected_hash: str | None = None,
        expected_size: int | None = None,
    ) -> StagedContent: ...

    def finalize(self, staged: StagedContent) -> str: ...

    def finalized_available(self, content_hash: str, size_bytes: int) -> bool: ...

    def staged_available(self, content_hash: str, size_bytes: int) -> bool: ...

    def iter_staged(self) -> tuple[tuple[str, int, float], ...]: ...

    def iter_finalized(self) -> tuple[tuple[str, int, float], ...]: ...

    def delete_staged(self, content_hash: str) -> None: ...

    def delete_finalized(self, content_hash: str) -> None: ...

    def read(self, storage_ref: str) -> bytes: ...


class LocalArtifactStorage:
    """Content-addressed blob storage backed by a local durable directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.staging_root = self.root / "staging"
        self.content_root = self.root / "content" / "sha256"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.content_root.mkdir(parents=True, exist_ok=True)

    def stage(
        self,
        content: bytes,
        *,
        expected_hash: str | None = None,
        expected_size: int | None = None,
    ) -> StagedContent:
        content_hash = _content_hash(content)
        size_bytes = len(content)
        _verify_metadata(content_hash, size_bytes, expected_hash, expected_size)

        final_path = self._final_path(content_hash)
        if final_path.exists():
            self._verify_path(final_path, content_hash, size_bytes)
            return StagedContent(content_hash, size_bytes, None, self._storage_ref(content_hash))

        staged_path = self._staged_path(content_hash)
        if staged_path.exists():
            self._verify_path(staged_path, content_hash, size_bytes)
            return StagedContent(content_hash, size_bytes, staged_path)

        temporary_path = self.staging_root / f".{uuid.uuid4().hex}.tmp"
        try:
            with temporary_path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, staged_path)
            _fsync_directory(self.staging_root)
        finally:
            temporary_path.unlink(missing_ok=True)
        self._verify_path(staged_path, content_hash, size_bytes)
        return StagedContent(content_hash, size_bytes, staged_path)

    def finalize(self, staged: StagedContent) -> str:
        final_path = self._final_path(staged.content_hash)
        if final_path.exists():
            self._verify_path(final_path, staged.content_hash, staged.size_bytes)
            if staged.staged_path is not None:
                staged.staged_path.unlink(missing_ok=True)
            return self._storage_ref(staged.content_hash)

        staged_path = staged.staged_path or self._staged_path(staged.content_hash)
        if not staged_path.exists():
            raise ArtifactStorageError(f"staged content is unavailable: {staged.content_hash}")
        self._verify_path(staged_path, staged.content_hash, staged.size_bytes)
        os.replace(staged_path, final_path)
        _fsync_directory(self.staging_root)
        _fsync_directory(self.content_root)
        self._verify_path(final_path, staged.content_hash, staged.size_bytes)
        return self._storage_ref(staged.content_hash)

    def finalized_available(self, content_hash: str, size_bytes: int) -> bool:
        try:
            self._verify_path(self._final_path(content_hash), content_hash, size_bytes)
        except ArtifactStorageError:
            return False
        return True

    def staged_available(self, content_hash: str, size_bytes: int) -> bool:
        try:
            self._verify_path(self._staged_path(content_hash), content_hash, size_bytes)
        except ArtifactStorageError:
            return False
        return True

    def iter_staged(self) -> tuple[tuple[str, int, float], ...]:
        return _iter_content(self.staging_root)

    def iter_finalized(self) -> tuple[tuple[str, int, float], ...]:
        return _iter_content(self.content_root)

    def delete_staged(self, content_hash: str) -> None:
        self._staged_path(content_hash).unlink(missing_ok=True)

    def delete_finalized(self, content_hash: str) -> None:
        self._final_path(content_hash).unlink(missing_ok=True)

    def path_for_ref(self, storage_ref: str) -> Path:
        prefix = "local://sha256/"
        if not storage_ref.startswith(prefix):
            raise ArtifactStorageError(f"unsupported local storage reference: {storage_ref}")
        return self._final_path(f"sha256:{storage_ref.removeprefix(prefix)}")

    def read(self, storage_ref: str) -> bytes:
        path = self.path_for_ref(storage_ref)
        if not path.is_file():
            raise ArtifactStorageError(f"artifact content is unavailable: {storage_ref}")
        return path.read_bytes()

    def _staged_path(self, content_hash: str) -> Path:
        return self.staging_root / _digest(content_hash)

    def _final_path(self, content_hash: str) -> Path:
        return self.content_root / _digest(content_hash)

    @staticmethod
    def _storage_ref(content_hash: str) -> str:
        return f"local://sha256/{_digest(content_hash)}"

    @staticmethod
    def _verify_path(path: Path, content_hash: str, size_bytes: int) -> None:
        if not path.is_file():
            raise ArtifactStorageError(f"artifact content is unavailable: {content_hash}")
        data = path.read_bytes()
        actual_hash = _content_hash(data)
        actual_size = len(data)
        _verify_metadata(actual_hash, actual_size, content_hash, size_bytes)


def artifact_storage() -> LocalArtifactStorage:
    configured = os.environ.get("AGENTIC_OS_ARTIFACT_ROOT")
    if configured:
        return LocalArtifactStorage(configured)
    data_root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return LocalArtifactStorage(data_root / "agentic-os" / "artifacts")


def _content_hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _digest(content_hash: str) -> str:
    prefix = "sha256:"
    digest = content_hash.removeprefix(prefix)
    if (
        not content_hash.startswith(prefix)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ArtifactStorageError(f"invalid SHA-256 content hash: {content_hash}")
    return digest


def _verify_metadata(
    actual_hash: str,
    actual_size: int,
    expected_hash: str | None,
    expected_size: int | None,
) -> None:
    if expected_hash is not None and actual_hash != expected_hash:
        raise ContentVerificationError(f"content hash mismatch: expected {expected_hash}, got {actual_hash}")
    if expected_size is not None and actual_size != expected_size:
        raise ContentVerificationError(f"content size mismatch: expected {expected_size}, got {actual_size}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _iter_content(root: Path) -> tuple[tuple[str, int, float], ...]:
    rows: list[tuple[str, int, float]] = []
    for path in root.glob("[0-9a-f]" * 64):
        stat = path.stat()
        rows.append((f"sha256:{path.name}", stat.st_size, stat.st_mtime))
    return tuple(sorted(rows))
