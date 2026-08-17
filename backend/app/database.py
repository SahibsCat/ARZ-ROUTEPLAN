import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# PostgreSQL (Neon) in production, via DATABASE_URL; falls back to a local
# SQLite file only when DATABASE_URL isn't set (local dev with no Postgres
# handy). Nothing else in this module (or anywhere that imports it) is
# database-specific.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./rootplan.db").strip()

# Some providers (Neon included, historically Heroku too) hand out
# "postgres://" URLs, a scheme SQLAlchemy 1.4+/2.x no longer accepts -
# normalize to "postgresql://" so either form works without editing the env
# var by hand.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

_is_sqlite = DATABASE_URL.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
_engine_kwargs = {} if _is_sqlite else {
    # Neon (and most managed Postgres) can close idle connections behind the
    # pool's back; pre_ping catches that with a cheap SELECT 1 before reuse
    # instead of surfacing a stale-connection error on the request that
    # happens to draw it, and recycle keeps connections from going stale in
    # the pool between requests.
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Creates any tables missing from the target database. Safe to call on
    every startup - existing tables/data are left untouched. Schema changes
    to existing tables go through Alembic migrations (see backend/alembic/),
    not this function."""
    from app import models  # noqa: F401 - ensures models are registered on Base before create_all

    Base.metadata.create_all(bind=engine)
