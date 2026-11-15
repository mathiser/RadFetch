"""Integration tests against a real Orthanc. Skipped unless RADFETCH_TEST_ORTHANC_URL is set.

Orthanc must run with AET ORTHANC, the DICOMweb plugin, no authentication, and a
modality RADFETCH -> <this host>:11193 for C-MOVE. See .github/workflows/test.yml.
"""

from __future__ import annotations

import io
import os
import uuid

import httpx
import pytest
from pydicom import dcmread, dcmwrite

from radfetch import store
from radfetch.config import INTERACTIVE_PRIORITY
from radfetch.devtools.fakepacs import FakeSeries, FakeStudy, _instance
from radfetch.gateway.dicomweb import DicomWebClient
from radfetch.gateway.dimse import DimseClient
from radfetch.gateway.worker import Gateway
from radfetch.sender import send_dicom, send_stow

URL = os.environ.get("RADFETCH_TEST_ORTHANC_URL")
pytestmark = pytest.mark.skipif(not URL, reason="RADFETCH_TEST_ORTHANC_URL not set")


@pytest.fixture
def orthanc(settings):
    settings.pacs_host = httpx.URL(URL).host
    settings.pacs_port = int(os.environ.get("RADFETCH_TEST_ORTHANC_DICOM_PORT", "4242"))
    settings.pacs_ae_title = "ORTHANC"
    settings.local_ae_title = "RADFETCH"
    settings.move_scp_port = 11193
    settings.dicomweb_url = URL.rstrip("/") + "/dicom-web"
    settings.acse_timeout_s = settings.dimse_timeout_s = settings.network_timeout_s = 30
    return settings


@pytest.fixture
def study():
    """A fresh synthetic study uploaded through Orthanc's REST API."""
    st = FakeStudy(
        "RF" + uuid.uuid4().hex[:10].upper(),
        [FakeSeries("CT", "Thorax 2.0", 12), FakeSeries("CT", "Scout", 2), FakeSeries("OT", "Dose report", 1)],
    )
    with httpx.Client(base_url=URL, timeout=30) as http:
        for series in st.series:
            for i in range(series.instances):
                buf = io.BytesIO()
                dcmwrite(buf, _instance(st, series, i + 1), enforce_file_format=True)
                http.post("/instances", content=buf.getvalue()).raise_for_status()
    return st


def make_client(settings, mode):
    settings.retrieve_mode = mode
    return DicomWebClient(settings) if mode == "dicomweb" else DimseClient(settings)


@pytest.mark.parametrize("mode", ["get", "move", "dicomweb"])
def test_query_and_retrieve(orthanc, study, tmp_path, mode):
    client = make_client(orthanc, mode)
    try:
        found = client.find_studies(study.accession_number)
        assert [s.study_instance_uid for s in found] == [study.uid]
        assert client.find_studies("NO-SUCH-ACCESSION") == []

        series = {s.series_instance_uid: s for s in client.find_series(study.uid)}
        assert set(series) == {s.uid for s in study.series}
        thorax = series[study.series[0].uid]
        assert (thorax.modality, thorax.description, thorax.instances) == ("CT", "Thorax 2.0", 12)

        result = client.retrieve_series(study.uid, thorax.series_instance_uid, tmp_path / "s")
        files = sorted((tmp_path / "s").glob("*.dcm"))
        assert (result.received, result.failed, len(files)) == (12, 0, 12)
        assert {dcmread(f).SeriesInstanceUID for f in files} == {thorax.series_instance_uid}
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["get", "move", "dicomweb"])
def test_gateway_end_to_end(orthanc, study, conn, mode):
    client = make_client(orthanc, mode)
    try:
        job = store.submit_job(conn, study.accession_number, "seg", INTERACTIVE_PRIORITY)
        gateway = Gateway(conn, client, orthanc, disk_fraction=lambda: 0.1)
        while gateway.step():
            pass
        row = store.get_job(conn, job)
        assert row["state"] == "ready", row["error"]
        assert store.job_series_uids(conn, job) == [study.series[0].uid]
    finally:
        client.close()


@pytest.mark.parametrize("how", ["dicom", "stow"])
def test_delivery_into_orthanc(orthanc, tmp_path, how):
    out = tmp_path / "out"
    out.mkdir()
    st = FakeStudy("RFOUT" + uuid.uuid4().hex[:8].upper(), [FakeSeries("CT", "Result", 3)])
    for i in range(3):
        _instance(st, st.series[0], i + 1).save_as(out / f"{i}.dcm", enforce_file_format=True)
    (out / "report.json").write_text("{}")
    if how == "dicom":
        dest = {"type": "dicom", "host": orthanc.pacs_host, "port": orthanc.pacs_port, "ae_title": "ORTHANC"}
        send_dicom(out, dest, orthanc)
    else:
        send_stow(out, {"type": "stow", "url": orthanc.dicomweb_url}, orthanc)
    found = httpx.post(
        URL.rstrip("/") + "/tools/find",
        json={"Level": "Instance", "Query": {"AccessionNumber": st.accession_number}},
        timeout=30,
    ).json()
    assert len(found) == 3
