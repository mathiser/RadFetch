"""DICOMweb client against the fake QIDO-RS/WADO-RS server, plus the streaming multipart splitter."""

from __future__ import annotations

import io
import socket
import threading
import time

import pytest
import uvicorn
from pydicom import dcmread

from radfetch import store
from radfetch.config import INTERACTIVE_PRIORITY
from radfetch.devtools.fakepacs import FakePacsServer, default_studies, dicomweb_app
from radfetch.gateway.dicomweb import DicomWebClient, split_multipart
from radfetch.gateway.pacs import PacsError
from radfetch.gateway.worker import Gateway


def multipart(parts: list[bytes], boundary=b"B0UND") -> bytes:
    body = b"preamble to ignore\r\n"
    for p in parts:
        body += b"--" + boundary + b"\r\nContent-Type: application/dicom\r\nContent-Length: 1\r\n\r\n" + p + b"\r\n"
    return body + b"--" + boundary + b"--\r\ntrailer"


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 64, 10_000])
def test_split_multipart_any_chunking(chunk_size):
    # Parts containing CRLFs, dashes and a near-miss of the delimiter must come through byte for byte.
    parts = [b"first", b"", b"\r\n--B0UN\r\n--B0UNX" * 50, bytes(range(256)) * 20]
    body = multipart(parts)
    chunks = [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]
    files: list[io.BytesIO] = []

    class Keep(io.BytesIO):
        def close(self):  # keep contents readable after the splitter closes the part
            pass

    def open_part():
        files.append(Keep())
        return files[-1]

    assert split_multipart(chunks, b"B0UND", open_part) == 4
    assert [f.getvalue() for f in files] == parts


def test_split_multipart_truncated():
    body = multipart([b"abc" * 100])[:-30]
    with pytest.raises(PacsError, match="closing boundary"):
        split_multipart([body], b"B0UND", io.BytesIO)


@pytest.fixture
def web(settings):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    pacs = FakePacsServer(default_studies(3))
    server = uvicorn.Server(
        uvicorn.Config(dicomweb_app(pacs, page_limit=2, token="tok"), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    settings.retrieve_mode = "dicomweb"
    settings.dicomweb_url = f"http://127.0.0.1:{port}"
    settings.dicomweb_token = "tok"
    yield settings, pacs
    server.should_exit = True
    thread.join(5)


def test_query_and_retrieve(web, tmp_path):
    settings, pacs = web
    client = DicomWebClient(settings)
    (study,) = client.find_studies("ACC0002")
    assert client.find_studies("MISSING") == []
    series = client.find_series(study.study_instance_uid)  # 3 series through a server page size of 2
    assert [(s.modality, s.description, s.instances) for s in series] == [
        ("CT", "Thorax 2.0 B31f", 12), ("CT", "Scout", 2), ("OT", "Dose report", 1),
    ]
    result = client.retrieve_series(study.study_instance_uid, series[0].series_instance_uid, tmp_path / "s")
    files = sorted((tmp_path / "s").iterdir())
    assert result.received == len(files) == 12 and result.failed == 0
    assert result.bytes == sum(f.stat().st_size for f in files)
    assert all(f.suffix == ".dcm" for f in files)  # no leftover part files
    assert dcmread(files[0]).SeriesInstanceUID == series[0].series_instance_uid


def test_errors_become_pacs_errors(web, tmp_path):
    settings, _ = web
    settings.dicomweb_token = "wrong"
    with pytest.raises(PacsError, match="HTTP 401"):
        DicomWebClient(settings).find_studies("ACC0001")
    settings.dicomweb_token = "tok"
    with pytest.raises(PacsError, match="HTTP 404"):
        DicomWebClient(settings).retrieve_series("1.2", "9.9.9", tmp_path / "x")
    settings.dicomweb_url = "http://127.0.0.1:9"  # nothing listens here
    with pytest.raises(PacsError, match="request failed"):
        DicomWebClient(settings).find_studies("ACC0001")


def test_gateway_over_dicomweb(web, conn):
    settings, pacs = web
    job = store.submit_job(conn, "ACC0001", "seg", INTERACTIVE_PRIORITY)
    gateway = Gateway(conn, DicomWebClient(settings), settings, disk_fraction=lambda: 0.1)
    while gateway.step():
        pass
    assert store.get_job(conn, job)["state"] == "ready"
    (uid,) = store.job_series_uids(conn, job)
    assert len(list((settings.cache_dir / uid).glob("*.dcm"))) == 12 and pacs.counts["get"] == 1


def test_stow_delivery(web, conn, tmp_path, monkeypatch):
    """A run's DICOM output is delivered by STOW-RS; non-DICOM files are skipped."""
    from radfetch.devtools.fakepacs import _instance
    from radfetch.sender import DeliveryError, send_stow

    settings, pacs = web
    out = tmp_path / "out"
    out.mkdir()
    study = default_studies(1)[0]
    sent = [_instance(study, study.series[0], i) for i in (1, 2, 3)]
    for i, ds in enumerate(sent):
        ds.save_as(out / f"r{i}.dcm", enforce_file_format=True)
    (out / "notes.txt").write_text("not dicom")
    dest = {"type": "stow", "url": settings.dicomweb_url, "token_env": "STOW_TOKEN"}

    with pytest.raises(DeliveryError, match="STOW_TOKEN is not set"):
        send_stow(out, dest, settings)
    monkeypatch.setenv("STOW_TOKEN", "wrong")
    with pytest.raises(DeliveryError, match="HTTP 401"):
        send_stow(out, dest, settings)
    monkeypatch.setenv("STOW_TOKEN", "tok")
    send_stow(out, dest, settings)
    assert set(pacs.stored) == {ds.SOPInstanceUID for ds in sent}
    assert pacs.stored[sent[0].SOPInstanceUID] == (out / "r0.dcm").read_bytes()  # byte for byte
