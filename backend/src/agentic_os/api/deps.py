from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from agentic_os.domain import create_database_engine, session_factory


@lru_cache(maxsize=1)
def _engine() -> Engine:
    return create_database_engine()


def get_session() -> Iterator[Session]:
    session = session_factory(_engine())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
