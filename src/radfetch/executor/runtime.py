"""The container runtime interface. Docker today; Kubernetes Jobs or Slurm are further implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class RuntimeError_(Exception):
    """The runtime could not start or inspect a container."""


@dataclass
class ContainerSpec:
    run_id: str
    job_id: str
    image: str
    input_dir: Path | None  # the job's series, mounted read-only at input_path; None if the step does not read them
    output_dir: Path  # mounted read-write at /output
    input_path: str = "/input"
    # Output folders of earlier steps, mounted read-only: container path -> host folder
    extra_inputs: dict[str, Path] = field(default_factory=dict)
    step: str = "main"
    command: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    gpu: bool = False
    mem_limit: str | None = None
    shm_size: str | None = None
    network: str = "none"


class Runtime(Protocol):
    def start(self, spec: ContainerSpec) -> str:
        """Start the container and return a handle."""
        ...

    def find(self, run_id: str) -> str | None:
        """Handle of the container started for this run, if it still exists (used after a restart)."""
        ...

    def image_digest(self, handle: str) -> str | None:
        """Immutable identifier of the image the container actually runs, for traceability."""
        ...

    def list_run_ids(self) -> list[str]:
        """Run ids of every flow container that exists on this node, running or not."""
        ...

    def exit_code(self, handle: str) -> int | None:
        """None while running, otherwise the exit code."""
        ...

    def logs(self, handle: str) -> bytes: ...

    def kill(self, handle: str) -> None: ...

    def remove(self, handle: str) -> None: ...
