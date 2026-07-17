from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agentic_os.domain.models import AuditEvent
from agentic_os.observability import TelemetryExporter, deliver_pending_telemetry
from agentic_os.worker.leases import DEFAULT_LEASE_SECONDS
from agentic_os.worker.runner import run_task_worker_once

# How long an idle worker waits before re-attempting a claim while at least
# one sibling worker is still active. A task that lost a dependency or
# resource-key race becomes claimable again once the winning task completes,
# so an idle worker must keep polling rather than exit early.
_IDLE_POLL_SECONDS = 0.05

WORKER_HEARTBEAT_EVENT_TYPE = "worker.heartbeat"

# A worker process (or an operator-run `agentic-os worker run-once` loop) is
# considered live if it heartbeated within this window. This is deliberately
# generous relative to the local `worker-loop.sh` default 2-second poll
# cadence so a single slow claim/execute cycle never flips a healthy worker
# to "missing".
_HEARTBEAT_LIVE_SECONDS = 90

# A worker id observed in this longer window but not the live window above is
# reported as "missing" (it existed recently and stopped heartbeating)
# instead of being silently forgotten, which is what lets health evidence
# distinguish "never had that many workers" from "lost a worker".
_HEARTBEAT_HISTORY_SECONDS = 900


def record_worker_heartbeat(
    session: Session,
    worker_id_prefix: str,
    *,
    worker_count: int,
    claimed: int,
    error_count: int,
) -> None:
    """Persist proof that a worker process polled for work.

    Reuses the existing durable audit trail as the single source of
    worker-liveness truth rather than introducing a separate heartbeat
    table. Emitted once per ``run_scheduler_once`` invocation (i.e. once per
    outer poll cycle a process or shell loop runs), not per idle sub-loop
    iteration, so heartbeat volume tracks operator-configured poll cadence
    rather than the in-process idle-retry rate.
    """
    session.add(
        AuditEvent(
            event_type=WORKER_HEARTBEAT_EVENT_TYPE,
            payload={
                "worker_id_prefix": worker_id_prefix,
                "worker_count": worker_count,
                "claimed": claimed,
                "error_count": error_count,
            },
        )
    )
    session.flush()


@dataclass(frozen=True)
class WorkerHeartbeatSummary:
    """Worker-fleet liveness evidence derived from recent heartbeat events."""

    live_worker_ids: list[str]
    missing_worker_ids: list[str]
    configured_capacity: int


def summarize_worker_heartbeats(session: Session, *, now: datetime) -> WorkerHeartbeatSummary:
    """Summarize worker-process liveness from the last heartbeat each known
    worker id reported, without assuming a fixed or externally-configured
    worker count.
    """
    history_cutoff = now - timedelta(seconds=_HEARTBEAT_HISTORY_SECONDS)
    live_cutoff = now - timedelta(seconds=_HEARTBEAT_LIVE_SECONDS)
    rows = session.execute(
        select(AuditEvent.payload, AuditEvent.occurred_at)
        .where(
            AuditEvent.event_type == WORKER_HEARTBEAT_EVENT_TYPE,
            AuditEvent.occurred_at >= history_cutoff,
        )
        .order_by(AuditEvent.occurred_at.desc())
    ).all()

    latest_by_prefix: dict[str, tuple[dict, datetime]] = {}
    for payload, occurred_at in rows:
        prefix = payload.get("worker_id_prefix")
        if not prefix or prefix in latest_by_prefix:
            continue
        latest_by_prefix[prefix] = (payload, occurred_at)

    live_ids = sorted(
        prefix for prefix, (_, occurred_at) in latest_by_prefix.items() if occurred_at >= live_cutoff
    )
    missing_ids = sorted(
        prefix for prefix, (_, occurred_at) in latest_by_prefix.items() if occurred_at < live_cutoff
    )
    configured_capacity = sum(
        int(payload.get("worker_count") or 1)
        for prefix, (payload, occurred_at) in latest_by_prefix.items()
        if occurred_at >= live_cutoff
    )
    return WorkerHeartbeatSummary(
        live_worker_ids=live_ids, missing_worker_ids=missing_ids, configured_capacity=configured_capacity
    )


@dataclass
class SchedulerResult:
    """Outcome of one drain-the-queue scheduler invocation."""

    claimed: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_scheduler_once(
    session_maker: sessionmaker[Session],
    worker_id_prefix: str,
    *,
    worker_count: int = 1,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    on_run_started: Callable[[], None] | None = None,
    on_promoted: Callable[[], None] | None = None,
    telemetry_exporter: TelemetryExporter | None = None,
) -> SchedulerResult:
    """Run up to ``worker_count`` claim/execute loops concurrently until no
    ready task remains.

    Each concurrent worker owns its own database session/transaction per
    claimed task. Database-backed leases with fencing tokens (see
    ``claim_ready_task``) make concurrent claim attempts from independent
    worker threads safe, and the claim query's dependency and resource-key
    checks keep parallel execution limited to tasks that are actually safe to
    run at the same time. This provides local, in-process multi-worker
    semantics; cross-host worker orchestration is out of scope.

    A worker that finds nothing claimable does not exit immediately: another
    worker may currently hold the dependency or resource key that was
    blocking it, and that task becomes claimable again once that worker
    finishes. Workers only stop once every worker is simultaneously idle,
    i.e. a full round found no claimable work anywhere.
    """
    result = SchedulerResult()
    result_lock = threading.Lock()
    active_count = max(1, worker_count)
    quiescence_lock = threading.Lock()

    def worker_loop(worker_index: int) -> None:
        nonlocal active_count
        # A single worker keeps the caller-supplied id unchanged so callers
        # that key off a specific worker id (e.g. lease ownership checks)
        # see the same identity as before concurrent scheduling existed.
        worker_id = worker_id_prefix if worker_count <= 1 else f"{worker_id_prefix}-{worker_index}"
        idle = False
        while True:
            with session_maker() as session:
                try:
                    task = run_task_worker_once(
                        session,
                        worker_id,
                        lease_seconds=lease_seconds,
                        on_run_started=on_run_started,
                        on_promoted=on_promoted,
                    )
                except Exception as error:
                    session.commit()
                    with result_lock:
                        result.errors.append(str(error))
                    with quiescence_lock:
                        active_count -= 1
                    return
                session.commit()

            if task is None:
                if not idle:
                    idle = True
                    with quiescence_lock:
                        active_count -= 1
                with quiescence_lock:
                    still_active = active_count > 0
                if not still_active:
                    return
                time.sleep(_IDLE_POLL_SECONDS)
                continue

            if idle:
                idle = False
                with quiescence_lock:
                    active_count += 1
            with result_lock:
                result.claimed.append((str(task.id), task.status))

    threads = [
        threading.Thread(target=worker_loop, args=(index,), daemon=True)
        for index in range(max(1, worker_count))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with session_maker() as session:
        record_worker_heartbeat(
            session,
            worker_id_prefix,
            worker_count=max(1, worker_count),
            claimed=len(result.claimed),
            error_count=len(result.errors),
        )
        session.commit()
    if telemetry_exporter is not None:
        with session_maker() as session:
            deliver_pending_telemetry(session, telemetry_exporter)
    return result
