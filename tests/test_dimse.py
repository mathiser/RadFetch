"""DICOM networking against the in-process fake PACS, including a full run through real DIMSE."""

from __future__ import annotations

import socket
import threading

import pytest
from pydicom import dcmread
from pynetdicom import AE, AllStoragePresentationContexts, evt

from radfetch import store
from radfetch.config import INTERACTIVE_PRIORITY
from radfetch.devtools.fakepacs import FakePacsServer, _instance, default_studies
from radfetch.executor.runtime import ContainerSpec
from radfetch.executor.worker import Executor
from radfetch.gateway.dimse import DimseClient
from radfetch.gateway.pacs import PacsError
from radfetch.gateway.worker import Gateway
from radfetch.sender import Sender

from conftest import FakeRuntime


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(params=["get", "move"])
def dimse(request, settings):
    settings.pacs_host, settings.pacs_port = "127.0.0.1", free_port()
    settings.retrieve_mode, settings.move_scp_port = request.param, free_port()
    settings.acse_timeout_s = settings.dimse_timeout_s = settings.network_timeout_s = 10
    server = FakePacsServer(
        default_studies(3), move_destinations={settings.local_ae_title: ("127.0.0.1", settings.move_scp_port)}
    ).start(settings.pacs_port)
    client = DimseClient(settings)
    yield client, server
    client.close()
    server.stop()


def test_find_and_retrieve(dimse, tmp_path):
    client, server = dimse
    (study,) = client.find_studies("ACC0002")
    assert client.find_studies("MISSING") == []
    series = client.find_series(study.study_instance_uid)
    assert [(s.modality, s.instances) for s in series] == [("CT", 12), ("CT", 2), ("OT", 1)]

    result = client.retrieve_series(study.study_instance_uid, series[0].series_instance_uid, tmp_path / "s")
    files = list((tmp_path / "s").glob("*.dcm"))
    assert result.received == len(files) == 12 and result.failed == 0 and result.bytes > 0
    ds = dcmread(files[0])
    assert ds.SeriesInstanceUID == series[0].series_instance_uid and ds.AccessionNumber == "ACC0002"


def test_unreachable_pacs_raises_pacs_error(settings):
    settings.pacs_host, settings.pacs_port = "127.0.0.1", free_port()
    settings.acse_timeout_s = settings.network_timeout_s = 2
    with pytest.raises(PacsError):
        DimseClient(settings).find_studies("ACC0001")


def test_full_run_with_dicom_delivery(dimse, conn, settings):
    """PACS -> gateway -> executor -> C-STORE to a destination SCP."""
    client, _ = dimse
    received = []
    done = threading.Event()

    def on_store(event):
        received.append(event.dataset.SOPInstanceUID)
        done.set()
        return 0x0000

    dest_port = free_port()
    dest_ae = AE(ae_title="DEST")
    dest_ae.supported_contexts = AllStoragePresentationContexts
    dest = dest_ae.start_server(("127.0.0.1", dest_port), block=False, evt_handlers=[(evt.EVT_C_STORE, on_store)])
    (settings.flows_dir / "echo.yaml").write_text(
        "name: echo\nimage: x\nselect:\n  - modality: CT\n    min_instances: 5\n"
        f"destinations:\n  - type: dicom\n    host: 127.0.0.1\n    port: {dest_port}\n    ae_title: DEST\n"
    )

    def container(spec: ContainerSpec) -> int:
        # Stand-in for a model: emit one new DICOM object plus a non-DICOM file.
        study, series = default_studies(1)[0], default_studies(1)[0].series[0]
        _instance(study, series, 1).save_as(spec.output_dir / "result.dcm", enforce_file_format=True)
        (spec.output_dir / "report.json").write_text("{}")
        return 0

    try:
        job = store.submit_job(conn, "ACC0001", "echo", INTERACTIVE_PRIORITY)
        gateway = Gateway(conn, client, settings, disk_fraction=lambda: 0.1)
        while gateway.step():
            pass
        assert store.get_job(conn, job)["state"] == "ready"
        Executor(conn, FakeRuntime(container), settings).step()
        Sender(conn, settings).step()
        assert store.get_job(conn, job)["state"] == "done"
        assert done.wait(5) and len(received) == 1
    finally:
        dest.shutdown()
