"""Flow definitions: which series to fetch, which container to run, where results go."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from radfetch.gateway.pacs import SeriesInfo

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class FlowError(Exception):
    pass


class SeriesRule(BaseModel):
    """A series matches a rule when every given condition holds."""

    modality: str | None = None
    series_description: str | None = None  # regular expression, searched
    min_instances: int = 1
    max_instances: int | None = None

    @field_validator("series_description")
    @classmethod
    def _valid_regex(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v

    def matches(self, s: SeriesInfo) -> bool:
        if self.modality and (s.modality or "").upper() != self.modality.upper():
            return False
        if self.series_description and not re.search(self.series_description, s.description or ""):
            return False
        # An unknown instance count (PACS did not return it) is not held against the series.
        if s.instances is not None:
            if s.instances < self.min_instances:
                return False
            if self.max_instances is not None and s.instances > self.max_instances:
                return False
        return True


class DicomDestination(BaseModel):
    type: Literal["dicom"]
    host: str
    port: int
    ae_title: str
    tls: bool = False  # uses the sender's RADFETCH_TLS_* settings


class DirectoryDestination(BaseModel):
    type: Literal["directory"]
    path: str


class StowDestination(BaseModel):
    """STOW-RS: POST every DICOM file to `<url>/studies`."""

    type: Literal["stow"]
    url: str
    # Name of an environment variable of the sender service holding a bearer token.
    # The token itself never goes in the flow file or the database.
    token_env: str | None = None


class Trigger(BaseModel):
    """Lets studies pushed to the storage SCP start this flow. Every given condition must hold."""

    calling_ae_title: str | None = None  # regular expression, searched

    @field_validator("calling_ae_title")
    @classmethod
    def _valid_regex(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v

    def matches(self, calling_ae_title: str) -> bool:
        return not self.calling_ae_title or bool(re.search(self.calling_ae_title, calling_ae_title or ""))


Destination = Annotated[Union[DicomDestination, DirectoryDestination, StowDestination], Field(discriminator="type")]


SERIES = "series"  # the input name for the DICOM series fetched or received for the job


class Step(BaseModel):
    """One container in a flow. Its output folder can be an input of later steps."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    image: str
    command: list[str] | None = None  # overrides the image's default command
    gpu: bool = False
    timeout_s: int | None = None  # default: the flow's timeout_s
    max_attempts: int | None = None  # default: the flow's max_attempts
    env: dict[str, str] = {}
    mem_limit: str | None = None
    shm_size: str | None = None
    # What the container gets to read, and where: "series" for the DICOM series, or the
    # name of another step for that step's output folder. Everything is mounted read-only.
    inputs: dict[str, str] = {SERIES: "/input"}


class Flow(BaseModel):
    name: str
    version: str = "1"
    select: list[SeriesRule] = Field(min_length=1)
    # all: every matching series; largest: only the matching series with most instances
    pick: Literal["all", "largest"] = "all"
    # What to do when an accession number maps to several studies.
    # fail: fail the job. all: run once on the matching series of all of them.
    multiple_studies: Literal["fail", "all"] = "fail"

    # Either one container, given directly here...
    image: str | None = None
    command: list[str] | None = None
    gpu: bool = False
    mem_limit: str | None = None
    shm_size: str | None = None
    # ...or several, as steps. A step reads the series and/or the output of other steps.
    steps: list[Step] = []
    # Steps whose output is delivered and whose results file is stored. Default: the last step.
    deliver: list[str] | None = None
    results_from: str | None = None

    # Defaults for every step.
    timeout_s: int = 3600
    env: dict[str, str] = {}
    max_attempts: int = 2

    destinations: list[Destination] = []
    # A JSON file the container may write under /output with structured results (measurements,
    # scores, labels...). It is stored with the job and can be queried and exported.
    results_file: str = "results.json"
    # Without a trigger the flow only runs when a job is submitted for it.
    trigger: Trigger | None = None

    @model_validator(mode="after")
    def _check_steps(self) -> "Flow":
        if self.steps and self.image:
            raise ValueError("give either image (one container) or steps (several), not both")
        if not self.steps:
            if not self.image:
                raise ValueError("a flow needs an image, or steps")
            self.steps = [
                Step(name="main", image=self.image, command=self.command, gpu=self.gpu,
                     mem_limit=self.mem_limit, shm_size=self.shm_size)
            ]
        names = [s.name for s in self.steps]
        if len(set(names)) != len(names):
            raise ValueError("step names must be unique")
        if SERIES in names:
            raise ValueError(f"a step cannot be named {SERIES!r}")
        for step in self.steps:
            paths = list(step.inputs.values())
            for source, path in step.inputs.items():
                if source != SERIES and source not in names:
                    raise ValueError(f"step {step.name}: unknown input {source!r}")
                if source == step.name:
                    raise ValueError(f"step {step.name} cannot read its own output")
                if not path.startswith("/") or path.rstrip("/") == "" or path.rstrip("/") == "/output" \
                        or path.startswith("/output/"):
                    raise ValueError(f"step {step.name}: {path!r} is not a usable mount path")
            if len(set(paths)) != len(paths):
                raise ValueError(f"step {step.name}: two inputs share a mount path")
        self.ordered_steps()  # raises on a cycle
        for name in [*(self.deliver or []), *([self.results_from] if self.results_from else [])]:
            if name not in names:
                raise ValueError(f"unknown step {name!r} in deliver or results_from")
        return self

    def ordered_steps(self) -> list[Step]:
        """Steps in an order where each comes after the steps it reads from (file order where possible)."""
        done: list[Step] = []
        remaining = list(self.steps)
        while remaining:
            ready = [s for s in remaining
                     if all(src == SERIES or src in {d.name for d in done} for src in s.inputs)]
            if not ready:
                raise ValueError("steps depend on each other in a circle: " + ", ".join(s.name for s in remaining))
            done.append(ready[0])
            remaining.remove(ready[0])
        return done

    def step(self, name: str) -> Step | None:
        return next((s for s in self.steps if s.name == name), None)

    def timeout_for(self, step: Step) -> int:
        return step.timeout_s or self.timeout_s

    def attempts_for(self, step: Step) -> int:
        return step.max_attempts or self.max_attempts

    def delivered_steps(self) -> list[str]:
        return self.deliver if self.deliver is not None else [self.ordered_steps()[-1].name]

    def result_step(self) -> str:
        return self.results_from or self.ordered_steps()[-1].name

    def select_series(self, series: list[SeriesInfo]) -> list[SeriesInfo]:
        matched = [s for s in series if any(rule.matches(s) for rule in self.select)]
        if self.pick == "largest" and matched:
            matched = [max(matched, key=lambda s: s.instances or 0)]
        return matched


def load_flow(flows_dir: Path, name: str) -> Flow:
    """Load `<flows_dir>/<name>.yaml`. Read on every call so edits apply without restarts."""
    if not _NAME_RE.match(name):
        raise FlowError(f"invalid flow name: {name!r}")
    path = flows_dir / f"{name}.yaml"
    if not path.is_file():
        raise FlowError(f"unknown flow: {name}")
    try:
        flow = Flow.model_validate(yaml.safe_load(path.read_text()))
    except Exception as exc:
        raise FlowError(f"invalid flow file {path.name}: {exc}") from exc
    if flow.name != name:
        raise FlowError(f"{path.name}: name must equal the file stem ({name!r}), got {flow.name!r}")
    return flow


def list_flows(flows_dir: Path) -> list[str]:
    return sorted(p.stem for p in flows_dir.glob("*.yaml"))
