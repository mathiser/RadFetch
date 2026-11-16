"""The executor: runs one flow container at a time (one worker process = one GPU)."""

from __future__ import annotations

import json
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
from radfetch.flows import SERIES, Flow, FlowError, Step, load_flow

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

        run = handle = step = None
        if job["prev_state"] == "running":
            # The previous executor died. Its container may still be running or have finished.
            run, handle, step = self._adopt(job, flow)
        # A flow's steps run one after another here; each keeps its output for the steps that read it.
        while True:
            if handle is None:
                done = self._done_steps(job["id"])
                step = next((s for s in flow.ordered_steps() if s.name not in done), None)
                if step is None:
                    self._finish(job, flow, done)
                    return True
                started = self._start(job, flow, step, done)
                if started is None:
                    return True
                run, handle = started
            if not self._supervise(job, flow, step, run, handle):
                return True
            handle = None
            if not store.renew_job_lease(self.conn, job["id"], self.owner, self.s.lease_s):
                return True  # cancelled between steps

    # --- start -------------------------------------------------------------------------

    def _adopt(self, job, flow: Flow):
        run = store.latest_run(self.conn, job["id"])
        if run is None or run["state"] != "running":
            return None, None, None
        handle = self.runtime.find(str(run["id"]))
        step = flow.step(run["step"])
        if handle is None or step is None:
            if handle is not None:  # the flow was edited and no longer has this step
                self.runtime.kill(handle)
                self.runtime.remove(handle)
            store.finish_run(self.conn, run["id"], "lost", error="container not found after executor restart")
            return None, None, None
        log.info("adopted container of run %s for job %s", run["id"], job["id"])
        return run, handle, step

    def _done_steps(self, job_id) -> dict:
        """Steps that succeeded and whose output is still on disk: step name -> run id."""
        done = store.succeeded_steps(self.conn, job_id)
        return {name: run_id for name, run_id in done.items() if (self.s.runs_dir / str(run_id) / "out").is_dir()}

    def _start(self, job, flow: Flow, step: Step, done: dict):
        job_id = job["id"]
        attempts = flow.attempts_for(step)
        if store.count_runs(self.conn, job_id, step.name) >= attempts:
            what = "run attempts" if len(flow.steps) == 1 else f"attempts of step {step.name}"
            store.fail_job(self.conn, job_id, f"gave up after {attempts} {what}", self.owner)
            return None
        run = store.create_run(self.conn, job_id, step.image, step.name)
        run_dir = self.s.runs_dir / str(run["id"])
        try:
            input_dir, output_dir = self._prepare(job_id, run_dir, SERIES in step.inputs)
        except CacheMiss as miss:
            store.delete_run(self.conn, run["id"])  # never started: must not count as an attempt
            store.requeue_for_retrieval(self.conn, job_id, self.owner, miss.series)
            shutil.rmtree(run_dir, ignore_errors=True)
            return None
        spec = ContainerSpec(
            run_id=str(run["id"]),
            job_id=str(job_id),
            step=step.name,
            image=step.image,
            command=step.command,
            input_dir=input_dir,
            input_path=step.inputs.get(SERIES, "/input"),
            extra_inputs={
                path: self.s.runs_dir / str(done[source]) / "out"
                for source, path in step.inputs.items()
                if source != SERIES
            },
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
        """Wait for the step's container. True when it succeeded and the job is still ours."""
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
                if not store.renew_job_lease(self.conn, job_id, self.owner, self.s.lease_s):
                    current = store.get_job(self.conn, job_id)
                    if current is not None and current["state"] == "running":
                        # We stalled and another executor adopted this container: it is theirs now.
                        log.warning("job %s was taken over; leaving run %s to the new owner", job_id, run_id)
                        return False
                    self.runtime.kill(handle)
                    store.finish_run(self.conn, run_id, "cancelled")
                    remove = True
                    log.info("job %s was cancelled; stopped run %s", job_id, run_id)
                    return False
                if self._clock() >= deadline:
                    self.runtime.kill(handle)
                    self._save_logs(handle, run_dir)
                    self._fail_run(job, flow, step, run, f"timed out after {timeout}s", None)
                    remove = True
                    return False
                self._sleep(self.s.executor_poll_s)

            self._save_logs(handle, run_dir)
            remove = True
            if code != 0:
                self._fail_run(job, flow, step, run, f"container exited with code {code}", code)
                return False
            store.step_succeeded(self.conn, job_id, run_id, step.name)
            log.info("job %s: step %s (run %s) succeeded", job_id, step.name, run_id)
            return True
        finally:
            if remove:
                self.runtime.remove(handle)

    def _finish(self, job, flow: Flow, done: dict) -> None:
        """Every step has succeeded: queue the deliveries and store the result."""
        deliveries = []
        names = flow.delivered_steps()
        for name in names:
            self._write_manifest(done[name], self.s.runs_dir / str(done[name]))
            # With several delivered steps, a directory destination keeps each in its own subfolder.
            extra = {"subfolder": name} if len(names) > 1 else {}
            deliveries += [(done[name], {**d.model_dump(), **extra}) for d in flow.destinations]
        result_run = done[flow.result_step()]
        result, problem = self._read_result(flow, self.s.runs_dir / str(result_run))
        store.run_succeeded(self.conn, job["id"], self.owner, deliveries, result_run, result, problem)
        log.info("job %s: all steps succeeded", job["id"])

    def _fail_run(self, job, flow: Flow, step: Step, run, error: str, exit_code: int | None) -> None:
        retry = store.count_runs(self.conn, job["id"], step.name) < flow.attempts_for(step)
        if len(flow.steps) > 1:
            error = f"step {step.name}: {error}"
        store.run_failed(
            self.conn, job["id"], run["id"], self.owner, error, exit_code, retry, self.s.backoff_s(run["attempt"])
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
