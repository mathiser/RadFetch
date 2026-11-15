"""The executor: runs one flow container at a time (one worker process = one GPU)."""

from __future__ import annotations

import logging
import os
import shutil
import socket
import time
from pathlib import Path
from typing import Callable

from radfetch import store
from radfetch.config import Settings
from radfetch.loop import run_steps
from radfetch.executor.runtime import ContainerSpec, Runtime, RuntimeError_
from radfetch.flows import Flow, FlowError, load_flow

log = logging.getLogger("radfetch.executor")


class CacheMiss(Exception):
    def __init__(self, series: list[str]):
        super().__init__(f"series missing from cache: {series}")
        self.series = series


class Executor:
    def __init__(
        self,
        conn: store.Conn,
        runtime: Runtime,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.conn, self.runtime, self.s = conn, runtime, settings
        self.owner = f"executor-{socket.gethostname()}-{os.getpid()}"
        self._clock, self._sleep = clock, sleep
        settings.runs_dir.mkdir(parents=True, exist_ok=True)

    def run_forever(self) -> None:
        log.info("executor started as %s", self.owner)
        if self.s.debug_fake_gpu:
            log.warning("RADFETCH_DEBUG_FAKE_GPU is set: gpu flows run WITHOUT a GPU. Do not use in production.")
        run_steps(self.step, self.s.idle_sleep_s, self._sleep)

    def step(self) -> bool:
        job = store.claim_job_to_run(self.conn, self.owner, self.s.lease_s)
        if job is None:
            return False
        try:
            flow = load_flow(self.s.flows_dir, job["flow"])
        except FlowError as exc:
            store.fail_job(self.conn, job["id"], str(exc), self.owner)
            return True

        run = handle = None
        if job["prev_state"] == "running":
            # The previous executor died. Its container may still be running or have finished.
            run, handle = self._adopt(job)
        if handle is None:
            started = self._start(job, flow)
            if started is None:
                return True
            run, handle = started
        self._supervise(job, flow, run, handle)
        return True

    # --- start -------------------------------------------------------------------------

    def _adopt(self, job):
        run = store.latest_run(self.conn, job["id"])
        if run is None or run["state"] != "running":
            return None, None
        handle = self.runtime.find(str(run["id"]))
        if handle is None:
            store.finish_run(self.conn, run["id"], "lost", error="container not found after executor restart")
            return None, None
        log.info("adopted container of run %s for job %s", run["id"], job["id"])
        return run, handle

    def _start(self, job, flow: Flow):
        job_id = job["id"]
        if store.count_runs(self.conn, job_id) >= flow.max_attempts:
            store.fail_job(self.conn, job_id, f"gave up after {flow.max_attempts} run attempts", self.owner)
            return None
        run = store.create_run(self.conn, job_id, flow.image)
        run_dir = self.s.runs_dir / str(run["id"])
        try:
            input_dir, output_dir = self._prepare(job_id, run_dir)
        except CacheMiss as miss:
            store.delete_run(self.conn, run["id"])  # never started: must not count as an attempt
            store.requeue_for_retrieval(self.conn, job_id, self.owner, miss.series)
            shutil.rmtree(run_dir, ignore_errors=True)
            return None
        spec = ContainerSpec(
            run_id=str(run["id"]),
            job_id=str(job_id),
            image=flow.image,
            command=flow.command,
            input_dir=input_dir,
            output_dir=output_dir,
            env={
                **flow.env,
                "RADFETCH_JOB_ID": str(job_id),
                "RADFETCH_RUN_ID": str(run["id"]),
                "RADFETCH_ACCESSION_NUMBER": job["accession_number"],
                "RADFETCH_STUDY_INSTANCE_UID": job["study_instance_uid"] or "",
                "RADFETCH_FLOW": flow.name,
            },
            gpu=flow.gpu,
            mem_limit=flow.mem_limit,
            shm_size=flow.shm_size,
            network=self.s.container_network,
        )
        try:
            handle = self.runtime.start(spec)
        except RuntimeError_ as exc:
            self._fail_run(job, flow, run, str(exc), None)
            return None
        return run, handle

    def _prepare(self, job_id, run_dir: Path) -> tuple[Path, Path]:
        """Build /input from hardlinks into the cache: no copy, and eviction cannot pull files from under a run."""
        input_dir, output_dir = run_dir / "in", run_dir / "out"
        shutil.rmtree(run_dir, ignore_errors=True)
        output_dir.mkdir(parents=True)
        os.chmod(output_dir, 0o777)  # flow images often run as a non-root user
        missing = []
        for series_uid in store.job_series_uids(self.conn, job_id):
            src = self.s.cache_dir / series_uid
            files = sorted(src.glob("*.dcm")) if src.is_dir() else []
            if not files:
                missing.append(series_uid)
                continue
            dst = input_dir / series_uid
            dst.mkdir(parents=True)
            try:
                for f in files:
                    os.link(f, dst / f.name)
            except FileNotFoundError:  # evicted while linking
                missing.append(series_uid)
        if missing:
            raise CacheMiss(missing)
        return input_dir, output_dir

    # --- supervise ---------------------------------------------------------------------

    def _supervise(self, job, flow: Flow, run, handle: str) -> None:
        job_id, run_id = job["id"], run["id"]
        run_dir = self.s.runs_dir / str(run_id)
        deadline = self._clock() + flow.timeout_s
        # The container is removed only once its outcome is recorded. If we fail unexpectedly
        # (say the database drops mid-run) it keeps running and is adopted after the lease expires.
        remove = False
        try:
            while True:
                try:
                    code = self.runtime.exit_code(handle)
                except RuntimeError_ as exc:
                    self._fail_run(job, flow, run, str(exc), None)
                    remove = True
                    return
                if code is not None:
                    break
                if not store.renew_job_lease(self.conn, job_id, self.owner, self.s.lease_s):
                    current = store.get_job(self.conn, job_id)
                    if current is not None and current["state"] == "running":
                        # We stalled and another executor adopted this container: it is theirs now.
                        log.warning("job %s was taken over; leaving run %s to the new owner", job_id, run_id)
                        return
                    self.runtime.kill(handle)
                    store.finish_run(self.conn, run_id, "cancelled")
                    remove = True
                    log.info("job %s was cancelled; stopped run %s", job_id, run_id)
                    return
                if self._clock() >= deadline:
                    self.runtime.kill(handle)
                    self._save_logs(handle, run_dir)
                    self._fail_run(job, flow, run, f"timed out after {flow.timeout_s}s", None)
                    remove = True
                    return
                self._sleep(self.s.executor_poll_s)

            self._save_logs(handle, run_dir)
            if code == 0:
                destinations = [d.model_dump() for d in flow.destinations]
                store.run_succeeded(self.conn, job_id, run_id, self.owner, destinations)
                log.info("job %s: run %s succeeded", job_id, run_id)
            else:
                self._fail_run(job, flow, run, f"container exited with code {code}", code)
            remove = True
        finally:
            if remove:
                self.runtime.remove(handle)

    def _fail_run(self, job, flow: Flow, run, error: str, exit_code: int | None) -> None:
        retry = store.count_runs(self.conn, job["id"]) < flow.max_attempts
        store.run_failed(
            self.conn, job["id"], run["id"], self.owner, error, exit_code, retry, self.s.backoff_s(run["attempt"])
        )
        log.warning("job %s: run %s failed (%s)%s", job["id"], run["id"], error, ", will retry" if retry else "")

    def _save_logs(self, handle: str, run_dir: Path) -> None:
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "container.log").write_bytes(self.runtime.logs(handle))
        except OSError:
            log.exception("could not save container log")
