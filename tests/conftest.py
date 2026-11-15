"""Shared fixtures. Database tests need RADFETCH_TEST_DATABASE_URL (they wipe that database)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from radfetch import db
from radfetch.config import Settings
from radfetch.executor.runtime import ContainerSpec
from radfetch.gateway.pacs import PacsError, RetrieveResult, SeriesInfo, StudyInfo

FLOW_SEG = """
name: seg
version: "3"
select:
  - modality: CT
    series_description: "(?i)thorax"
    min_instances: 5
image: registry.example/seg@sha256:abc
gpu: true
max_attempts: 2
destinations:
  - type: directory
    path: {dest}
"""

FLOW_ALL = """
name: allct
select:
  - modality: CT
image: registry.example/allct:1
"""


@pytest.fixture
def conn():
    url = os.environ.get("RADFETCH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("RADFETCH_TEST_DATABASE_URL not set")
    c = db.connect(url)
    c.execute("drop schema public cascade; create schema public")
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    flows = tmp_path / "flows"
    flows.mkdir()
    (tmp_path / "dest").mkdir()
    (flows / "seg.yaml").write_text(FLOW_SEG.format(dest=tmp_path / "dest"))
    (flows / "allct.yaml").write_text(FLOW_ALL)
    s = Settings(
        _env_file=None,
        database_url=os.environ.get("RADFETCH_TEST_DATABASE_URL", ""),
        data_dir=tmp_path / "data",
        flows_dir=flows,
        min_interval_s=0,
        rate_instances_per_min=0,
        retry_base_delay_s=0,
        idle_sleep_s=0,
        executor_poll_s=0,
    )
    s.cache_dir.mkdir(parents=True)
    return s


@dataclass
class MemoryPacs:
    """In-memory PacsClient. ACC0001..ACC00NN, each with a 12-slice thorax CT, a scout and a dose report."""

    n: int = 20
    fail: bool = False
    drop_instances: int = 0  # deliver this many instances fewer than announced
    finds: int = 0
    retrieved: list[str] = field(default_factory=list)
    extra_studies: dict[str, list[StudyInfo]] = field(default_factory=dict)

    def _check(self):
        if self.fail:
            raise PacsError("PACS down")

    def find_studies(self, accession_number):
        self._check()
        self.finds += 1
        if accession_number in self.extra_studies:
            return self.extra_studies[accession_number]
        if accession_number.startswith("ACC") and 1 <= int(accession_number[3:]) <= self.n:
            return [StudyInfo(f"1.2.{int(accession_number[3:])}", accession_number)]
        return []

    def find_series(self, study_instance_uid):
        self._check()
        self.finds += 1
        return [
            SeriesInfo(f"{study_instance_uid}.1", study_instance_uid, "CT", "Thorax 2.0", 12),
            SeriesInfo(f"{study_instance_uid}.2", study_instance_uid, "CT", "Scout", 2),
            SeriesInfo(f"{study_instance_uid}.3", study_instance_uid, "OT", "Dose report", 1),
        ]

    def retrieve_series(self, study_instance_uid, series_instance_uid, dest: Path):
        self._check()
        self.retrieved.append(series_instance_uid)
        total = {"1": 12, "2": 2, "3": 1}[series_instance_uid.rsplit(".", 1)[1]]
        dest.mkdir(parents=True, exist_ok=True)
        count = total - self.drop_instances
        for i in range(count):
            (dest / f"{series_instance_uid}.{i}.dcm").write_bytes(b"x" * 10)
        return RetrieveResult(received=count, failed=0, bytes=count * 10)


class FakeRuntime:
    """Runtime double. `behavior(spec)` returns an exit code, or None to leave the container running."""

    def __init__(self, behavior=None):
        self.behavior = behavior or self.write_result
        self.containers: dict[str, dict] = {}
        self.started: list[ContainerSpec] = []

    @staticmethod
    def write_result(spec: ContainerSpec) -> int:
        n = sum(1 for _ in spec.input_dir.rglob("*.dcm"))
        (spec.output_dir / "result.txt").write_text(f"{n} instances")
        return 0

    def start(self, spec):
        self.started.append(spec)
        self.containers[spec.run_id] = {"exit": self.behavior(spec), "killed": False}
        return spec.run_id

    def find(self, run_id):
        return run_id if run_id in self.containers else None

    def exit_code(self, handle):
        return self.containers[handle]["exit"]

    def logs(self, handle):
        return b"log output\n"

    def kill(self, handle):
        self.containers[handle].update(exit=137, killed=True)

    def remove(self, handle):
        self.containers.pop(handle, None)


@pytest.fixture
def pacs():
    return MemoryPacs()


@pytest.fixture
def runtime():
    return FakeRuntime()
