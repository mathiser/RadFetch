"""The janitor: evicts unused cached series and removes old run directories."""

from __future__ import annotations

import logging
import shutil
import uuid

from radfetch import store
from radfetch.config import Settings
from radfetch.loop import run_steps

log = logging.getLogger("radfetch.janitor")


class Janitor:
    def __init__(self, conn: store.Conn, settings: Settings, *, disk_fraction=None):
        self.conn, self.s = conn, settings
        self._disk_fraction = disk_fraction or self._measure
        self._trash = settings.cache_dir / ".trash"

    def _measure(self) -> float:
        usage = shutil.disk_usage(self.s.cache_dir)
        return usage.used / usage.total

    def run_forever(self) -> None:
        def step() -> bool:
            self.sweep()
            return False

        run_steps(step, self.s.janitor_interval_s)

    def sweep(self) -> None:
        self.s.cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self._trash, ignore_errors=True)
        evicted = self._evict(
            store.eviction_candidates(self.conn, self.s.cache_ttl_hours), self.s.keep_failed_hours
        )
        if self._disk_fraction() >= self.s.disk_high_watermark:
            # Under disk pressure the TTL no longer protects anything that no active job needs.
            for series_uid in store.eviction_candidates(self.conn, 0):
                if self._disk_fraction() < self.s.disk_high_watermark:
                    break
                evicted += self._evict([series_uid], 0)
        promoted = store.promote_ready_jobs(self.conn)
        if promoted:
            log.warning("%d job(s) had all series staged but were still waiting; moved to ready", promoted)
        store.prune_status(self.conn)
        purged = self._purge_runs()
        if evicted or purged:
            log.info("evicted %d series, purged %d run directories", evicted, purged)

    def _evict(self, series_uids: list[str], keep_failed_hours: float) -> int:
        n = 0
        for series_uid in series_uids:
            path = self.s.cache_dir / series_uid

            def remove_files(path=path):
                # Atomic rename while the row lock is held; the slow delete happens afterwards.
                if path.exists():
                    self._trash.mkdir(parents=True, exist_ok=True)
                    path.rename(self._trash / f"{path.name}.{uuid.uuid4().hex}")

            if store.evict_retrieval(self.conn, series_uid, remove_files, keep_failed_hours):
                n += 1
        shutil.rmtree(self._trash, ignore_errors=True)
        return n

    def _purge_runs(self) -> int:
        n = 0
        if not self.s.runs_dir.is_dir():
            return 0
        for run_dir in self.s.runs_dir.iterdir():
            try:
                run_id = uuid.UUID(run_dir.name)
            except ValueError:
                continue
            if store.purgeable_run(self.conn, run_id, self.s.run_ttl_hours, self.s.keep_failed_hours):
                shutil.rmtree(run_dir, ignore_errors=True)
                n += 1
        return n
