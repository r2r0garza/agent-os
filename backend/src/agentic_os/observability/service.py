from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from agentic_os.domain.models import (
    Goal,
    ObservabilityRecord,
    Project,
    Run,
    Task,
    TelemetryExportAttempt,
    TelemetryExportSetting,
)

logger = logging.getLogger("agentic_os.observability")
_REQUEST_CONTEXT: ContextVar[CorrelationContext | None] = ContextVar(
    "agentic_os_request_correlation", default=None
)
_METRICS: Counter[tuple[str, str]] = Counter()
_SENSITIVE_FRAGMENTS = (
    "authorization",
    "api-key",
    "api_key",
    "cookie",
    "credential",
    "material",
    "password",
    "secret",
    "token",
)


class TelemetryExporter(Protocol):
    """Transport boundary for optional OpenTelemetry/Langfuse-style export."""

    def __call__(self, destination: str, payload: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class CorrelationContext:
    correlation_id: uuid.UUID
    trace_id: str
    request_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    goal_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None

    @classmethod
    def for_request(cls, request_id: uuid.UUID | None = None) -> CorrelationContext:
        request_id = request_id or uuid.uuid4()
        return cls(
            correlation_id=request_id,
            request_id=request_id,
            trace_id=_trace_id(request_id),
        )

    @classmethod
    def for_run(
        cls,
        *,
        project: Project,
        goal: Goal,
        task: Task,
        run: Run,
    ) -> CorrelationContext:
        # A task-derived identity intentionally survives run retries and worker
        # restarts; each individual event still receives its own span id.
        correlation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"agentic-os:task:{task.id}")
        trace_id = _trace_id(correlation_id)
        snapshot = dict(run.snapshot or {})
        snapshot.setdefault("correlation_id", str(correlation_id))
        snapshot.setdefault("trace_id", trace_id)
        run.snapshot = snapshot
        return cls(
            correlation_id=correlation_id,
            trace_id=trace_id,
            team_id=project.team_id,
            user_id=goal.created_by,
            project_id=project.id,
            goal_id=goal.id,
            task_id=task.id,
            run_id=run.id,
        )


def _trace_id(value: uuid.UUID) -> str:
    return hashlib.sha256(value.bytes).hexdigest()[:32]


def current_request_context() -> CorrelationContext | None:
    return _REQUEST_CONTEXT.get()


@contextmanager
def request_correlation_scope(
    request_id: uuid.UUID | None = None,
) -> Iterator[CorrelationContext]:
    context = CorrelationContext.for_request(request_id)
    token = _REQUEST_CONTEXT.set(context)
    try:
        yield context
    finally:
        _REQUEST_CONTEXT.reset(token)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if any(fragment in str(key).lower() for fragment in _SENSITIVE_FRAGMENTS)
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _export_setting(
    session: Session, context: CorrelationContext
) -> TelemetryExportSetting | None:
    if context.team_id is None and context.project_id is None:
        return None
    return session.execute(
        select(TelemetryExportSetting)
        .where(
            TelemetryExportSetting.team_id == context.team_id,
            (
                (TelemetryExportSetting.project_id == context.project_id)
                | (TelemetryExportSetting.project_id.is_(None))
            ),
        )
        .order_by(
            case((TelemetryExportSetting.project_id == context.project_id, 0), else_=1),
            TelemetryExportSetting.created_at.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def record_observability(
    session: Session,
    context: CorrelationContext,
    *,
    event_kind: str,
    operation_name: str,
    status: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    parent_span_id: str | None = None,
    audit_event_id: uuid.UUID | None = None,
    cost_ledger_entry_id: uuid.UUID | None = None,
    approval_request_id: uuid.UUID | None = None,
    approval_decision_id: uuid.UUID | None = None,
    artifact_id: uuid.UUID | None = None,
    artifact_version_id: uuid.UUID | None = None,
    model_call_id: uuid.UUID | None = None,
    tool_call_id: uuid.UUID | None = None,
    mcp_call_id: uuid.UUID | None = None,
    sandbox_id: uuid.UUID | None = None,
    checkpoint_id: uuid.UUID | None = None,
) -> ObservabilityRecord:
    """Persist canonical evidence and enqueue optional export in one transaction.

    The function never calls an external exporter. A pending delivery attempt
    is processed later by :func:`deliver_pending_telemetry`, after the product
    transaction has committed.
    """
    safe_attributes = _redact(dict(attributes or {}))
    setting = _export_setting(session, context)
    capture_policy = {
        "capture_prompts": bool(setting and setting.capture_prompts),
        "capture_outputs": bool(setting and setting.capture_outputs),
    }
    record = ObservabilityRecord(
        correlation_id=context.correlation_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=parent_span_id,
        event_kind=event_kind,
        operation_name=operation_name,
        status=status,
        team_id=context.team_id,
        user_id=context.user_id,
        project_id=context.project_id,
        goal_id=context.goal_id,
        task_id=context.task_id,
        run_id=context.run_id,
        audit_event_id=audit_event_id,
        cost_ledger_entry_id=cost_ledger_entry_id,
        approval_request_id=approval_request_id,
        approval_decision_id=approval_decision_id,
        artifact_id=artifact_id,
        artifact_version_id=artifact_version_id,
        model_call_id=model_call_id,
        tool_call_id=tool_call_id,
        mcp_call_id=mcp_call_id,
        sandbox_id=sandbox_id,
        checkpoint_id=checkpoint_id,
        attributes=safe_attributes,
        capture_policy_evidence=capture_policy,
        redaction_evidence={
            "policy": "sensitive-key-redaction-v1",
            "applied": True,
            **_redact(setting.redaction_policy_evidence if setting else {}),
        },
    )
    session.add(record)
    session.flush()

    enabled = bool(setting and setting.enabled)
    session.add(
        TelemetryExportAttempt(
            observability_record_id=record.id,
            export_setting_id=setting.id if setting else None,
            destination=setting.exporter_type if setting else "none",
            attempt_number=1,
            status="pending" if enabled else "disabled",
            delivery_evidence=(
                {"queued": True}
                if enabled
                else {"reason": "export setting disabled" if setting else "no export setting"}
            ),
        )
    )
    _METRICS[(event_kind, status or "unknown")] += 1
    logger.info(
        json.dumps(
            {
                "event": operation_name,
                "kind": event_kind,
                "status": status,
                "correlation_id": str(context.correlation_id),
                "trace_id": context.trace_id,
                "record_id": str(record.id),
                "attributes": safe_attributes,
            },
            sort_keys=True,
        )
    )
    return record


def deliver_pending_telemetry(
    session: Session,
    exporter: TelemetryExporter,
    *,
    limit: int = 100,
) -> tuple[int, int]:
    """Deliver committed pending records without coupling export to product state."""
    pending = list(
        session.execute(
            select(TelemetryExportAttempt, ObservabilityRecord)
            .join(
                ObservabilityRecord,
                ObservabilityRecord.id == TelemetryExportAttempt.observability_record_id,
            )
            .where(TelemetryExportAttempt.status.in_(("pending", "delayed")))
            .order_by(TelemetryExportAttempt.created_at)
            .limit(limit)
        ).all()
    )
    payloads = [
        (
            attempt.id,
            attempt.destination,
            {
                "record": _redact(
                    {
                        **asdict(
                            CorrelationContext(
                                correlation_id=record.correlation_id,
                                request_id=record.request_id,
                                trace_id=record.trace_id or "",
                                team_id=record.team_id,
                                user_id=record.user_id,
                                project_id=record.project_id,
                                goal_id=record.goal_id,
                                task_id=record.task_id,
                                run_id=record.run_id,
                            )
                        ),
                        "id": record.id,
                        "span_id": record.span_id,
                        "parent_span_id": record.parent_span_id,
                        "event_kind": record.event_kind,
                        "operation_name": record.operation_name,
                        "status": record.status,
                        "occurred_at": record.occurred_at,
                        "attributes": record.attributes,
                    }
                )
            },
        )
        for attempt, record in pending
    ]
    # End the read transaction before invoking an external sink. Export
    # failures therefore cannot roll back the canonical product transaction.
    session.commit()

    delivered = failed = 0
    for attempt_id, destination, payload in payloads:
        now = datetime.now(timezone.utc)
        try:
            exporter(destination, payload)
        except Exception as error:  # external adapters are deliberately isolated
            attempt = session.get(TelemetryExportAttempt, attempt_id)
            if attempt is not None:
                attempt.status = "failed"
                attempt.last_attempted_at = now
                attempt.failure_code = type(error).__name__
                attempt.failure_message = str(error)[:1000]
                attempt.delivery_evidence = {"isolated_from_product_transaction": True}
                session.commit()
            failed += 1
            continue
        attempt = session.get(TelemetryExportAttempt, attempt_id)
        if attempt is not None:
            attempt.status = "delivered"
            attempt.last_attempted_at = now
            attempt.delivered_at = now
            attempt.failure_code = None
            attempt.failure_message = None
            attempt.delivery_evidence = {"delivered": True}
            session.commit()
        delivered += 1
    return delivered, failed
