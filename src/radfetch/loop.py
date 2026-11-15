"""Shared worker loop with database reconnection."""

from __future__ import annotations

import logging
import time
from typing import Callable

import psycopg

from radfetch import db

log = logging.getLogger("radfetch")


def run_steps(step: Callable[[], bool], idle_sleep_s: float, sleep: Callable[[float], None] = time.sleep) -> None:
    """Call step() forever. Unexpected errors are logged; a lost database connection is raised."""
    while True:
        try:
            worked = step()
        except psycopg.OperationalError:
            raise
        except Exception:
            log.exception("step failed")
            worked = False
        if not worked:
            sleep(idle_sleep_s)


def serve(database_url: str, make_worker: Callable[[psycopg.Connection], object]) -> None:
    """Run make_worker(conn).run_forever(), reconnecting when the database connection drops."""
    while True:
        try:
            conn = db.connect(database_url)
        except psycopg.OperationalError as exc:
            log.error("database unavailable: %s", exc)
            time.sleep(5)
            continue
        try:
            make_worker(conn).run_forever()
        except psycopg.OperationalError as exc:
            log.error("database connection lost, reconnecting: %s", exc)
            time.sleep(5)
        finally:
            conn.close()
