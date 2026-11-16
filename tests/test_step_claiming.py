"""Step-level claiming: CPU and GPU executors, parallel steps, and what happens when things go wrong."""

from __future__ import annotations

import threading

import pytest

from radfetch import db, store
from radfetch.config import BATCH_PRIORITY, INTERACTIVE_PRIORITY
from radfetch.executor.worker import Executor
from radfetch.gateway.worker import Gateway

from conftest import FakeRuntime
from test_steps import PIPELINE, behave, run_all

# two independent CPU branches joined by a third step
FAN = """
name: fan
select:
  - modality: CT
    min_instances: 5
steps:
  - {name: left, image: reg/left:1}
  - {name: right, image: reg/right:1}
  - name: join
    image: reg/join:1
    inputs: {left: /left, right: /right}
"""


def ready_job(conn, settings, pacs, flow, accession="ACC0001", priority=INTERACTIVE_PRIORITY, batch=None):
    job = store.submit_job(conn, accession, flow, priority, batch)
    gateway = Gateway(conn, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)
    while gateway.step():
        pass
    assert store.get_job(conn, job)["state"] == "ready"
    return job


def make(conn, settings, runtime, kind="any", **kw):
    kw.setdefault("sleep", lambda s: None)
    return Executor(conn, runtime, settings.model_copy(update={"executor_kind": kind}), **kw)


@pytest.fixture
def flows(settings, tmp_path):
    (settings.flows_dir / "pipe.yaml").write_text(PIPELINE.format(dest=tmp_path / "dest"))
    (settings.flows_dir / "fan.yaml").write_text(FAN)


def steps_of(conn, job):
    return {s["step"]: s["state"] for s in store.job_steps(conn, job)}


def test_gpu_and_cpu_executors_each_take_their_own_steps(conn, settings, pacs, flows):
    job = ready_job(conn, settings, pacs, "pipe")
    gpu_rt, cpu_rt = FakeRuntime(behave), FakeRuntime(behave)
    gpu, cpu = make(conn, settings, gpu_rt, "gpu"), make(conn, settings, cpu_rt, "cpu")

    assert not cpu.step()  # the only runnable step needs a GPU; the job is still just "ready"
    assert store.get_job(conn, job)["state"] == "ready"
    assert gpu.step() and not gpu.step()  # segment done; measure and report are not for a GPU executor
    assert store.get_job(conn, job)["state"] == "running"
    run_all(cpu)
    assert [s.step for s in gpu_rt.started] == ["segment"]
    assert [s.step for s in cpu_rt.started] == ["measure", "report"]
    assert store.get_job(conn, job)["state"] == "sending"
    # the cpu executor read what the gpu executor produced
    assert cpu_rt.started[0].extra_inputs == {"/masks": gpu_rt.started[0].output_dir}


def test_gpu_is_free_for_the_next_job_while_cpu_steps_wait(conn, settings, pacs, flows):
    first = ready_job(conn, settings, pacs, "pipe", "ACC0001")
    second = ready_job(conn, settings, pacs, "pipe", "ACC0002")
    gpu_rt = FakeRuntime(behave)
    gpu = make(conn, settings, gpu_rt, "gpu")
    run_all(gpu)
    # both jobs' GPU steps ran back to back, without waiting for anyone's CPU steps
    assert [(s.step, s.env["RADFETCH_ACCESSION_NUMBER"]) for s in gpu_rt.started] == [
        ("segment", "ACC0001"), ("segment", "ACC0002")]
    assert steps_of(conn, first)["measure"] == steps_of(conn, second)["measure"] == "pending"


def test_independent_steps_run_at_the_same_time(conn, settings, pacs, flows):
    job = ready_job(conn, settings, pacs, "fan")
    other = db.connect(settings.database_url)
    rt_a, rt_b = FakeRuntime(lambda spec: None), FakeRuntime(behave)  # A's container keeps running
    b = make(other, settings, rt_b, "cpu")
    overlap = []

    def while_a_runs(_):
        # A is in the middle of its step; B takes the other branch and completes it meanwhile.
        if not overlap:
            b.step()
            overlap.append(steps_of(conn, job))
            behave(rt_a.started[0])
            rt_a.containers[rt_a.started[0].run_id]["exit"] = 0

    a = make(conn, settings, rt_a, "cpu", sleep=while_a_runs)
    a.step()
    assert overlap[0] == {"left": "running", "right": "succeeded", "join": "pending"}
    assert steps_of(conn, job) == {"left": "succeeded", "right": "succeeded", "join": "pending"}
    assert not any(s.step == "join" for s in rt_a.started + rt_b.started)  # join waited for both
    rt_a.behavior = behave
    run_all(a)
    assert store.get_job(conn, job)["state"] == "done"
    other.close()


def test_a_step_is_never_claimed_twice(conn, settings, pacs, flows):
    for i in range(1, 9):
        ready_job(conn, settings, pacs, "fan", f"ACC{i:04d}")
    started, lock = [], threading.Lock()

    def worker():
        c = db.connect(settings.database_url)

        def record(spec):
            with lock:
                started.append((spec.job_id, spec.step))
            return behave(spec)

        run_all(make(c, settings, FakeRuntime(record), "cpu"))
        c.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(started) == len(set(started)) == 8 * 3
    assert store.state_counts(conn) == {"done": 8}


def test_failure_in_one_step_stops_the_sibling_step(conn, settings, pacs, flows):
    (settings.flows_dir / "fan.yaml").write_text(FAN + "max_attempts: 1\n")
    job = ready_job(conn, settings, pacs, "fan")
    other = db.connect(settings.database_url)
    rt_a = FakeRuntime(lambda spec: None)
    rt_b = FakeRuntime(lambda spec: 5)  # the other branch fails for good
    b = make(other, settings, rt_b, "cpu")
    a = make(conn, settings, rt_a, "cpu", sleep=lambda _: b.step())
    a.step()
    row = store.get_job(conn, job)
    assert row["state"] == "failed" and "container exited with code 5" in row["error"]
    assert rt_a.containers == {}  # A's still-running container was stopped and removed
    assert [r["state"] for r in store.job_runs(conn, job)] == ["cancelled", "failed"]
    other.close()


def test_job_is_finished_even_if_the_executor_dies_after_the_last_step(conn, settings, pacs, flows):
    job = ready_job(conn, settings, pacs, "seg")
    dying = make(conn, settings, FakeRuntime())
    dying._finish = lambda *a: 1 / 0  # dies between recording the last step and finishing the job
    with pytest.raises(ZeroDivisionError):
        dying.step()
    assert store.get_job(conn, job)["state"] == "running" and steps_of(conn, job) == {"main": "succeeded"}

    settings.lease_s = 3600
    survivor = make(conn, settings, FakeRuntime())
    assert not survivor.step()  # too soon: the first executor may just be busy finishing
    conn.execute("update runs set finished_at = now() - interval '2 hours'")
    assert survivor.step()
    assert store.get_job(conn, job)["state"] == "sending"
    assert not survivor.step()  # finishing twice does nothing


def test_priority_applies_to_steps(conn, settings, pacs, flows):
    batch = store.create_batch(conn, "b", "pipe", ["ACC0001"], BATCH_PRIORITY)
    gateway = Gateway(conn, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)
    while gateway.step():
        pass
    runtime = FakeRuntime(behave)
    ex = make(conn, settings, runtime)
    ex.step()  # the batch job's first step
    ready_job(conn, settings, pacs, "pipe", "ACC0009")  # an urgent job arrives
    run_all(ex)
    order = [(s.env["RADFETCH_ACCESSION_NUMBER"], s.step) for s in runtime.started]
    assert order[0] == ("ACC0001", "segment")
    assert [o[0] for o in order[1:4]] == ["ACC0009"] * 3  # all of the urgent job before the batch job's later steps
    assert store.get_batch(conn, batch)["jobs"] == {"sending": 1}


def test_paused_batch_starts_no_further_steps(conn, settings, pacs, flows):
    batch = store.create_batch(conn, "b", "pipe", ["ACC0001"], BATCH_PRIORITY)
    gateway = Gateway(conn, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)
    while gateway.step():
        pass
    runtime = FakeRuntime(behave)
    ex = make(conn, settings, runtime)
    ex.step()
    store.set_batch_paused(conn, batch, True)
    assert not ex.step() and len(runtime.started) == 1
    store.set_batch_paused(conn, batch, False)
    run_all(ex)
    assert len(runtime.started) == 3


def test_lost_upstream_output_makes_that_step_run_again(conn, settings, pacs, flows):
    import shutil

    job = ready_job(conn, settings, pacs, "pipe")
    runtime = FakeRuntime(behave)
    ex = make(conn, settings, runtime)
    ex.step()  # segment
    shutil.rmtree(runtime.started[0].output_dir)  # its output disappears
    run_all(ex)
    assert [s.step for s in runtime.started] == ["segment", "segment", "measure", "report"]
    assert store.get_job(conn, job)["state"] == "sending"


def test_executors_report_themselves_and_leftover_containers_are_removed(conn, settings, pacs, flows):
    runtime = FakeRuntime()
    runtime.containers["11111111-1111-1111-1111-111111111111"] = {"exit": None, "killed": False}  # unknown to the database
    ex = make(conn, settings, runtime, "gpu")
    ex.step()
    assert runtime.containers == {}
    (status,) = [v for k, v in store.get_status(conn).items() if k.startswith("executor:")]
    assert status["kind"] == "gpu" and status["busy"] is False


def test_flow_changed_under_a_running_job_fails_it_clearly(conn, settings, pacs, flows):
    job = ready_job(conn, settings, pacs, "pipe")
    ex = make(conn, settings, FakeRuntime(behave))
    ex.step()
    (settings.flows_dir / "pipe.yaml").write_text(
        "name: pipe\nselect:\n  - modality: CT\nsteps:\n  - {name: segment, image: a}\n  - {name: measure, image: b}\n")
    ex.step()
    row = store.get_job(conn, job)
    assert row["state"] == "failed" and "flow was changed" in row["error"]


def test_batch_preloading_counts_jobs_queueing_for_the_gpu(conn, settings, pacs, flows):
    """A CPU step first, then a GPU step: jobs pile up before the GPU, and fetching must hold back."""
    (settings.flows_dir / "prep.yaml").write_text(
        "name: prep\nselect:\n  - modality: CT\n    min_instances: 5\nsteps:\n"
        "  - {name: prepare, image: a}\n  - {name: infer, image: b, gpu: true, inputs: {prepare: /prep}}\n")
    settings.max_staged_jobs = 2
    store.create_batch(conn, "b", "prep", [f"ACC{i:04d}" for i in range(1, 11)], BATCH_PRIORITY)
    gateway = Gateway(conn, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)
    cpu = make(conn, settings, FakeRuntime(behave), "cpu")

    def settle():
        for _ in range(40):
            if not (run_all(cpu) + sum(1 for _ in iter(gateway.step, False))):
                return

    settle()
    # Only CPU executors are running, so every fetched job stops before its GPU step.
    # The gateway stops at two such jobs instead of fetching all ten.
    assert len(pacs.retrieved) == 2 and store.staged_backlog(conn) == 2
    assert store.waiting_steps(conn) == {"gpu": 2, "cpu": 0}

    gpu = make(conn, settings, FakeRuntime(behave), "gpu")
    for _ in range(40):
        run_all(gpu)
        settle()
    assert store.state_counts(conn) == {"done": 10} and len(pacs.retrieved) == 10
