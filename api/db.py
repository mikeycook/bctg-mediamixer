"""
Database access for the content library API.

Uses PostgresInterpreter and psycopg2 rather than SQLAlchemy async, to stay
consistent with the rest of this repository and with the sync job. A
connection is opened per request, which is fine for an admin tool used by
one person and avoids a pool to reason about; revisit if this ever serves
real traffic.
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PostgresInterpreter import PostgresInterpreter  # noqa: E402


def parse_database_url(database_url):
    database_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    u = urlparse(database_url)
    if u.scheme not in ("postgresql", "postgres"):
        raise ValueError(f"Unsupported DB scheme: {u.scheme}")
    user = u.username or ""
    password = unquote(u.password or "")
    database = (u.path or "").lstrip("/")
    if not user or not database:
        raise ValueError("Could not parse user/database from DATABASE_URL")
    return {"user": user, "password": password, "host": u.hostname or "127.0.0.1",
            "port": str(u.port or 5432), "database": database}


def get_db():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL env var is required")
    db = PostgresInterpreter(**parse_database_url(database_url))
    db.connect()
    try:
        yield db
    finally:
        db.close()
