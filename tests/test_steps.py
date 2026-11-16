"""Flows with several steps: wiring of outputs to inputs, order, resume after failure, delivery."""

from __future__ import annotations

import json

import pytest

from radfetch import store
from radfetch.config import INTERACTIVE_PRIORITY
from radfetch.executor.runtime import ContainerSpec
from radfetch.executor.worker import Executor
from radfetch.flows import FlowError, load_flow
from radfetch.gateway.worker import Gateway
from radfetch.sender import Sender

from conftest import FakeRuntime

PIPELINE = """
name: pipe
version: "4"
select:
  - modality: CT
    min_instances: 5
steps:
  - name: report            # listed first on purpose: order comes from the inputs, not the file
    image: reg/report:1
    inputs: {{measure: /measurements}}
  - name: segment
    image: reg/segment:1
    gpu: true
  - name: measure
    image: reg/measure:1
    max_attempts: 3
    inputs: {{series: /dicom, segment: /masks}}
deliver: [report, segment]
results_from: measure
destinations:
  - type: directory
    path: {dest}
"""


def behave(spec: ContainerSpec) -> int:
    """Each fake step writes a file that proves which inputs it was given."""
    seen = {path: sorted(p.name for p in host.iterdir()) for path, host in spec.extra_inputs.items()}
    series = sorted(p.name for p in spec.input_dir.iterdir()) if spec.input_dir else None
    (spec.output_dir / f"{spec.step}.json").write_text(json.dumps({"series": series, "inputs": seen}))
    if spec.step == "measure":
        (spec.output_dir / "results.json").write_text('{"volume_ml": 41.5}')
    return 0


@pytest.fixture
def pipe(conn, settings, pacs, tmp_path):
    (settings.flows_dir / "pipe.yaml").write_text(PIPELINE.format(dest=tmp_path / "dest"))
    job = store.submit_job(conn, "ACC0001", "pipe", INTERACTIVE_PRIORITY)
    gateway = Gateway(conn, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)
    while gateway.step():
        pass
    return job


def executor(conn, settings, runtime, kind="any"):
    settings = settings.model_copy(update={"executor_kind": kind})
    return Executor(conn, runtime, settings, sleep=lambda s: None)


def run_all(ex):
    """An executor runs one step per call; keep going until it finds nothing to do."""
    n = 0
    while ex.step():
        n += 1
        assert n < 50
    return n


def test_steps_run_in_dependency_order_with_outputs_wired(conn, settings, pipe, tmp_path):
    runtime = FakeRuntime(behave)
    run_all(executor(conn, settings, runtime))
    specs = {s.step: s for s in runtime.started}
    assert [s.step for s in runtime.started] == ["segment", "measure", "report"]

    assert specs["segment"].gpu and specs["segment"].input_path == "/input" and not specs["segment"].extra_inputs
    assert specs["measure"].input_path == "/dicom" and not specs["measure"].gpu
    assert specs["measure"].extra_inputs == {"/masks": specs["segment"].output_dir}
    assert specs["report"].input_dir is None  # this step does not read the DICOM series
    assert specs["report"].extra_inputs == {"/measurements": specs["measure"].output_dir}
    assert specs["measure"].env["RADFETCH_STEP"] == "measure"
    seen = json.loads((specs["measure"].output_dir / "measure.json").read_text())
    assert seen == {"series": ["1.2.1.1"], "inputs": {"/masks": ["segment.json"]}}

    # the outputs of the two delivered steps are sent; the result comes from the measure step
    assert store.get_job(conn, pipe)["state"] == "sending"
    while Sender(conn, settings).step():
        pass
    assert store.get_job(conn, pipe)["state"] == "done"
    root = tmp_path / "dest" / str(pipe)
    delivered = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert delivered == ["report/radfetch.json", "report/report.json", "segment/radfetch.json", "segment/segment.json"]
    assert store.get_result(conn, pipe)["data"] == {"volume_ml": 41.5}
    manifest = json.loads((root / "report" / "radfetch.json").read_text())
    assert [(s["step"], s["image"]) for s in manifest["steps"]] == [
        ("measure", "reg/measure:1"), ("report", "reg/report:1"), ("segment", "reg/segment:1")]
    assert [r["step"] for r in store.job_runs(conn, pipe)] == ["segment", "measure", "report"]


def test_failed_step_is_retried_without_repeating_earlier_steps(conn, settings, pipe):
    fail = {"measure": 2}  # measure fails twice, then works (it allows 3 attempts)

    def flaky(spec):
        if fail.get(spec.step, 0) > 0:
            fail[spec.step] -= 1
            return 1
        return behave(spec)

    runtime = FakeRuntime(flaky)
    ex = executor(conn, settings, runtime)
    ex.step()
    ex.step()
    assert store.get_job(conn, pipe)["state"] == "running"
    assert "step measure: container exited with code 1" in store.get_job(conn, pipe)["error"]
    run_all(ex)
    assert store.get_job(conn, pipe)["state"] == "sending" and store.get_job(conn, pipe)["error"] is None
    assert [s.step for s in runtime.started] == ["segment", "measure", "measure", "measure", "report"]


def test_job_fails_when_a_step_runs_out_of_attempts_and_retry_resumes_there(conn, settings, pipe):
    broken = FakeRuntime(lambda spec: 7 if spec.step == "report" else behave(spec))
    run_all(executor(conn, settings, broken))
    row = store.get_job(conn, pipe)
    assert row["state"] == "failed" and "report" in row["error"]
    assert [s.step for s in broken.started] == ["segment", "measure", "report", "report"]

    # an ordinary retry continues at the failed step
    assert store.retry_job(conn, pipe) == "queued"
    gateway = Gateway(conn, __import__("conftest").MemoryPacs(), settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)
    while gateway.step():
        pass
    fixed = FakeRuntime(behave)
    run_all(executor(conn, settings, fixed))
    assert [s.step for s in fixed.started] == ["report"]
    assert store.get_job(conn, pipe)["state"] == "sending"

    # a full retry runs everything again
    while Sender(conn, settings).step():
        pass
    conn.execute("update jobs set state = 'failed' where id = %s", (pipe,))
    store.retry_job(conn, pipe, full=True)
    while gateway.step():
        pass
    again = FakeRuntime(behave)
    run_all(executor(conn, settings, again))
    assert [s.step for s in again.started] == ["segment", "measure", "report"]


def test_executor_crash_mid_pipeline_adopts_the_step_and_continues(conn, settings, pipe):
    def hang_on_measure(spec):
        return None if spec.step == "measure" else behave(spec)

    runtime = FakeRuntime(hang_on_measure)
    executor(conn, settings, runtime).step()  # segment
    with pytest.raises(ZeroDivisionError):
        Executor(conn, runtime, settings, sleep=lambda s: 1 / 0).step()  # dies while measure runs
    conn.execute("update job_steps set lease_expires_at = now() - interval '1 second' where job_id = %s", (pipe,))
    spec = next(s for s in runtime.started if s.step == "measure")
    behave(spec)  # the container finishes while nobody is watching
    runtime.containers[spec.run_id]["exit"] = 0
    runtime.behavior = behave
    run_all(executor(conn, settings, runtime))
    assert store.get_job(conn, pipe)["state"] == "sending"
    assert [s.step for s in runtime.started] == ["segment", "measure", "report"]  # measure was not started twice


def test_cancel_during_a_step_stops_the_pipeline(conn, settings, pipe):
    runtime = FakeRuntime(lambda spec: None if spec.step == "measure" else behave(spec))
    ex = Executor(conn, runtime, settings, sleep=lambda s: store.cancel_job(conn, pipe))
    run_all(ex)
    assert store.get_job(conn, pipe)["state"] == "cancelled"
    assert [s.step for s in runtime.started] == ["segment", "measure"]
    assert runtime.containers == {}  # the running container was stopped and removed


def test_single_image_flows_are_one_step_named_main(settings):
    flow = load_flow(settings.flows_dir, "seg")
    (step,) = flow.steps
    assert (step.name, step.image, step.gpu, step.inputs) == ("main", "registry.example/seg@sha256:abc", True, {"series": "/input"})
    assert flow.delivered_steps() == ["main"] and flow.result_step() == "main"


@pytest.mark.parametrize("body,message", [
    ("image: x\nsteps:\n  - {name: a, image: y}\n", "either image"),
    ("steps: []\n", "needs an image"),
    ("steps:\n  - {name: a, image: x}\n  - {name: a, image: y}\n", "unique"),
    ("steps:\n  - {name: series, image: x}\n", "cannot be named"),
    ("steps:\n  - {name: a, image: x, inputs: {nope: /in}}\n", "unknown input"),
    ("steps:\n  - {name: a, image: x, inputs: {a: /in}}\n", "its own output"),
    ("steps:\n  - {name: a, image: x, inputs: {b: /in}}\n  - {name: b, image: y, inputs: {a: /in}}\n", "circle"),
    ("steps:\n  - {name: a, image: x, inputs: {series: /output}}\n", "not a usable mount path"),
    ("steps:\n  - {name: a, image: x, inputs: {series: relative}}\n", "not a usable mount path"),
    ("steps:\n  - {name: a, image: x}\n  - {name: b, image: y, inputs: {series: /in, a: /in}}\n", "share a mount path"),
    ("steps:\n  - {name: a, image: x}\ndeliver: [zzz]\n", "unknown step"),
])
def test_invalid_step_definitions_are_rejected(settings, body, message):
    (settings.flows_dir / "bad.yaml").write_text("name: bad\nselect:\n  - modality: CT\n" + body)
    with pytest.raises(FlowError, match=message):
        load_flow(settings.flows_dir, "bad")
