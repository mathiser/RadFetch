"""A small in-memory Q/R SCP with synthetic studies, for development and tests.

It answers C-ECHO, C-FIND (study and series level), C-GET and C-MOVE. It is not a
PACS: no persistence, no patient level, minimal matching.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from io import BytesIO

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydicom import dcmwrite
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid
from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelGet,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)

CT = "1.2.840.10008.5.1.4.1.1.2"
SC = "1.2.840.10008.5.1.4.1.1.7"


@dataclass
class FakeSeries:
    modality: str
    description: str
    instances: int
    uid: str = field(default_factory=generate_uid)


@dataclass
class FakeStudy:
    accession_number: str
    series: list[FakeSeries]
    uid: str = field(default_factory=generate_uid)
    description: str = "Synthetic study"


def default_studies(n: int = 5) -> list[FakeStudy]:
    return [
        FakeStudy(
            f"ACC{i:04d}",
            [
                FakeSeries("CT", "Thorax 2.0 B31f", 12),
                FakeSeries("CT", "Scout", 2),
                FakeSeries("OT", "Dose report", 1),
            ],
        )
        for i in range(1, n + 1)
    ]


def _instance(study: FakeStudy, series: FakeSeries, number: int) -> Dataset:
    ds = Dataset()
    ds.SOPClassUID = CT if series.modality == "CT" else SC
    ds.SOPInstanceUID = generate_uid()
    ds.PatientName = "Synthetic^Patient"
    ds.PatientID = f"SYN-{study.accession_number}"
    ds.AccessionNumber = study.accession_number
    ds.StudyInstanceUID = study.uid
    ds.SeriesInstanceUID = series.uid
    ds.Modality = series.modality
    ds.SeriesDescription = series.description
    ds.InstanceNumber = number
    ds.Rows = ds.Columns = 8
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = bytes(64)
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    return ds


class FakePacsServer:
    def __init__(
        self,
        studies: list[FakeStudy] | None = None,
        ae_title: str = "PACS",
        move_destinations: dict[str, tuple[str, int]] | None = None,
    ):
        self.studies = studies if studies is not None else default_studies()
        self.ae_title = ae_title
        self.move_destinations = move_destinations or {}
        self.counts = {"find": 0, "get": 0, "move": 0}
        self._instances = {
            series.uid: [_instance(study, series, i + 1) for i in range(series.instances)]
            for study in self.studies
            for series in study.series
        }
        self._server = None

    # --- handlers ------------------------------------------------------------------

    def _handle_find(self, event):
        self.counts["find"] += 1
        q = event.identifier
        level = q.get("QueryRetrieveLevel")
        if level == "STUDY":
            for study in self.studies:
                if q.get("AccessionNumber") not in (None, "", study.accession_number):
                    continue
                if q.get("StudyInstanceUID") not in (None, "", study.uid):
                    continue
                r = Dataset()
                r.QueryRetrieveLevel = "STUDY"
                r.AccessionNumber = study.accession_number
                r.StudyInstanceUID = study.uid
                r.StudyDescription = study.description
                r.StudyDate = "20260101"
                yield 0xFF00, r
        elif level == "SERIES":
            for study in self.studies:
                if q.get("StudyInstanceUID") != study.uid:
                    continue
                for series in study.series:
                    r = Dataset()
                    r.QueryRetrieveLevel = "SERIES"
                    r.StudyInstanceUID = study.uid
                    r.SeriesInstanceUID = series.uid
                    r.Modality = series.modality
                    r.SeriesDescription = series.description
                    r.NumberOfSeriesRelatedInstances = series.instances
                    yield 0xFF00, r
        else:
            yield 0xC000, None

    def _matching(self, q: Dataset) -> list[Dataset]:
        series_uid = q.get("SeriesInstanceUID")
        if series_uid:
            return list(self._instances.get(series_uid, []))
        out = []
        for study in self.studies:
            if study.uid == q.get("StudyInstanceUID"):
                for series in study.series:
                    out.extend(self._instances[series.uid])
        return out

    def _handle_get(self, event):
        self.counts["get"] += 1
        instances = self._matching(event.identifier)
        yield len(instances)
        for ds in instances:
            if event.is_cancelled:
                yield 0xFE00, None
                return
            yield 0xFF00, ds

    def _handle_move(self, event):
        self.counts["move"] += 1
        dest = self.move_destinations.get(event.move_destination)
        if dest is None:
            yield None, None
            return
        yield dest
        instances = self._matching(event.identifier)
        yield len(instances)
        for ds in instances:
            if event.is_cancelled:
                yield 0xFE00, None
                return
            yield 0xFF00, ds

    # --- lifecycle -----------------------------------------------------------------

    def start(self, port: int = 11112, block: bool = False):
        ae = AE(ae_title=self.ae_title)
        for uid in (CT, SC):
            ae.add_requested_context(uid)  # storage SCU, for C-MOVE sub-operations
            # storage for C-GET: the requestor acts as SCP on the same association
            ae.add_supported_context(uid, scu_role=False, scp_role=True)
        for sop in (
            Verification,
            StudyRootQueryRetrieveInformationModelFind,
            StudyRootQueryRetrieveInformationModelGet,
            StudyRootQueryRetrieveInformationModelMove,
        ):
            ae.add_supported_context(sop)
        handlers = [
            (evt.EVT_C_FIND, self._handle_find),
            (evt.EVT_C_GET, self._handle_get),
            (evt.EVT_C_MOVE, self._handle_move),
        ]
        self._server = ae.start_server(("0.0.0.0", port), block=block, evt_handlers=handlers)
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None


def dicomweb_app(server: FakePacsServer, page_limit: int = 1000, token: str | None = None):
    """The same synthetic studies over QIDO-RS and WADO-RS, as a FastAPI app."""
    app = FastAPI()

    def check(request: Request) -> None:
        if token and request.headers.get("authorization") != f"Bearer {token}":
            raise HTTPException(401)

    def page(items: list[dict], limit: int, offset: int):
        chunk = items[offset : offset + min(limit, page_limit)]
        return JSONResponse(chunk, media_type="application/dicom+json") if chunk else Response(status_code=204)

    @app.get("/studies")
    def studies(request: Request, AccessionNumber: str = "", limit: int = 1000, offset: int = 0):
        check(request)
        server.counts["find"] += 1
        items = [
            {
                "0020000D": {"vr": "UI", "Value": [s.uid]},
                "00080050": {"vr": "SH", "Value": [s.accession_number]},
                "00081030": {"vr": "LO", "Value": [s.description]},
                "00080020": {"vr": "DA", "Value": ["20260101"]},
            }
            for s in server.studies
            if AccessionNumber in ("", s.accession_number)
        ]
        return page(items, limit, offset)

    @app.get("/studies/{study_uid}/series")
    def series(request: Request, study_uid: str, limit: int = 1000, offset: int = 0):
        check(request)
        server.counts["find"] += 1
        items = [
            {
                "0020000E": {"vr": "UI", "Value": [se.uid]},
                "00080060": {"vr": "CS", "Value": [se.modality]},
                "0008103E": {"vr": "LO", "Value": [se.description]},
                "00201209": {"vr": "IS", "Value": [se.instances]},
            }
            for st in server.studies
            if st.uid == study_uid
            for se in st.series
        ]
        return page(items, limit, offset)

    @app.get("/studies/{study_uid}/series/{series_uid}")
    def retrieve(request: Request, study_uid: str, series_uid: str):
        check(request)
        server.counts["get"] += 1
        instances = server._instances.get(series_uid)
        if not instances:
            raise HTTPException(404)
        boundary = "fake-pacs-boundary"

        def body():
            for ds in instances:
                buf = BytesIO()
                dcmwrite(buf, ds, enforce_file_format=True)
                yield f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode() + buf.getvalue() + b"\r\n"
            yield f"--{boundary}--\r\n".encode()

        return StreamingResponse(
            body(), media_type=f'multipart/related; type="application/dicom"; boundary={boundary}'
        )

    return app
