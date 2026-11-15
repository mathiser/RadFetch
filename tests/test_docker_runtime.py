"""DockerRuntime against a real Docker daemon. Skipped when none is reachable or busybox cannot be pulled."""

from __future__ import annotations

import time
import uuid

import pytest

from radfetch.executor.runtime import ContainerSpec

IMAGE = "busybox:1.36"


@pytest.fixture
def docker_runtime(settings):
    try:
        import docker

        from radfetch.executor.docker_runtime import DockerRuntime

        runtime = DockerRuntime(settings)
        runtime.client.ping()
        try:
            runtime.client.images.get(IMAGE)
        except docker.errors.ImageNotFound:
            runtime.client.images.pull(IMAGE)
    except Exception as exc:
        pytest.skip(f"docker not available: {exc}")
    return runtime


def spec_for(settings, command, gpu=False) -> ContainerSpec:
    run_id = str(uuid.uuid4())
    run_dir = settings.runs_dir / run_id
    (run_dir / "in" / "series").mkdir(parents=True)
    (run_dir / "out").mkdir()
    (run_dir / "out").chmod(0o777)
    (run_dir / "in" / "series" / "a.dcm").write_text("x")
    return ContainerSpec(
        run_id=run_id,
        job_id="job",
        image=IMAGE,
        command=command,
        input_dir=run_dir / "in",
        output_dir=run_dir / "out",
        env={"GREETING": "hello"},
        gpu=gpu,
    )


def wait(runtime, handle, timeout=30):
    deadline = time.monotonic() + timeout
    while (code := runtime.exit_code(handle)) is None:
        assert time.monotonic() < deadline
        time.sleep(0.2)
    return code


def test_run_to_completion(docker_runtime, settings):
    script = (
        "echo $GREETING; ls /input/series > /output/listing.txt;"
        " touch /input/series/new 2>/dev/null && exit 9;"  # input must be read-only
        " wget -q -T 2 http://1.1.1.1 2>/dev/null && exit 8;"  # network must be off
        " exit 0"
    )
    spec = spec_for(settings, ["sh", "-c", script])
    handle = docker_runtime.start(spec)
    try:
        assert docker_runtime.find(spec.run_id) == handle
        assert "sha256:" in docker_runtime.image_digest(handle)
        assert wait(docker_runtime, handle) == 0
        assert docker_runtime.logs(handle).strip() == b"hello"
        assert (spec.output_dir / "listing.txt").read_text().strip() == "a.dcm"
    finally:
        docker_runtime.remove(handle)
    assert docker_runtime.find(spec.run_id) is None


def test_exit_code_and_kill(docker_runtime, settings):
    failing = docker_runtime.start(spec_for(settings, ["sh", "-c", "exit 3"]))
    assert wait(docker_runtime, failing) == 3
    docker_runtime.remove(failing)

    spec = spec_for(settings, ["sleep", "300"])
    handle = docker_runtime.start(spec)
    assert docker_runtime.exit_code(handle) is None
    docker_runtime.kill(handle)
    assert wait(docker_runtime, handle) == 137
    docker_runtime.remove(handle)
    docker_runtime.remove(handle)  # idempotent


def has_gpu(runtime) -> bool:
    return "nvidia" in runtime.client.info().get("Runtimes", {})


def test_fake_gpu_runs_gpu_flow_without_gpu(docker_runtime, settings):
    settings.debug_fake_gpu = True
    spec = spec_for(settings, ["sh", "-c", "echo fake=$RADFETCH_FAKE_GPU"], gpu=True)
    handle = docker_runtime.start(spec)
    try:
        assert wait(docker_runtime, handle) == 0
        assert docker_runtime.logs(handle).strip() == b"fake=1"
        assert "RADFETCH_FAKE_GPU" not in spec.env  # the caller's spec is not modified
    finally:
        docker_runtime.remove(handle)


def test_gpu_flow_without_gpu_fails_clearly(docker_runtime, settings):
    if has_gpu(docker_runtime):
        pytest.skip("this host has a GPU runtime")
    from radfetch.executor.runtime import RuntimeError_

    spec = spec_for(settings, ["true"], gpu=True)
    with pytest.raises(RuntimeError_, match="cannot start container"):
        docker_runtime.start(spec)
    assert docker_runtime.find(spec.run_id) is None  # nothing left behind


def test_real_gpu_is_visible_in_container(docker_runtime, settings):
    """Runs only on a host with the NVIDIA runtime: the one test that touches a real GPU."""
    if not has_gpu(docker_runtime):
        pytest.skip("no NVIDIA container runtime on this host")
    spec = spec_for(settings, ["sh", "-c", "ls /dev/nvidia0 && echo fake=$RADFETCH_FAKE_GPU"], gpu=True)
    handle = docker_runtime.start(spec)
    try:
        assert wait(docker_runtime, handle) == 0
        assert docker_runtime.logs(handle).strip().endswith(b"fake=")
    finally:
        docker_runtime.remove(handle)


def test_gpu_device_selection(settings):
    from radfetch.executor.docker_runtime import DockerRuntime

    runtime = DockerRuntime.__new__(DockerRuntime)  # no daemon needed to build the request
    runtime.s = settings
    assert runtime.gpu_request() == {"Driver": "", "Count": -1, "DeviceIDs": [], "Capabilities": [["gpu"]], "Options": {}}
    settings.gpu_device_ids = "1, 3"
    request = runtime.gpu_request()
    assert request["DeviceIDs"] == ["1", "3"] and request["Count"] == 0
