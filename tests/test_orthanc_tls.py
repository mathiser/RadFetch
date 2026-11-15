"""C-FIND and C-MOVE over mutual TLS against a real Orthanc started by the test.

Needs Docker with host networking; enabled with RADFETCH_TEST_ORTHANC_TLS=1.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import time
import uuid

import httpx
import pytest
from pydicom import dcmwrite

from radfetch.devtools.fakepacs import FakeSeries, FakeStudy, _instance
from radfetch.gateway.dimse import DimseClient
from radfetch.gateway.pacs import PacsError

pytestmark = pytest.mark.skipif(not os.environ.get("RADFETCH_TEST_ORTHANC_TLS"), reason="RADFETCH_TEST_ORTHANC_TLS not set")

HTTP_PORT, DICOM_PORT, MOVE_PORT = 8043, 4343, 11194


@pytest.fixture
def orthanc_tls(pki):
    certs = pki.tls_ca_file.parent
    for f in certs.iterdir():
        f.chmod(0o644)  # the container runs as another user
    name = f"orthanc-tls-{uuid.uuid4().hex[:8]}"
    env = {
        "ORTHANC__DICOM_AET": "ORTHANC",
        "ORTHANC__DICOM_PORT": str(DICOM_PORT),
        "ORTHANC__HTTP_PORT": str(HTTP_PORT),
        "ORTHANC__AUTHENTICATION_ENABLED": "false",
        "ORTHANC__REMOTE_ACCESS_ALLOWED": "true",
        "ORTHANC__DICOM_TLS_ENABLED": "true",
        "ORTHANC__DICOM_TLS_CERTIFICATE": "/certs/node.pem",
        "ORTHANC__DICOM_TLS_PRIVATE_KEY": "/certs/node.key",
        "ORTHANC__DICOM_TLS_TRUSTED_CERTIFICATES": "/certs/ca.pem",
        "ORTHANC__DICOM_TLS_REMOTE_CERTIFICATE_REQUIRED": "true",
        "ORTHANC__DICOM_MODALITIES": json.dumps(
            {"radfetch": {"AET": "RADFETCH", "Host": "127.0.0.1", "Port": MOVE_PORT, "UseDicomTls": True}}
        ),
    }
    args = ["docker", "run", "-d", "--rm", "--name", name, "--network", "host", "-v", f"{certs}:/certs:ro"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    subprocess.run([*args, "orthancteam/orthanc:latest"], check=True, capture_output=True)
    try:
        url = f"http://127.0.0.1:{HTTP_PORT}"
        deadline = time.monotonic() + 60
        while True:
            try:
                httpx.get(url + "/system", timeout=2).raise_for_status()
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:
                    logs = subprocess.run(["docker", "logs", "--tail", "30", name], capture_output=True, text=True)
                    pytest.fail("Orthanc did not start:\n" + logs.stdout + logs.stderr)
                time.sleep(1)
        pki.pacs_port, pki.pacs_ae_title, pki.local_ae_title = DICOM_PORT, "ORTHANC", "RADFETCH"
        pki.move_scp_port, pki.pacs_tls = MOVE_PORT, True
        pki.acse_timeout_s = pki.dimse_timeout_s = pki.network_timeout_s = 30
        yield pki, url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_find_and_move_over_mutual_tls(orthanc_tls, tmp_path):
    settings, url = orthanc_tls
    study = FakeStudy("TLS" + uuid.uuid4().hex[:8].upper(), [FakeSeries("CT", "Thorax", 6)])
    for i in range(6):
        buf = io.BytesIO()
        dcmwrite(buf, _instance(study, study.series[0], i + 1), enforce_file_format=True)
        httpx.post(url + "/instances", content=buf.getvalue(), timeout=30).raise_for_status()

    settings.retrieve_mode = "move"
    client = DimseClient(settings)
    try:
        assert [s.study_instance_uid for s in client.find_studies(study.accession_number)] == [study.uid]
        result = client.retrieve_series(study.uid, study.series[0].uid, tmp_path / "s")
        assert (result.received, result.failed) == (6, 0)  # Orthanc connected back to our TLS storage SCP
    finally:
        client.close()

    settings.pacs_tls = False  # without TLS (and a client certificate) Orthanc must turn us away
    settings.retrieve_mode = "get"
    with pytest.raises(PacsError):
        DimseClient(settings).find_studies(study.accession_number)
