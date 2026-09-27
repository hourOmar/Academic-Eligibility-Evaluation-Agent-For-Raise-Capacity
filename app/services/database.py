"""Read-only Supabase/PostgreSQL access.

All queries use quoted identifiers (the schema's table/column names are mixed-case) and
bound parameters. No INSERT/UPDATE/DELETE statements exist anywhere in this module or its
callers. The connection additionally requests a read-only transaction from Postgres itself
as a second line of defense.
"""

from functools import lru_cache

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from app import config
from app.exceptions import DatabaseUnavailableError


def _normalize_database_url(url: str) -> str:
    """Supabase's connection-string UI gives plain postgresql:// / postgres:// URLs, which
    make SQLAlchemy default to the psycopg2 driver. This project installs psycopg 3
    instead, so rewrite the scheme to the +psycopg dialect rather than requiring the user
    to hand-edit their .env."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    if not config.DATABASE_URL:
        raise DatabaseUnavailableError(
            "DATABASE_URL is not configured. Set it in .env before running any database-backed command."
        )
    try:
        engine = create_engine(
            _normalize_database_url(config.DATABASE_URL),
            pool_pre_ping=True,
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any engine-creation failure is "unavailable"
        raise DatabaseUnavailableError(f"Could not create the database engine: {exc}") from exc

    @event.listens_for(engine, "connect")
    def _enforce_read_only(dbapi_connection, _connection_record):
        # Supabase routes through PgBouncer in transaction-pooling mode, which silently
        # drops startup-packet options (e.g. connect_args={"options": "-c ..."}). Setting
        # it as a plain SQL statement on every new DBAPI connection works reliably instead.
        cursor = dbapi_connection.cursor()
        cursor.execute("SET default_transaction_read_only = on")
        cursor.close()
        dbapi_connection.commit()

    return engine


def get_connection():
    try:
        return get_engine().connect()
    except DatabaseUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailableError(f"Could not connect to the database: {exc}") from exc


def fetch_course(course_id: int) -> dict | None:
    query = text(
        'SELECT "Course_ID", "Course_Name", "Credit_Hours" FROM "Course" WHERE "Course_ID" = :cid'
    )
    with get_connection() as conn:
        row = conn.execute(query, {"cid": course_id}).mappings().first()
    return dict(row) if row is not None else None
