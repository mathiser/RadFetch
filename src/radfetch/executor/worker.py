"""The executor: runs one step's container at a time.

Executors claim single steps, not whole jobs. A `gpu` executor takes steps marked
gpu, a `cpu` executor takes the others, and `any` takes both. Steps of one job can
therefore run on different executors, and independent steps at the same time. One
executor process is one slot: run more processes for more parallel work.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import time
from pathlib import Path
from collections.abc import Callable

from radfetch import store
from radfetch.config import Settings
from radfetch.executor.runtime import ContainerSpec, Runtime, RuntimeError_
from radfetch.flows import SERIES, Flow, FlowError, Step, load_flow
from radfetch.loop import run_steps

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
        self.kind = settings.executor_kind
        self.owner = f"executor-{socket.gethostname()}-{os.getpid()}"
        self._clock, self._sleep = clock, sleep
        self._last_heartbeat = self._last_reap = float("-inf")
        settings.runs_dir.mkdir(parents=True, exist_ok=True)

    def run_forever(self) -> None:
        log.info("executor started as %s (kind: %s)", self.owner, self.kind)
        if self.s.debug_fake_gpu:
            log.warning("RADFETCH_DEBUG_FAKE_GPU is set: gpu flows run WITHOUT a GPU. Do not use in production.")
        run_steps(self.step, self.s.idle_sleep_s, self._sleep)

    # --- one unit of work --------------------------------------------------------------

    def step(self) -> bool:
        self._heartbeat(busy=False)
        self._reap()
        self._plan()
        if self._finish_stragglers():
            return True
        claim = store.claim_step(self.conn, self.owner, self.s.lease_s, self.kind)
        if claim is None:
            return False
        self._heartbeat(busy=True, force=True)
        try:
            self._work(claim)
        finally:
            self._heartbeat(busy=False, force=True)
        return True

    def _work(self, claim) -> None:
        job = store.get_job(self.conn, claim["job_id"])
        try:
            flow = load_flow(self.s.flows_dir, job["flow"])
        except FlowError as exc:
            store.fail_job(self.conn, job["id"], str(exc))
            return
        step = flow.step(claim["step"])
        if step is None or {s for s in step.inputs if s != SERIES} != set(claim["needs"]):
            store.fail_job(
                self.conn, job["id"],
                "the flow was changed while this job was running; retry it with a full rerun",
            )
            return

        run = handle = None
        if claim["prev_state"] == "running":
            # The executor that had this step died. Its container may still be running or have finished.
            run, handle = self._adopt(job, step)
        if handle is None:
            started = self._start(job, flow, step)
            if started is None:
                return
            run, handle = started
        if self._supervise(job, flow, step, run, handle):
            if store.step_succeeded(self.conn, job["id"], step.name, run["id"], self.owner):
                self._finish(job, flow)

    def _heartbeat(self, busy: bool, force: bool = False) -> None:
        """Tell the status board this executor exists, so a missing kind of executor can be noticed."""
        if not force and self._clock() - self._last_heartbeat < 10:
            return
        self._last_heartbeat = self._clock()
        store.set_status(self.conn, f"executor:{self.owner}", kind=self.kind, busy=busy)

    def _reap(self) -> None:
        """Remove containers on this node whose run is over according to the database:
        leftovers from an upgrade, a failover, or a crash at the wrong moment."""
        if self._clock() - self._last_reap < 600:
            return
        self._last_reap = self._clock()
        try:
            for run_id in self.runtime.list_run_ids():
                if store.run_state(self.conn, run_id) != "running":
                    handle = self.runtime.find(run_id)
                    if handle:
                        log.warning("removing leftover container of run %s", run_id)
                        self.runtime.kill(handle)
                        self.runtime.remove(handle)
        except RuntimeError_ as exc:
            log.warning("could not look for leftover containers: %s", exc)

    def _plan(self) -> None:
        """Lay out the steps of ready jobs that have none yet, so executors can claim them."""
        for job in store.unplanned_jobs(self.conn):
            try:
                flow = load_flow(self.s.flows_dir, job["flow"])
            except FlowError as exc:
                store.fail_job(self.conn, job["id"], str(exc))
                continue
            store.plan_job(
                self.conn,
                job["id"],
                [(s.name, s.gpu, [src for src in s.inputs if src != SERIES]) for s in flow.steps],
            )

    def _finish_stragglers(self) -> bool:
        """Finish jobs whose steps all succeeded but whose executor died before queuing the deliveries."""
        worked = False
        for job in store.jobs_to_finish(self.conn, self.s.lease_s):
            try:
                self._finish(job, load_flow(self.s.flows_dir, job["flow"]))
            except FlowError as exc:
                store.fail_job(self.conn, job["id"], str(exc))
            worked = True
        return worked

    # --- start -------------------------------------------------------------------------

    def _adopt(self, job, step: Step):
        run = store.latest_run(self.conn, job["id"], step.name)
        if run is None or run["state"] != "running":
            return None, None
        handle = self.runtime.find(str(run["id"]))
        if handle is None:
            store.finish_run(self.conn, run["id"], "lost", error="container not found after executor restart")
            return None, None
        log.info("adopted container of run %s (job %s, step %s)", run["id"], job["id"], step.name)
        return run, handle

    def _start(self, job, flow: Flow, step: Step):
        job_id = job["id"]
        attempts = flow.attempts_for(step)
        if store.count_runs(self.conn, job_id, step.name) >= attempts:
            what = "run attempts" if len(flow.steps) == 1 else f"attempts of step {step.name}"
            store.fail_job(self.conn, job_id, f"gave up after {attempts} {what}")
            return None

        # The outputs this step reads must still be on disk; a step whose output is gone runs again.
        done = store.succeeded_steps(self.conn, job_id)
        upstream = {}
        for source, path in step.inputs.items():
            if source == SERIES:
                continue
            out = self.s.runs_dir / str(done.get(source)) / "out"
            if source not in done or not out.is_dir():
                store.reset_step(self.conn, job_id, source)
                store.release_step(self.conn, job_id, step.name, self.owner)
                log.warning("job %s: output of step %s is gone; it will run again", job_id, source)
                return None
            upstream[path] = out

        run = store.create_run(self.conn, job_id, step.image, step.name)
        run_dir = self.s.runs_dir / str(run["id"])
        try:
            input_dir, output_dir = self._prepare(job_id, run_dir, SERIES in step.inputs)
        except CacheMiss as miss:
            shutil.rmtree(run_dir, ignore_errors=True)
            if store.requeue_for_retrieval(self.conn, job_id, step.name, self.owner, miss.series):
                store.delete_run(self.conn, run["id"])  # never started: must not count as an attempt
            else:
                self._fail_run(job, flow, step, run, "the input series are missing from the cache", None)
            return None
        spec = ContainerSpec(
            run_id=str(run["id"]),
            job_id=str(job_id),
            step=step.name,
            image=step.image,
            command=step.command,
            input_dir=input_dir,
            input_path=step.inputs.get(SERIES, "/input"),
            extra_inputs=upstream,
            output_dir=output_dir,
            env={
                **flow.env,
                **step.env,
                "RADFETCH_JOB_ID": str(job_id),
                "RADFETCH_RUN_ID": str(run["id"]),
                "RADFETCH_ACCESSION_NUMBER": job["accession_number"],
                "RADFETCH_STUDY_INSTANCE_UID": job["study_instance_uid"] or "",
                "RADFETCH_FLOW": flow.name,
                "RADFETCH_STEP": step.name,
            },
            gpu=step.gpu,
            mem_limit=step.mem_limit,
            shm_size=step.shm_size,
            network=self.s.container_network,
        )
        try:
            handle = self.runtime.start(spec)
        except RuntimeError_ as exc:
            self._fail_run(job, flow, step, run, str(exc), None)
            return None
        store.set_run_image_digest(self.conn, run["id"], self.runtime.image_digest(handle))
        return run, handle

    def _prepare(self, job_id, run_dir: Path, with_series: bool = True) -> tuple[Path | None, Path]:
        """Build /input from hardlinks into the cache: no copy, and eviction cannot pull files from under a run."""
        input_dir, output_dir = run_dir / "in", run_dir / "out"
        shutil.rmtree(run_dir, ignore_errors=True)
        output_dir.mkdir(parents=True)
        os.chmod(output_dir, 0o777)  # flow images often run as a non-root user
        if not with_series:
            return None, output_dir
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

    def _supervise(self, job, flow: Flow, step: Step, run, handle: str) -> bool:
        """Wait for the step's container. True when it exited with code 0 while the step was still ours."""
        job_id, run_id = job["id"], run["id"]
        run_dir = self.s.runs_dir / str(run_id)
        timeout = flow.timeout_for(step)
        deadline = self._clock() + timeout
        # The container is removed only once its outcome is recorded. If we fail unexpectedly
        # (say the database drops mid-run) it keeps running and is adopted after the lease expires.
        remove = False
        try:
            while True:
                try:
                    code = self.runtime.exit_code(handle)
                except RuntimeError_ as exc:
                    self._fail_run(job, flow, step, run, str(exc), None)
                    remove = True
                    return False
                if code is not None:
                    break
                lease = store.renew_step_lease(self.conn, job_id, step.name, self.owner, self.s.lease_s)
                if lease == "taken":
                    # We stalled and another executor adopted this container: it is theirs now.
                    log.warning("job %s: step %s was taken over; leaving run %s to the new owner", job_id, step.name, run_id)
                    return False
                if lease == "stopped":
                    # The job was cancelled, or failed in another step.
                    self.runtime.kill(handle)
                    store.finish_run(self.conn, run_id, "cancelled")
                    remove = True
                    log.info("job %s is no longer running; stopped run %s", job_id, run_id)
                    return False
                if self._clock() >= deadline:
                    self.runtime.kill(handle)
                    self._save_logs(handle, run_dir)
                    self._fail_run(job, flow, step, run, f"timed out after {timeout}s", None)
                    remove = True
                    return False
                self._heartbeat(busy=True)
                self._sleep(self.s.executor_poll_s)

            self._save_logs(handle, run_dir)
            remove = True
            if code != 0:
                self._fail_run(job, flow, step, run, f"container exited with code {code}", code)
                return False
            log.info("job %s: step %s (run %s) succeeded", job_id, step.name, run_id)
            return True
        finally:
            if remove:
                self.runtime.remove(handle)

    def _finish(self, job, flow: Flow) -> None:
        """Every step has succeeded: queue the deliveries and store the result."""
        done = store.succeeded_steps(self.conn, job["id"])
        names = [n for n in flow.delivered_steps() if n in done]
        deliveries = []
        for name in names:
            self._write_manifest(done[name], self.s.runs_dir / str(done[name]))
            # With several delivered steps, a directory destination keeps each in its own subfolder.
            extra = {"subfolder": name} if len(names) > 1 else {}
            deliveries += [(done[name], {**d.model_dump(), **extra}) for d in flow.destinations]
        result_run = done.get(flow.result_step())
        result, problem = (None, None)
        if result_run is not None:
            result, problem = self._read_result(flow, self.s.runs_dir / str(result_run))
        if store.finish_job(self.conn, job["id"], deliveries, result_run, result, problem):
            log.info("job %s: all steps succeeded", job["id"])

    def _fail_run(self, job, flow: Flow, step: Step, run, error: str, exit_code: int | None) -> None:
        retry = store.count_runs(self.conn, job["id"], step.name) < flow.attempts_for(step)
        if len(flow.steps) > 1:
            error = f"step {step.name}: {error}"
        store.step_failed(
            self.conn, job["id"], step.name, run["id"], self.owner, error, exit_code, retry,
            self.s.backoff_s(run["attempt"]),
        )
        log.warning("job %s: run %s failed (%s)%s", job["id"], run["id"], error, ", will retry" if retry else "")

    def _read_result(self, flow: Flow, run_dir: Path):
        """The flow's structured result, if it wrote one: (document, None) or (None, why it was not stored)."""
        out = (run_dir / "out").resolve()
        path = (out / flow.results_file).resolve()
        if out not in path.parents or not path.is_file():  # absent, or a path pointing outside /output
            return None, None
        if path.stat().st_size > self.s.results_max_bytes:
            return None, f"{flow.results_file} is larger than {self.s.results_max_bytes} bytes"
        try:
            text = path.read_text(encoding="utf-8")
            if "\\u0000" in text or "\x00" in text:
                return None, f"{flow.results_file} contains NUL characters, which cannot be stored"
            data = json.loads(text)
        except (OSError, ValueError) as exc:
            return None, f"{flow.results_file} is not valid JSON: {exc}"
        if not isinstance(data, (dict, list)):
            return None, f"{flow.results_file} must hold a JSON object or array"
        return data, None

    def _write_manifest(self, run_id, run_dir: Path) -> None:
        """Put radfetch.json next to the results so they carry their own provenance."""
        try:
            manifest = store.run_manifest(self.conn, run_id)
            (run_dir / "out" / "radfetch.json").write_text(json.dumps(manifest, indent=2, default=str))
        except OSError:
            log.exception("could not write the run manifest")

    def _save_logs(self, handle: str, run_dir: Path) -> None:
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "container.log").write_bytes(self.runtime.logs(handle))
        except OSError:
            log.exception("could not save container log")
