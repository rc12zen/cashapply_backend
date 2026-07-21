"""
cashapply_shared.db.session
=============================
Single engine/session factory shared by both apps (same DATABASE_URL).
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .settings import get_settings
from .models import Base

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, future=True)
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Session:
    """Use as: `with session_scope() as db: ...`"""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    """Create all tables. Use Alembic in real prod; this is the PoC bootstrap."""
    Base.metadata.create_all(bind=get_engine())
