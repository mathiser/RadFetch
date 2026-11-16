"""The gateway: the only component that talks to the PACS.

It runs as a single process doing one PACS operation at a time, which is what
caps the load at one association. Each step either resolves one job
(accession -> study -> series, via C-FIND) or retrieves one series into the cache.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import time
import uuid
from datetime import datetime
from collections.abc import Callable

from radfetch import store
from radfetch.config import Settings
from radfetch.loop import run_steps
from radfetch.flows import FlowError, load_flow
from radfetch.gateway.pacs import PacsClient, PacsError, valid_uid
from radfetch.gateway.throttle import CircuitBreaker, TimeWindows, TokenBucket

log = logging.getLogger("radfetch.gateway")


def _disk_fraction(path) -> float:
    usage = shutil.disk_usage(path)
    return usage.used / usage.total


class Gateway:
    def __init__(
        self,
        conn: store.Conn,
        pacs: PacsClient,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], None] = time.sleep,
        disk_fraction: Callable[[], float] | None = None,
    ):
        self.conn, self.pacs, self.s = conn, pacs, settings
        self.owner = f"gateway-{socket.gethostname()}-{os.getpid()}"
        self._clock, self._now, self._sleep = clock, now, sleep
        self._disk_fraction = disk_fraction or (lambda: _disk_fraction(settings.cache_dir))
        self.windows = TimeWindows(settings.batch_windows)
        self.bucket = TokenBucket(settings.rate_instances_per_min, clock=clock)
        self.breaker = CircuitBreaker(
            settings.breaker_failure_threshold,
            settings.breaker_base_cooldown_s,
            settings.breaker_max_cooldown_s,
            clock=clock,
        )
        self._last_op = float("-inf")
        self._last_heartbeat = float("-inf")
        self._outage_notified = False
        self._tmp = settings.cache_dir / ".tmp"
        self._leading = False

    # --- loop ------------------------------------------------------------------------

    def run_forever(self) -> None:
        log.info("gateway started as %s (retrieve mode: %s)", self.owner, self.s.retrieve_mode)
        run_steps(self.step, self.s.idle_sleep_s, self._sleep)

    def step(self) -> bool:
        """Do at most one unit of PACS work. Returns False when there was nothing admissible."""
        if not self._lead():
            return False
        self._heartbeat()
        if not self.breaker.allow():
            return False
        return self.resolve_one() or self.retrieve_one()

    def _lead(self) -> bool:
        """Only one gateway may talk to the PACS. Further instances stand by and take over if it goes away."""
        if self._leading:
            return True
        if not store.try_lead(self.conn, "gateway"):
            return False
        self._leading = True
        # Leftovers from a crash mid-retrieval; nothing references them. Safe now that we are the only one.
        shutil.rmtree(self._tmp, ignore_errors=True)
        self._tmp.mkdir(parents=True, exist_ok=True)
        log.info("%s is now the active gateway", self.owner)
        return True

    def _heartbeat(self) -> None:
        """Publish gateway state for /healthz and /metrics, at most every 10 seconds."""
        if self._clock() - self._last_heartbeat < 10:
            return
        self._last_heartbeat = self._clock()
        store.set_status(
            self.conn,
            "gateway",
            breaker_open=self.breaker.is_open,
            consecutive_failures=self.breaker.failures,
            batch_window_open=self.windows.is_open(self._now()),
            rate_budget_ok=self.bucket.ready(),
            disk_used_fraction=round(self._disk_fraction(), 3),
        )

    def _pace(self) -> None:
        wait = self.s.min_interval_s - (self._clock() - self._last_op)
        if wait > 0:
            self._sleep(wait)

    def _pacs(self, fn, *args):
        self._pace()
        try:
            return fn(*args)
        finally:
            self._last_op = self._clock()

    # --- resolve ---------------------------------------------------------------------

    def resolve_one(self) -> bool:
        s = self.s
        batch_ok = (
            self.windows.is_open(self._now())
            and store.count_jobs(self.conn, "retrieving") < s.max_resolved_ahead
        )
        job = store.claim_job_to_resolve(self.conn, self.owner, s.lease_s, s.interactive_priority, batch_ok)
        if job is None:
            return False
        job_id, accession = job["id"], job["accession_number"]
        try:
            flow = load_flow(s.flows_dir, job["flow"])
        except FlowError as exc:
            store.fail_job(self.conn, job_id, str(exc), self.owner)
            return True
        try:
            studies = self._pacs(self.pacs.find_studies, accession)
            if not studies or (len(studies) > 1 and flow.multiple_studies == "fail"):
                self._pacs_ok()
                reason = "no study found" if not studies else f"{len(studies)} studies found, expected one"
                store.fail_job(self.conn, job_id, f"{reason} for accession number {accession}", self.owner)
                return True
            studies = sorted(studies, key=lambda st: st.study_instance_uid)
            study = studies[0]  # recorded on the job; with several studies it is the first by UID
            series = []
            for st in studies:
                series += self._pacs(self.pacs.find_series, st.study_instance_uid)
        except PacsError as exc:
            self._pacs_failure("resolve", exc)
            if job["attempts"] >= s.resolve_max_attempts:
                store.fail_job(self.conn, job_id, f"PACS query failed: {exc}", self.owner)
            else:
                store.retry_resolve(self.conn, job_id, self.owner, str(exc), s.backoff_s(job["attempts"]))
            return True
        self._pacs_ok()

        selected = flow.select_series(series)
        if not all(valid_uid(u.series_instance_uid) for u in selected) or not all(
            valid_uid(st.study_instance_uid) for st in studies
        ):
            store.fail_job(self.conn, job_id, "PACS returned a malformed UID", self.owner)
            return True
        if not selected:
            store.fail_job(
                self.conn, job_id, f"none of the study's {len(series)} series match flow {flow.name}", self.owner
            )
            return True
        store.resolve_job(self.conn, job_id, self.owner, study.study_instance_uid, flow.version, selected)
        log.info("resolved %s (%s): %d of %d series selected", accession, flow.name, len(selected), len(series))
        return True

    # --- retrieve --------------------------------------------------------------------

    def retrieve_one(self) -> bool:
        s = self.s
        disk = self._disk_fraction()
        if disk >= s.disk_hard_limit:
            return False
        batch_ok = (
            self.windows.is_open(self._now())
            and self.bucket.ready()
            and disk < s.disk_high_watermark
            and store.staged_backlog(self.conn) < s.max_staged_jobs
        )
        r = store.claim_retrieval(self.conn, self.owner, s.retrieve_lease_s, s.interactive_priority, batch_ok)
        if r is None:
            return False
        series_uid = r["series_instance_uid"]
        tmp = self._tmp / f"{series_uid}.{uuid.uuid4().hex}"
        try:
            try:
                result = self._pacs(self.pacs.retrieve_series, r["study_instance_uid"], series_uid, tmp)
            except PacsError as exc:
                self._pacs_failure("retrieve", exc)
                self._retrieval_failed(r, str(exc))
                return True
            self._pacs_ok()
            self.bucket.consume(result.received)

            expected = r["expected_instances"]
            if result.failed or result.received == 0 or (expected is not None and result.received < expected):
                self._retrieval_failed(
                    r, f"incomplete: received {result.received}, expected {expected}, failed {result.failed}"
                )
                return True

            final = s.cache_dir / series_uid
            shutil.rmtree(final, ignore_errors=True)  # stale copy from before an eviction or cache miss
            os.replace(tmp, final)
            promoted = store.mark_staged(self.conn, series_uid, self.owner, result.received, result.bytes)
            log.info("staged series %s (%d instances), %d job(s) ready", series_uid, result.received, len(promoted))
            return True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _retrieval_failed(self, r, error: str) -> None:
        final = store.retrieval_failed(
            self.conn,
            r["series_instance_uid"],
            self.owner,
            error,
            self.s.retrieve_max_attempts,
            self.s.backoff_s(r["attempts"]),
        )
        log.warning("retrieval of %s failed%s: %s", r["series_instance_uid"], " permanently" if final else "", error)

    def _pacs_ok(self) -> None:
        self.breaker.record_success()
        self._outage_notified = False

    def _pacs_failure(self, what: str, exc: Exception) -> None:
        self.breaker.record_failure()
        if self.breaker.is_open and not self._outage_notified:
            # One message per outage: the flag is cleared by the next successful PACS operation.
            self._outage_notified = True
            store.enqueue_notification(self.conn, "pacs_unreachable", failures=self.breaker.failures, error=str(exc))
        if self.breaker.is_open:
            log.error("circuit breaker open after %d PACS failures (%s: %s)", self.breaker.failures, what, exc)
