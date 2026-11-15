"""End-to-end state machine tests against Postgres, with an in-memory PACS and a fake container runtime."""

from __future__ import annotations

import shutil
from datetime import datetime

import pytest

from radfetch import store
from radfetch.config import BATCH_PRIORITY, INTERACTIVE_PRIORITY
from radfetch.executor.worker import Executor
from radfetch.gateway.worker import Gateway
from radfetch.janitor import Janitor
from radfetch.sender import Sender

from conftest import FakeRuntime


def drain(worker, limit=500):
    n = 0
    while worker.step():
        n += 1
        assert n < limit, "worker did not settle"
    return n


def state(conn, job_id):
    return store.get_job(conn, job_id)["state"]


def make_gateway(conn, pacs, settings, **kw):
    kw.setdefault("disk_fraction", lambda: 0.1)
    kw.setdefault("sleep", lambda s: None)
    return Gateway(conn, pacs, settings, **kw)


def make_executor(conn, runtime, settings, **kw):
    kw.setdefault("sleep", lambda s: None)
    return Executor(conn, runtime, settings, **kw)


def one_off(conn, accession="ACC0001", flow="seg"):
    return store.submit_job(conn, accession, flow, INTERACTIVE_PRIORITY)


def test_one_off_end_to_end(conn, settings, pacs, runtime, tmp_path):
    job = one_off(conn)
    drain(make_gateway(conn, pacs, settings))
    assert state(conn, job) == "ready"
    assert pacs.retrieved == ["1.2.1.1"]  # only the series the flow selects

    drain(make_executor(conn, runtime, settings))
    assert state(conn, job) == "sending"
    spec = runtime.started[0]
    assert spec.gpu and spec.image == "registry.example/seg@sha256:abc"
    assert spec.env["RADFETCH_ACCESSION_NUMBER"] == "ACC0001"
    assert len(list((spec.input_dir / "1.2.1.1").glob("*.dcm"))) == 12
    assert (spec.input_dir.parent / "container.log").read_bytes() == b"log output\n"
    assert runtime.containers == {}  # removed after exit

    drain(Sender(conn, settings))
    row = store.get_job(conn, job)
    assert row["state"] == "done" and row["flow_version"] == "3"
    assert (tmp_path / "dest" / str(job) / "result.txt").read_text() == "12 instances"
    kinds = [e["kind"] for e in store.job_events(conn, job)]
    assert kinds == ["submitted", "resolved", "ready", "run_started", "run_succeeded", "done"]


def test_series_shared_between_jobs_is_fetched_once(conn, settings, pacs, runtime):
    a = one_off(conn, flow="seg")
    b = one_off(conn, flow="allct")  # thorax + scout
    gateway = make_gateway(conn, pacs, settings)
    drain(gateway)
    assert sorted(pacs.retrieved) == ["1.2.1.1", "1.2.1.2"]
    assert state(conn, a) == state(conn, b) == "ready"

    c = one_off(conn, flow="seg")  # already cached: ready without touching retrieval
    drain(gateway)
    assert state(conn, c) == "ready" and len(pacs.retrieved) == 2


def test_batch_preloading_is_bounded(conn, settings, pacs, runtime):
    settings.max_staged_jobs = 2
    settings.max_resolved_ahead = 3
    batch = store.create_batch(conn, "b", "seg", [f"ACC{i:04d}" for i in range(1, 11)], BATCH_PRIORITY)
    gateway = make_gateway(conn, pacs, settings)
    drain(gateway)
    counts = store.get_batch(conn, batch)["jobs"]
    # 2 staged for the executor, 3 more resolved and waiting, the rest untouched.
    assert counts == {"ready": 2, "retrieving": 3, "queued": 5}
    assert len(pacs.retrieved) == 2 and pacs.finds == 2 * 5

    # A one-off job is not held back by the batch backlog and runs first.
    urgent = one_off(conn, "ACC0020")
    drain(gateway)
    assert state(conn, urgent) == "ready"
    executor = make_executor(conn, runtime, settings)
    executor.step()
    assert runtime.started[0].env["RADFETCH_ACCESSION_NUMBER"] == "ACC0020"

    # As the executor consumes jobs, the gateway tops the buffer up again, until all are done.
    for _ in range(30):
        executor.step()
        drain(gateway)
        drain(Sender(conn, settings))
        assert store.count_jobs(conn, "ready") <= 2
    assert store.get_batch(conn, batch)["jobs"] == {"done": 10}
    assert len(pacs.retrieved) == 11


def test_batch_window_holds_batch_but_not_one_off(conn, settings, pacs):
    settings.batch_windows = "20:00-06:00"
    noon = lambda: datetime(2026, 11, 11, 12, 0)  # noqa: E731
    batch_job = store.submit_job(conn, "ACC0001", "seg", BATCH_PRIORITY)
    urgent = one_off(conn, "ACC0002")
    drain(make_gateway(conn, pacs, settings, now=noon))
    assert state(conn, batch_job) == "queued" and state(conn, urgent) == "ready"

    drain(make_gateway(conn, pacs, settings, now=lambda: datetime(2026, 11, 11, 23, 0)))
    assert state(conn, batch_job) == "ready"


def test_disk_watermarks(conn, settings, pacs):
    batch_job = store.submit_job(conn, "ACC0001", "seg", BATCH_PRIORITY)
    urgent = one_off(conn, "ACC0002")
    drain(make_gateway(conn, pacs, settings, disk_fraction=lambda: 0.97))
    assert state(conn, batch_job) == state(conn, urgent) == "retrieving"  # hard limit stops everything
    drain(make_gateway(conn, pacs, settings, disk_fraction=lambda: 0.85))
    assert state(conn, batch_job) == "retrieving" and state(conn, urgent) == "ready"


def test_rate_limit_applies_to_batch(conn, settings, pacs):
    settings.rate_instances_per_min = 6  # burst 6; one 12-instance series overdraws it
    t = [0.0]
    store.create_batch(conn, "b", "seg", ["ACC0001", "ACC0002"], BATCH_PRIORITY)
    gateway = make_gateway(conn, pacs, settings, clock=lambda: t[0])
    drain(gateway)
    assert len(pacs.retrieved) == 1
    t[0] = 59
    drain(gateway)
    assert len(pacs.retrieved) == 1
    t[0] = 61  # 6 tokens of debt paid back at 0.1/s
    drain(gateway)
    assert len(pacs.retrieved) == 2


def test_pacs_outage_opens_breaker_then_recovers(conn, settings, pacs):
    settings.breaker_failure_threshold = 2
    settings.breaker_base_cooldown_s = 100
    t = [0.0]
    jobs = [one_off(conn, f"ACC{i:04d}") for i in (1, 2, 3)]
    gateway = make_gateway(conn, pacs, settings, clock=lambda: t[0])
    pacs.fail = True
    assert drain(gateway) == 2  # two failures, then the breaker refuses further work
    assert gateway.breaker.is_open
    assert all(state(conn, j) == "queued" for j in jobs)

    pacs.fail = False
    assert drain(gateway) == 0  # still cooling down, PACS is left alone
    t[0] = 101
    drain(gateway)
    assert all(state(conn, j) == "ready" for j in jobs)


def test_resolve_gives_up_after_max_attempts(conn, settings, pacs):
    settings.resolve_max_attempts = 2
    settings.breaker_failure_threshold = 99
    pacs.fail = True
    job = one_off(conn)
    drain(make_gateway(conn, pacs, settings))
    row = store.get_job(conn, job)
    assert row["state"] == "failed" and "PACS query failed" in row["error"]


def test_incomplete_retrieval_is_retried_then_fails_job(conn, settings, pacs):
    settings.retrieve_max_attempts = 2
    pacs.drop_instances = 1
    job = one_off(conn)
    gateway = make_gateway(conn, pacs, settings)
    drain(gateway)
    row = store.get_job(conn, job)
    assert row["state"] == "failed" and "received 11, expected 12" in row["error"]
    assert len(pacs.retrieved) == 2
    assert not (settings.cache_dir / "1.2.1.1").exists()  # partial data never enters the cache

    # A later job for the same series gets a fresh set of attempts.
    pacs.drop_instances = 0
    again = one_off(conn)
    drain(gateway)
    assert state(conn, again) == "ready"


@pytest.mark.parametrize(
    "accession,flow,message",
    [
        ("NOPE", "seg", "no study found"),
        ("DOUBLE", "seg", "2 studies found"),
        ("ACC0001", "mr", "none of the study's 3 series match"),
    ],
)
def test_unresolvable_jobs_fail_with_reason(conn, settings, pacs, accession, flow, message):
    from radfetch.gateway.pacs import StudyInfo

    pacs.extra_studies["DOUBLE"] = [StudyInfo("9.1", "DOUBLE"), StudyInfo("9.2", "DOUBLE")]
    (settings.flows_dir / "mr.yaml").write_text("name: mr\nimage: x\nselect:\n  - modality: MR\n")
    job = one_off(conn, accession, flow)
    drain(make_gateway(conn, pacs, settings))
    row = store.get_job(conn, job)
    assert row["state"] == "failed" and message in row["error"]


def test_failed_run_is_retried_then_fails(conn, settings, pacs):
    job = one_off(conn)
    drain(make_gateway(conn, pacs, settings))
    runtime = FakeRuntime(lambda spec: 3)
    executor = make_executor(conn, runtime, settings)
    executor.step()
    assert state(conn, job) == "ready"  # attempt 1 of 2 failed
    executor.step()
    row = store.get_job(conn, job)
    assert row["state"] == "failed" and "exited with code 3" in row["error"]
    assert len(runtime.started) == 2 and not executor.step()


def test_timeout_kills_container(conn, settings, pacs):
    job = one_off(conn)
    drain(make_gateway(conn, pacs, settings))
    t = [0.0]
    runtime = FakeRuntime(lambda spec: None)
    executor = make_executor(conn, runtime, settings, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + 4000))
    executor.step()
    row = store.get_job(conn, job)
    assert row["state"] == "ready" and "timed out" in row["error"]
    assert runtime.containers == {}


def test_cancel_stops_running_container(conn, settings, pacs):
    job = one_off(conn)
    drain(make_gateway(conn, pacs, settings))
    runtime = FakeRuntime(lambda spec: None)
    killed = []
    runtime.kill = lambda handle: killed.append(handle) or runtime.containers[handle].update(exit=137)
    executor = make_executor(conn, runtime, settings, sleep=lambda s: store.cancel_job(conn, job))
    executor.step()
    assert state(conn, job) == "cancelled" and len(killed) == 1
    assert store.latest_run(conn, job)["state"] == "cancelled"


def test_executor_crash_adopts_running_container(conn, settings, pacs):
    job = one_off(conn)
    drain(make_gateway(conn, pacs, settings))
    runtime = FakeRuntime(lambda spec: None)

    class Crash(Exception):
        pass

    def crash(_):
        raise Crash

    with pytest.raises(Crash):
        make_executor(conn, runtime, settings, sleep=crash).step()
    assert state(conn, job) == "running"

    survivor = make_executor(conn, runtime, settings)
    assert not survivor.step()  # lease still valid: hands off
    conn.execute("update jobs set lease_expires_at = now() - interval '1 second' where id = %s", (job,))
    handle = next(iter(runtime.containers))
    (runtime.started[0].output_dir / "result.txt").write_text("finished while unattended")
    runtime.containers[handle]["exit"] = 0
    survivor.step()
    assert state(conn, job) == "sending"
    assert len(runtime.started) == 1  # adopted, not re-run


def test_executor_crash_with_lost_container_reruns(conn, settings, pacs):
    job = one_off(conn)
    drain(make_gateway(conn, pacs, settings))
    runtime = FakeRuntime(lambda spec: None)
    with pytest.raises(ZeroDivisionError):
        make_executor(conn, runtime, settings, sleep=lambda s: 1 / 0).step()
    runtime.containers.clear()  # e.g. host reboot
    conn.execute("update jobs set lease_expires_at = now() - interval '1 second' where id = %s", (job,))
    runtime.behavior = FakeRuntime.write_result
    make_executor(conn, runtime, settings).step()
    assert state(conn, job) == "sending"
    runs = conn.execute("select state from runs where job_id = %s order by attempt", (job,)).fetchall()
    assert [r["state"] for r in runs] == ["lost", "succeeded"]


def test_stale_gateway_cannot_resolve_after_cancel(conn, settings, pacs):
    job = one_off(conn)
    claimed = store.claim_job_to_resolve(conn, "gw-old", 60, INTERACTIVE_PRIORITY, True)
    assert claimed["id"] == job
    assert store.claim_job_to_resolve(conn, "gw-new", 60, INTERACTIVE_PRIORITY, True) is None  # leased
    store.cancel_job(conn, job)
    assert not store.resolve_job(conn, job, "gw-old", "1.2.1", "1", pacs.find_series("1.2.1"))
    assert state(conn, job) == "cancelled"


def test_pause_and_resume_batch(conn, settings, pacs, runtime):
    batch = store.create_batch(conn, "b", "seg", ["ACC0001", "ACC0002"], BATCH_PRIORITY)
    store.set_batch_paused(conn, batch, True)
    gateway, executor = make_gateway(conn, pacs, settings), make_executor(conn, runtime, settings)
    assert drain(gateway) == 0 and pacs.finds == 0
    store.set_batch_paused(conn, batch, False)
    drain(gateway)
    store.set_batch_paused(conn, batch, True)
    assert not executor.step()
    store.set_batch_paused(conn, batch, False)
    assert executor.step()


def test_cancel_batch(conn, settings, pacs):
    batch = store.create_batch(conn, "b", "seg", ["ACC0001", "ACC0002", "ACC0002", " "], BATCH_PRIORITY)
    assert store.get_batch(conn, batch)["jobs"] == {"queued": 2}  # duplicates and blanks dropped
    assert store.cancel_batch(conn, batch) == 2
    assert drain(make_gateway(conn, pacs, settings)) == 0


def test_eviction_respects_active_jobs_and_refetches(conn, settings, pacs, runtime):
    settings.cache_ttl_hours = 0
    job = one_off(conn)
    gateway, janitor = make_gateway(conn, pacs, settings), Janitor(conn, settings, disk_fraction=lambda: 0.1)
    drain(gateway)
    janitor.sweep()
    assert (settings.cache_dir / "1.2.1.1").is_dir()  # a ready job still needs it

    drain(make_executor(conn, runtime, settings))
    drain(Sender(conn, settings))
    janitor.sweep()
    assert not (settings.cache_dir / "1.2.1.1").exists()
    assert conn.execute("select state from retrievals").fetchone()["state"] == "evicted"

    again = one_off(conn)
    drain(gateway)
    assert state(conn, again) == "ready" and pacs.retrieved == ["1.2.1.1", "1.2.1.1"]


def test_cache_miss_at_run_time_triggers_refetch(conn, settings, pacs, runtime):
    job = one_off(conn)
    gateway, executor = make_gateway(conn, pacs, settings), make_executor(conn, runtime, settings)
    drain(gateway)
    shutil.rmtree(settings.cache_dir / "1.2.1.1")  # files lost behind the database's back
    executor.step()
    assert state(conn, job) == "retrieving" and runtime.started == []
    drain(gateway)
    executor.step()
    assert state(conn, job) == "sending"
    assert store.count_runs(conn, job) == 1  # the miss did not use up an attempt


def test_run_directories_are_purged_after_ttl(conn, settings, pacs, runtime):
    job = one_off(conn)
    drain(make_gateway(conn, pacs, settings))
    drain(make_executor(conn, runtime, settings))
    drain(Sender(conn, settings))
    run_dir = settings.runs_dir / str(store.latest_run(conn, job)["id"])
    janitor = Janitor(conn, settings, disk_fraction=lambda: 0.1)
    janitor.sweep()
    assert run_dir.is_dir()
    settings.run_ttl_hours = 0
    janitor.sweep()
    assert not run_dir.exists()


def test_failed_delivery_retries_without_rerunning(conn, settings, pacs, runtime, tmp_path):
    job = one_off(conn)
    drain(make_gateway(conn, pacs, settings))
    drain(make_executor(conn, runtime, settings))
    dest = tmp_path / "dest"
    shutil.rmtree(dest)
    dest.write_text("not a directory")  # destination broken
    sender = Sender(conn, settings)
    sender.step()
    assert state(conn, job) == "sending"
    dest.unlink()
    dest.mkdir()
    drain(sender)
    assert state(conn, job) == "done" and len(runtime.started) == 1


def test_retry_failed_run_starts_over_with_fresh_attempts(conn, settings, pacs):
    job = one_off(conn)
    gateway = make_gateway(conn, pacs, settings)
    drain(gateway)
    runtime = FakeRuntime(lambda spec: 3)
    executor = make_executor(conn, runtime, settings)
    drain(executor)
    assert state(conn, job) == "failed" and len(runtime.started) == 2
    assert store.retry_job(conn, one_off(conn, "ACC0002")) is None  # a queued job is not retryable

    assert store.retry_job(conn, job) == "queued"
    drain(gateway)
    assert state(conn, job) == "ready" and pacs.retrieved.count("1.2.1.1") == 1  # series still cached
    runtime.behavior = FakeRuntime.write_result
    drain(executor)
    assert state(conn, job) == "sending" and len([s for s in runtime.started if s.job_id == str(job)]) == 3
    assert [e["kind"] for e in store.job_events(conn, job)].count("retried") == 1


def test_retry_after_delivery_failure_only_resends(conn, settings, pacs, runtime, tmp_path):
    settings.deliver_max_attempts = 1
    job = one_off(conn)
    drain(make_gateway(conn, pacs, settings))
    drain(make_executor(conn, runtime, settings))
    dest = tmp_path / "dest"
    shutil.rmtree(dest)
    dest.write_text("broken")
    sender = Sender(conn, settings)
    drain(sender)
    assert state(conn, job) == "failed"

    dest.unlink()
    dest.mkdir()
    assert store.retry_job(conn, job) == "sending"
    drain(sender)
    assert state(conn, job) == "done" and len(runtime.started) == 1  # inference not repeated

    # full=True forces a rerun even though the run itself had succeeded
    other = one_off(conn, "ACC0003")
    drain(make_gateway(conn, pacs, settings))
    drain(make_executor(conn, runtime, settings))
    store.cancel_job(conn, other)
    assert store.retry_job(conn, other, full=True) == "queued"


def test_retry_failed_in_batch(conn, settings, pacs):
    batch = store.create_batch(conn, "b", "seg", ["ACC0001", "NOPE"], BATCH_PRIORITY)
    gateway = make_gateway(conn, pacs, settings)
    drain(gateway)
    assert store.get_batch(conn, batch)["jobs"] == {"ready": 1, "failed": 1}
    pacs.n = 0
    pacs.extra_studies["NOPE"] = [__import__("radfetch.gateway.pacs", fromlist=["StudyInfo"]).StudyInfo("1.2.7", "NOPE")]
    assert store.retry_failed_in_batch(conn, batch) == 1
    drain(gateway)
    assert store.get_batch(conn, batch)["jobs"] == {"ready": 2}


def test_multiple_studies_all(conn, settings, pacs, runtime):
    from radfetch.gateway.pacs import StudyInfo

    pacs.extra_studies["DOUBLE"] = [StudyInfo("1.2.9", "DOUBLE"), StudyInfo("1.2.8", "DOUBLE")]
    (settings.flows_dir / "multi.yaml").write_text(
        "name: multi\nimage: x\nmultiple_studies: all\nselect:\n  - modality: CT\n    min_instances: 5\n"
    )
    job = one_off(conn, "DOUBLE", "multi")
    drain(make_gateway(conn, pacs, settings))
    row = store.get_job(conn, job)
    assert row["state"] == "ready" and row["study_instance_uid"] == "1.2.8"
    assert store.job_series_uids(conn, job) == ["1.2.8.1", "1.2.9.1"]
    drain(make_executor(conn, runtime, settings))
    assert sorted(p.name for p in runtime.started[0].input_dir.iterdir()) == ["1.2.8.1", "1.2.9.1"]
