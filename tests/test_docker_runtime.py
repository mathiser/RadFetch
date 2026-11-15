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


def spec_for(settings, command) -> ContainerSpec:
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
