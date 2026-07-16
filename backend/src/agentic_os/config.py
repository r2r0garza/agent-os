"""Local configuration and master-key validation.

Startup/preflight checks for the local Compose deployment: required
environment, master-key material, and optional telemetry settings. Checks
fail closed and never surface raw secret material in their diagnostics.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet

MASTER_KEY_ENV = "AGENTIC_OS_MASTER_KEY"
MASTER_KEY_FILE_ENV = "AGENTIC_OS_MASTER_KEY_FILE"
DEFAULT_MASTER_KEY_FILE = "/etc/agentic-os/master.key"
DATABASE_URL_ENV = "AGENTIC_OS_DATABASE_URL"
ARTIFACT_ROOT_ENV = "AGENTIC_OS_ARTIFACT_ROOT"
API_URL_ENV = "AGENTIC_OS_API_URL"
TELEMETRY_DISABLED_ENV = "AGENTIC_OS_TELEMETRY_DISABLED"
OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

_INSECURE_MODE_BITS = stat.S_IRWXG | stat.S_IRWXO


class ConfigurationError(RuntimeError):
    """Raised when local configuration or secret material fails closed."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _validate_key_material(raw: bytes, *, source: str) -> None:
    try:
        Fernet(raw)
    except (ValueError, TypeError) as error:
        raise ConfigurationError(
            f"master key from {source} is not a valid urlsafe-base64 32-byte Fernet key"
        ) from error


def _check_file_permissions(path: Path) -> None:
    if os.name != "posix":
        return
    mode = path.stat().st_mode
    if mode & _INSECURE_MODE_BITS:
        raise ConfigurationError(
            f"master key file {path} must not be group- or world-readable/writable "
            f"(run: chmod 600 {path})"
        )


def resolve_master_key(*, env: dict[str, str] | None = None) -> bytes | None:
    """Resolve configured master key material, failing closed on invalid input.

    Returns ``None`` only when no key material is configured at all, so
    callers decide whether an unset key is acceptable (dev/test fallback) or
    fatal (preflight/startup).
    """
    environment = env if env is not None else os.environ
    raw_env_key = environment.get(MASTER_KEY_ENV)
    if raw_env_key:
        key_bytes = raw_env_key.encode()
        _validate_key_material(key_bytes, source=f"{MASTER_KEY_ENV} environment variable")
        return key_bytes

    key_path = Path(environment.get(MASTER_KEY_FILE_ENV) or DEFAULT_MASTER_KEY_FILE)
    if not key_path.exists():
        return None
    if not key_path.is_file():
        raise ConfigurationError(f"master key path {key_path} is not a regular file")
    _check_file_permissions(key_path)
    key_bytes = key_path.read_bytes().strip()
    _validate_key_material(key_bytes, source=str(key_path))
    return key_bytes


def generate_master_key(path: str | Path = DEFAULT_MASTER_KEY_FILE, *, force: bool = False) -> Path:
    """Generate and persist a new local master key with restrictive permissions."""
    target = Path(path)
    if target.exists() and not force:
        raise ConfigurationError(f"master key file {target} already exists; pass --force to overwrite")
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = Fernet.generate_key()
        target.write_bytes(key)
        target.chmod(0o600)
    except OSError as error:
        raise ConfigurationError(f"cannot write master key to {target}: {error.strerror or error}") from error
    return target


def validate_database_url(url: str | None = None) -> CheckResult:
    value = url if url is not None else os.environ.get(DATABASE_URL_ENV)
    if not value:
        return CheckResult("database_url", True, "using the built-in local PostgreSQL default")
    parsed = urlparse(value)
    if not parsed.scheme.startswith("postgresql") or not parsed.hostname:
        return CheckResult(
            "database_url",
            False,
            f"{DATABASE_URL_ENV} must be a postgresql:// URL with a host (got scheme {parsed.scheme!r})",
        )
    return CheckResult("database_url", True, f"postgresql host {parsed.hostname!r} configured")


def validate_artifact_root(path: str | None = None) -> CheckResult:
    configured = path if path is not None else os.environ.get(ARTIFACT_ROOT_ENV)
    target = (
        Path(configured)
        if configured
        else Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        / "agentic-os"
        / "artifacts"
    )
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".agentic-os-write-check"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as error:
        return CheckResult("artifact_root", False, f"{target} is not writable: {error.strerror or error}")
    return CheckResult("artifact_root", True, f"{target} is writable")


def validate_master_key(*, required: bool = True) -> CheckResult:
    try:
        key = resolve_master_key()
    except ConfigurationError as error:
        return CheckResult("master_key", False, str(error))
    if key is None:
        if required:
            return CheckResult(
                "master_key",
                False,
                f"no master key configured; set {MASTER_KEY_ENV} or run "
                "'agentic-os config generate-master-key'",
            )
        return CheckResult("master_key", True, "no master key configured; using an ephemeral in-process key")
    return CheckResult("master_key", True, "master key resolved and validated")


def validate_api_url() -> CheckResult:
    value = os.environ.get(API_URL_ENV)
    if not value:
        return CheckResult("api_url", True, f"{API_URL_ENV} not required for this role")
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.hostname:
        return CheckResult("api_url", False, f"{API_URL_ENV} must be an absolute URL (got {value!r})")
    return CheckResult("api_url", True, f"{API_URL_ENV} configured")


def validate_telemetry_settings() -> CheckResult:
    disabled = os.environ.get(TELEMETRY_DISABLED_ENV, "true").strip().lower() not in ("false", "0", "no")
    if disabled:
        return CheckResult("telemetry", True, "telemetry export disabled")
    endpoint = os.environ.get(OTLP_ENDPOINT_ENV)
    if not endpoint or not urlparse(endpoint).scheme:
        return CheckResult(
            "telemetry",
            False,
            f"telemetry is enabled but {OTLP_ENDPOINT_ENV} is missing or malformed",
        )
    return CheckResult("telemetry", True, f"telemetry endpoint configured")


def run_preflight(*, require_master_key: bool = True) -> list[CheckResult]:
    return [
        validate_database_url(),
        validate_artifact_root(),
        validate_master_key(required=require_master_key),
        validate_api_url(),
        validate_telemetry_settings(),
    ]


def format_report(results: list[CheckResult]) -> str:
    lines = [f"[{'OK' if result.ok else 'FAIL'}] {result.name}: {result.detail}" for result in results]
    return "\n".join(lines)
