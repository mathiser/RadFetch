"""The PACS interface the gateway depends on. Implementations: dimse.DimseClient, tests' fakes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

_UID_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")


def valid_uid(uid: str) -> bool:
    """UIDs become directory names, so anything but digits and dots is refused."""
    return len(uid) <= 64 and bool(_UID_RE.match(uid))


class PacsError(Exception):
    """The PACS could not be reached or answered with a failure. Counts against the circuit breaker."""


@dataclass(frozen=True)
class StudyInfo:
    study_instance_uid: str
    accession_number: str
    description: str | None = None
    date: str | None = None


@dataclass(frozen=True)
class SeriesInfo:
    series_instance_uid: str
    study_instance_uid: str
    modality: str | None = None
    description: str | None = None
    instances: int | None = None  # NumberOfSeriesRelatedInstances; optional in DICOM


@dataclass(frozen=True)
class RetrieveResult:
    received: int  # instances written to disk
    failed: int  # failed sub-operations reported by the PACS
    bytes: int


class PacsClient(Protocol):
    def find_studies(self, accession_number: str) -> list[StudyInfo]: ...

    def find_series(self, study_instance_uid: str) -> list[SeriesInfo]: ...

    def retrieve_series(self, study_instance_uid: str, series_instance_uid: str, dest: Path) -> RetrieveResult:
        """Write every instance of the series into `dest` as `<SOPInstanceUID>.dcm`."""
        ...
