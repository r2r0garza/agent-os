from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from agentic_os.domain import create_database_engine, session_factory
from agentic_os.observability import current_request_context, record_observability


@lru_cache(maxsize=1)
def _engine() -> Engine:
    return create_database_engine()


def get_session() -> Iterator[Session]:
    session = session_factory(_engine())()
    try:
        yield session
        context = current_request_context()
        if context is not None:
            record_observability(
                session,
                context,
                event_kind="request",
                operation_name="api.request",
                status="completed",
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
