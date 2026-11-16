"""Database connection helpers and schema migrations."""

from __future__ import annotations

from importlib import resources

import psycopg
from psycopg.rows import dict_row

_MIGRATION_LOCK = 727_001  # arbitrary advisory lock key, so concurrent starters migrate one at a time


def connect(url: str) -> psycopg.Connection:
    """Open an autocommit connection; use `with conn.transaction():` for multi-statement units."""
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def _migrations() -> list[tuple[int, str, str]]:
    """(version, name, sql) for every `migrations/NNNN_name.sql`, in order."""
    found = []
    for entry in resources.files("radfetch").joinpath("migrations").iterdir():
        if entry.name.endswith(".sql"):
            found.append((int(entry.name.split("_", 1)[0]), entry.name, entry.read_text()))
    return sorted(found)


def init_schema(conn: psycopg.Connection, through: int | None = None) -> list[str]:
    """Apply migrations that have not run yet, each in its own transaction. Returns the names applied.

    To change the schema, add the next numbered file under migrations/; never edit an applied one.
    `through` stops after that version; it exists for testing migrations of existing data.
    """
    applied_now = []
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        conn.execute(
            "create table if not exists schema_migrations"
            " (version int primary key, name text not null, applied_at timestamptz not null default now())"
        )
        done = {row["version"] for row in conn.execute("select version from schema_migrations").fetchall()}
        for version, name, sql in _migrations():
            if version in done or (through is not None and version > through):
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute("insert into schema_migrations (version, name) values (%s, %s)", (version, name))
            applied_now.append(name)
    return applied_now
