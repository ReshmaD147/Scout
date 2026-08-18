import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from scout.db.models import Base

DB_PATH = Path(__file__).resolve().parent.parent.parent.parent / "retail.db"


def normalize_database_url(url: str | None) -> str:
    """Return a SQLAlchemy URL with SQLite local default and Railway-friendly Postgres."""
    value = (url or "").strip()
    if not value:
        return f"sqlite:///{DB_PATH}"
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+psycopg://", 1)
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


def engine_connect_args(database_url: str) -> dict:
    if database_url.startswith("sqlite:"):
        return {"check_same_thread": False}
    return {}


DATABASE_URL = normalize_database_url(os.getenv("DATABASE_URL"))

engine = create_engine(DATABASE_URL, connect_args=engine_connect_args(DATABASE_URL))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Create all tables if they don't already exist."""
    Base.metadata.create_all(bind=engine)


def get_session() -> Session:
    """Yield a session; use as a FastAPI dependency later."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
