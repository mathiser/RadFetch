"""PacsClient over classic DICOM networking (C-FIND, C-GET or C-MOVE) using pynetdicom."""

from __future__ import annotations

import threading
from pathlib import Path

from pydicom.dataset import Dataset
from pynetdicom import AE, AllStoragePresentationContexts, build_role, evt
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind as FIND,
    StudyRootQueryRetrieveInformationModelGet as GET,
    StudyRootQueryRetrieveInformationModelMove as MOVE,
)

from radfetch.config import Settings
from radfetch.gateway.pacs import PacsError, RetrieveResult, SeriesInfo, StudyInfo

# Storage classes requested for C-GET. An association carries at most 128
# presentation contexts, so this is a curated list; extend it for your modalities.
GET_STORAGE_CLASSES = [
    "1.2.840.10008.5.1.4.1.1.2",  # CT Image
    "1.2.840.10008.5.1.4.1.1.2.1",  # Enhanced CT Image
    "1.2.840.10008.5.1.4.1.1.4",  # MR Image
    "1.2.840.10008.5.1.4.1.1.4.1",  # Enhanced MR Image
    "1.2.840.10008.5.1.4.1.1.128",  # PET Image
    "1.2.840.10008.5.1.4.1.1.130",  # Enhanced PET Image
    "1.2.840.10008.5.1.4.1.1.20",  # NM Image
    "1.2.840.10008.5.1.4.1.1.1",  # CR Image
    "1.2.840.10008.5.1.4.1.1.1.1",  # DX Image (presentation)
    "1.2.840.10008.5.1.4.1.1.1.2",  # Mammography Image (presentation)
    "1.2.840.10008.5.1.4.1.1.13.1.3",  # Breast Tomosynthesis Image
    "1.2.840.10008.5.1.4.1.1.6.1",  # US Image
    "1.2.840.10008.5.1.4.1.1.3.1",  # US Multi-frame Image
    "1.2.840.10008.5.1.4.1.1.12.1",  # XA Image
    "1.2.840.10008.5.1.4.1.1.7",  # Secondary Capture
    "1.2.840.10008.5.1.4.1.1.481.1",  # RT Image
    "1.2.840.10008.5.1.4.1.1.481.2",  # RT Dose
    "1.2.840.10008.5.1.4.1.1.481.3",  # RT Structure Set
    "1.2.840.10008.5.1.4.1.1.481.5",  # RT Plan
    "1.2.840.10008.5.1.4.1.1.66.1",  # Spatial Registration
    "1.2.840.10008.5.1.4.1.1.66.3",  # Deformable Spatial Registration
    "1.2.840.10008.5.1.4.1.1.66.4",  # Segmentation
    "1.2.840.10008.5.1.4.1.1.88.11",  # Basic Text SR
    "1.2.840.10008.5.1.4.1.1.88.22",  # Enhanced SR
    "1.2.840.10008.5.1.4.1.1.88.33",  # Comprehensive SR
    "1.2.840.10008.5.1.4.1.1.88.67",  # X-Ray Radiation Dose SR
]

_PENDING = (0xFF00, 0xFF01)


class _Sink:
    """Where incoming C-STORE instances go. Exactly one series is being retrieved at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._series: str | None = None
        self._dest: Path | None = None
        self.written: set[str] = set()
        self.bytes = 0

    def open(self, series_uid: str, dest: Path) -> None:
        with self._lock:
            self._series, self._dest = series_uid, dest
            self.written, self.bytes = set(), 0

    def close(self) -> None:
        with self._lock:
            self._series = self._dest = None

    def handle_store(self, event) -> int:
        ds = event.dataset
        sop_uid = str(event.request.AffectedSOPInstanceUID)
        with self._lock:
            # Refuse anything that is not part of the series being retrieved, e.g. a late
            # arrival from an earlier, timed-out C-MOVE.
            if self._dest is None or str(getattr(ds, "SeriesInstanceUID", "")) != self._series:
                return 0xA700  # out of resources: sender should not treat it as stored
            data = event.encoded_dataset()
            (self._dest / f"{sop_uid}.dcm").write_bytes(data)
            if sop_uid not in self.written:
                self.written.add(sop_uid)
                self.bytes += len(data)
        return 0x0000


class DimseClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._sink = _Sink()
        self._move_server = None
        if settings.retrieve_mode == "move":
            self._start_move_scp()

    # --- plumbing -----------------------------------------------------------------

    def _ae(self) -> AE:
        ae = AE(ae_title=self.s.local_ae_title)
        ae.acse_timeout = self.s.acse_timeout_s
        ae.dimse_timeout = self.s.dimse_timeout_s
        ae.network_timeout = self.s.network_timeout_s
        return ae

    def _associate(self, ae: AE, **kwargs):
        try:
            assoc = ae.associate(self.s.pacs_host, self.s.pacs_port, ae_title=self.s.pacs_ae_title, **kwargs)
        except Exception as exc:  # DNS failure, refused connection, ...
            raise PacsError(f"cannot connect to PACS: {exc}") from exc
        if not assoc.is_established:
            raise PacsError("association with PACS rejected, aborted or timed out")
        return assoc

    def _start_move_scp(self) -> None:
        ae = self._ae()
        ae.supported_contexts = AllStoragePresentationContexts
        self._move_server = ae.start_server(
            ("0.0.0.0", self.s.move_scp_port),
            block=False,
            evt_handlers=[(evt.EVT_C_STORE, self._sink.handle_store)],
        )

    def close(self) -> None:
        if self._move_server is not None:
            self._move_server.shutdown()
            self._move_server = None

    # --- queries ----------------------------------------------------------------------

    def _find(self, query: Dataset) -> list[Dataset]:
        ae = self._ae()
        ae.add_requested_context(FIND)
        assoc = self._associate(ae)
        try:
            results = []
            for status, identifier in assoc.send_c_find(query, FIND):
                if not status:
                    raise PacsError("C-FIND: connection lost or invalid response")
                if status.Status in _PENDING:
                    if identifier is not None:
                        results.append(identifier)
                elif status.Status != 0x0000:
                    raise PacsError(f"C-FIND failed with status 0x{status.Status:04X}")
            return results
        finally:
            assoc.release()

    def find_studies(self, accession_number: str) -> list[StudyInfo]:
        q = Dataset()
        q.QueryRetrieveLevel = "STUDY"
        q.AccessionNumber = accession_number
        q.StudyInstanceUID = ""
        q.StudyDescription = ""
        q.StudyDate = ""
        return [
            StudyInfo(
                study_instance_uid=str(r.StudyInstanceUID),
                accession_number=str(r.get("AccessionNumber", accession_number)),
                description=str(r.get("StudyDescription", "")) or None,
                date=str(r.get("StudyDate", "")) or None,
            )
            for r in self._find(q)
            if r.get("StudyInstanceUID")
        ]

    def find_series(self, study_instance_uid: str) -> list[SeriesInfo]:
        q = Dataset()
        q.QueryRetrieveLevel = "SERIES"
        q.StudyInstanceUID = study_instance_uid
        q.SeriesInstanceUID = ""
        q.Modality = ""
        q.SeriesDescription = ""
        q.NumberOfSeriesRelatedInstances = ""
        out = []
        for r in self._find(q):
            if not r.get("SeriesInstanceUID"):
                continue
            n = r.get("NumberOfSeriesRelatedInstances")
            out.append(
                SeriesInfo(
                    series_instance_uid=str(r.SeriesInstanceUID),
                    study_instance_uid=study_instance_uid,
                    modality=str(r.get("Modality", "")) or None,
                    description=str(r.get("SeriesDescription", "")) or None,
                    instances=int(n) if n not in (None, "") else None,
                )
            )
        return out

    # --- retrieval ---------------------------------------------------------------------

    def retrieve_series(self, study_instance_uid: str, series_instance_uid: str, dest: Path) -> RetrieveResult:
        dest.mkdir(parents=True, exist_ok=True)
        q = Dataset()
        q.QueryRetrieveLevel = "SERIES"
        q.StudyInstanceUID = study_instance_uid
        q.SeriesInstanceUID = series_instance_uid

        ae = self._ae()
        self._sink.open(series_instance_uid, dest)
        try:
            if self.s.retrieve_mode == "get":
                ae.add_requested_context(GET)
                for uid in GET_STORAGE_CLASSES:
                    ae.add_requested_context(uid)
                roles = [build_role(uid, scp_role=True) for uid in GET_STORAGE_CLASSES]
                assoc = self._associate(
                    ae, ext_neg=roles, evt_handlers=[(evt.EVT_C_STORE, self._sink.handle_store)]
                )
                responses = lambda: assoc.send_c_get(q, GET)  # noqa: E731
                op = "C-GET"
            else:
                ae.add_requested_context(MOVE)
                assoc = self._associate(ae)
                responses = lambda: assoc.send_c_move(q, self.s.local_ae_title, MOVE)  # noqa: E731
                op = "C-MOVE"
            try:
                failed = 0
                for status, _ in responses():
                    if not status:
                        raise PacsError(f"{op}: connection lost or invalid response")
                    if status.Status in _PENDING:
                        continue
                    failed = int(status.get("NumberOfFailedSuboperations", 0) or 0)
                    # 0x0000 success, 0xB000 warning: some sub-operations failed (reported via `failed`).
                    if status.Status not in (0x0000, 0xB000):
                        raise PacsError(f"{op} failed with status 0x{status.Status:04X}")
            finally:
                assoc.release()
            return RetrieveResult(received=len(self._sink.written), failed=failed, bytes=self._sink.bytes)
        finally:
            self._sink.close()
