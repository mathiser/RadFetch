"""Runtime backed by the local Docker Engine."""

from __future__ import annotations

import docker
from docker.errors import APIError, DockerException, NotFound
from docker.types import DeviceRequest

from radfetch.config import Settings
from radfetch.executor.runtime import ContainerSpec, RuntimeError_

LABEL_RUN = "radfetch.run_id"
LABEL_JOB = "radfetch.job_id"


class DockerRuntime:
    def __init__(self, settings: Settings):
        self.s = settings
        self.client = docker.from_env()

    def start(self, spec: ContainerSpec) -> str:
        volumes = {
            str(self.s.host_path(spec.input_dir)): {"bind": "/input", "mode": "ro"},
            str(self.s.host_path(spec.output_dir)): {"bind": "/output", "mode": "rw"},
        }
        kwargs = {}
        environment = dict(spec.env)
        if spec.gpu and self.s.debug_fake_gpu:
            environment["RADFETCH_FAKE_GPU"] = "1"
        elif spec.gpu:
            kwargs["device_requests"] = [self.gpu_request()]
        if spec.mem_limit:
            kwargs["mem_limit"] = spec.mem_limit
        if spec.shm_size:
            kwargs["shm_size"] = spec.shm_size
        try:
            container = self.client.containers.run(
                spec.image,
                spec.command,
                detach=True,
                name=f"radfetch-{spec.run_id}",
                labels={LABEL_RUN: spec.run_id, LABEL_JOB: spec.job_id},
                environment=environment,
                volumes=volumes,
                network_mode=spec.network,
                **kwargs,
            )
        except DockerException as exc:
            # A failed start (missing image, no GPU runtime, bad mount) can leave a created
            # container behind; remove it so failures do not pile up on the host.
            try:
                leftover = self.find(spec.run_id)
                if leftover:
                    self.remove(leftover)
            except DockerException:
                pass
            raise RuntimeError_(f"cannot start container: {exc}") from exc
        return container.id

    def gpu_request(self) -> DeviceRequest:
        ids = [i.strip() for i in self.s.gpu_device_ids.split(",") if i.strip()]
        if ids:
            return DeviceRequest(device_ids=ids, capabilities=[["gpu"]])
        return DeviceRequest(count=-1, capabilities=[["gpu"]])

    def find(self, run_id: str) -> str | None:
        found = self.client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"})
        return found[0].id if found else None

    def exit_code(self, handle: str) -> int | None:
        try:
            container = self.client.containers.get(handle)
        except NotFound as exc:
            raise RuntimeError_("container disappeared") from exc
        if container.status in ("created", "running", "restarting", "paused"):
            return None
        return int(container.attrs["State"]["ExitCode"])

    def logs(self, handle: str) -> bytes:
        try:
            return self.client.containers.get(handle).logs(stdout=True, stderr=True)
        except DockerException:
            return b""

    def kill(self, handle: str) -> None:
        try:
            self.client.containers.get(handle).kill()
        except (NotFound, APIError):
            pass  # already gone or already stopped

    def remove(self, handle: str) -> None:
        try:
            self.client.containers.get(handle).remove(force=True)
        except NotFound:
            pass
