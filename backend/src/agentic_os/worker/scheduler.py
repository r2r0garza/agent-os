from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.orm import Session, sessionmaker

from agentic_os.worker.leases import DEFAULT_LEASE_SECONDS
from agentic_os.worker.runner import run_task_worker_once

# How long an idle worker waits before re-attempting a claim while at least
# one sibling worker is still active. A task that lost a dependency or
# resource-key race becomes claimable again once the winning task completes,
# so an idle worker must keep polling rather than exit early.
_IDLE_POLL_SECONDS = 0.05


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
    return result
