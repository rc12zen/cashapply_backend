from .db.session import get_session_factory


def get_db():
    """FastAPI dependency — yields a SQLAlchemy session per request."""
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
