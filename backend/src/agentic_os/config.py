"""Local and remote/team configuration and master-key validation.

Startup/preflight checks for the local Compose deployment and, when
``AGENTIC_OS_DEPLOYMENT_MODE=team``, the team VM deployment described in
``docs/team-vm-deployment.md``: required environment, master-key material,
TLS/proxy origin assumptions, durable backup destinations, and optional
telemetry settings. Checks fail closed and never surface raw secret material
in their diagnostics.
"""
from __future__ import annotations

import os
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
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
DEPLOYMENT_MODE_ENV = "AGENTIC_OS_DEPLOYMENT_MODE"
PUBLIC_ORIGIN_ENV = "AGENTIC_OS_PUBLIC_ORIGIN"
BACKUP_ROOT_ENV = "AGENTIC_OS_BACKUP_ROOT"

LOCAL_DEPLOYMENT_MODE = "local"
TEAM_DEPLOYMENT_MODE = "team"
VALID_DEPLOYMENT_MODES = (LOCAL_DEPLOYMENT_MODE, TEAM_DEPLOYMENT_MODE)

REQUIRED_POSTGRES_TOOLS = ("pg_dump", "pg_restore", "pg_isready")

_INSECURE_MODE_BITS = stat.S_IRWXG | stat.S_IRWXO
_REMOTE_URI_MARKER = "://"


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

    key_file_value = environment.get(MASTER_KEY_FILE_ENV) or DEFAULT_MASTER_KEY_FILE
    if _REMOTE_URI_MARKER in key_file_value:
        raise ConfigurationError(
            f"{MASTER_KEY_FILE_ENV} must be a local/mounted durable filesystem path, never object "
            f"storage, since the master key requires POSIX file permissions (got {key_file_value!r})"
        )
    key_path = Path(key_file_value)
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
    if configured and _REMOTE_URI_MARKER in configured:
        scheme = urlparse(configured).scheme or configured.split(_REMOTE_URI_MARKER, 1)[0]
        return CheckResult(
            "artifact_root",
            False,
            f"artifact root object-storage backends ({scheme}://) are not yet supported by this "
            f"deployment; configure a durable local/mounted filesystem path (see VISION.md's "
            f"object-storage abstraction) (got {configured!r})",
        )
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


def resolve_deployment_mode(*, env: dict[str, str] | None = None) -> str:
    environment = env if env is not None else os.environ
    return (environment.get(DEPLOYMENT_MODE_ENV) or LOCAL_DEPLOYMENT_MODE).strip().lower()


def validate_deployment_mode(*, mode: str | None = None, env: dict[str, str] | None = None) -> CheckResult:
    resolved = mode if mode is not None else resolve_deployment_mode(env=env)
    if resolved not in VALID_DEPLOYMENT_MODES:
        return CheckResult(
            "deployment_mode",
            False,
            f"{DEPLOYMENT_MODE_ENV} must be one of {VALID_DEPLOYMENT_MODES!r} (got {resolved!r})",
        )
    return CheckResult("deployment_mode", True, f"deployment mode is {resolved!r}")


def validate_master_key(
    *, required: bool = True, mode: str | None = None, env: dict[str, str] | None = None
) -> CheckResult:
    environment = env if env is not None else os.environ
    resolved_mode = mode if mode is not None else resolve_deployment_mode(env=environment)
    effective_required = required or resolved_mode == TEAM_DEPLOYMENT_MODE
    try:
        key = resolve_master_key(env=environment)
    except ConfigurationError as error:
        return CheckResult("master_key", False, str(error))
    if key is None:
        if effective_required:
            reason = (
                "team deployments never fall back to an ephemeral key"
                if resolved_mode == TEAM_DEPLOYMENT_MODE
                else "this role"
            )
            return CheckResult(
                "master_key",
                False,
                f"no master key configured; set {MASTER_KEY_ENV} or run "
                f"'agentic-os config generate-master-key' ({reason})",
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


def validate_public_origin(*, mode: str | None = None, env: dict[str, str] | None = None) -> CheckResult:
    """Validate the TLS-terminated proxy origin required for team VM deployments.

    Per docs/team-vm-deployment.md the proxy is the only public entry point
    and must terminate TLS before forwarding to `frontend`/`api`; this check
    fails closed rather than allowing a plaintext public origin.
    """
    environment = env if env is not None else os.environ
    resolved_mode = mode if mode is not None else resolve_deployment_mode(env=environment)
    value = environment.get(PUBLIC_ORIGIN_ENV)
    if not value:
        if resolved_mode == TEAM_DEPLOYMENT_MODE:
            return CheckResult(
                "public_origin",
                False,
                f"{PUBLIC_ORIGIN_ENV} is required for team deployment; set the TLS-terminated proxy "
                "origin (for example https://team.example.com); see docs/team-vm-deployment.md",
            )
        return CheckResult("public_origin", True, "not required for local deployment")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        return CheckResult(
            "public_origin",
            False,
            f"{PUBLIC_ORIGIN_ENV} must be an https:// URL with a hostname; the proxy terminates TLS "
            f"at the edge and must never forward plaintext to the public internet (got {value!r})",
        )
    return CheckResult("public_origin", True, f"public origin {parsed.hostname!r} configured over TLS")


def validate_backup_destination(*, env: dict[str, str] | None = None) -> CheckResult:
    """Validate the optional durable backup destination directory.

    Not required for local deployments (operators pass `--output` per
    invocation); when configured, or when running in team mode, it must be a
    local/mounted durable path distinct from object storage so backup
    archives land on durable, permission-controlled storage.
    """
    environment = env if env is not None else os.environ
    value = environment.get(BACKUP_ROOT_ENV)
    if not value:
        return CheckResult(
            "backup_destination",
            True,
            f"{BACKUP_ROOT_ENV} not configured; pass --output explicitly to 'operations backup'",
        )
    if _REMOTE_URI_MARKER in value:
        return CheckResult(
            "backup_destination",
            False,
            f"{BACKUP_ROOT_ENV} must be a local/mounted durable filesystem path, not a remote URI "
            f"(got {value!r})",
        )
    target = Path(value)
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".agentic-os-write-check"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as error:
        return CheckResult("backup_destination", False, f"{target} is not writable: {error.strerror or error}")
    return CheckResult("backup_destination", True, f"{target} is writable")


def validate_postgres_tools() -> CheckResult:
    missing = [tool for tool in REQUIRED_POSTGRES_TOOLS if shutil.which(tool) is None]
    if missing:
        return CheckResult(
            "postgres_tools",
            False,
            "required PostgreSQL client tools are unavailable: " + ", ".join(missing),
        )
    return CheckResult(
        "postgres_tools",
        True,
        "pg_dump, pg_restore, and pg_isready are available for backup/restore operations",
    )


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


def run_preflight(
    *, require_master_key: bool = True, deployment_mode: str | None = None
) -> list[CheckResult]:
    resolved_mode = deployment_mode if deployment_mode is not None else resolve_deployment_mode()
    checks = [
        validate_deployment_mode(mode=resolved_mode),
        validate_database_url(),
        validate_artifact_root(),
        validate_master_key(required=require_master_key, mode=resolved_mode),
        validate_api_url(),
        validate_public_origin(mode=resolved_mode),
        validate_telemetry_settings(),
    ]
    if resolved_mode == TEAM_DEPLOYMENT_MODE:
        checks.append(validate_backup_destination())
        checks.append(validate_postgres_tools())
    return checks


def format_report(results: list[CheckResult]) -> str:
    lines = [f"[{'OK' if result.ok else 'FAIL'}] {result.name}: {result.detail}" for result in results]
    return "\n".join(lines)


def preflight_evidence(results: list[CheckResult]) -> list[dict[str, Any]]:
    """Structured, JSON-safe evidence for operations commands and admin views.

    Mirrors `format_report` field-for-field; never includes raw secret
    material since `CheckResult.detail` never carries it.
    """
    return [asdict(result) for result in results]
