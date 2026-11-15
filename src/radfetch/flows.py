"""Flow definitions: which series to fetch, which container to run, where results go."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, Field, field_validator

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


class Flow(BaseModel):
    name: str
    version: str = "1"
    select: list[SeriesRule] = Field(min_length=1)
    # all: every matching series; largest: only the matching series with most instances
    pick: Literal["all", "largest"] = "all"
    # What to do when an accession number maps to several studies.
    # fail: fail the job. all: run once on the matching series of all of them.
    multiple_studies: Literal["fail", "all"] = "fail"
    image: str
    command: list[str] | None = None  # overrides the image's default command
    gpu: bool = False
    timeout_s: int = 3600
    env: dict[str, str] = {}
    mem_limit: str | None = None
    shm_size: str | None = None
    max_attempts: int = 2
    destinations: list[Destination] = []
    # Without a trigger the flow only runs when a job is submitted for it.
    trigger: Trigger | None = None

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
