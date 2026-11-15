"""TLS on DICOM connections, with a throwaway CA made by the openssl command."""

from __future__ import annotations

import socket

import pytest
from pynetdicom import AE, AllStoragePresentationContexts, evt

from radfetch import tls
from radfetch.devtools.fakepacs import CT, FakePacsServer, _instance, default_studies
from radfetch.gateway.dimse import DimseClient
from radfetch.gateway.pacs import PacsError
from radfetch.ingress import IngressServer
from radfetch.sender import DeliveryError, send_dicom


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_query_and_retrieve_over_tls(pki, tmp_path):
    pki.pacs_port, pki.pacs_tls = free_port(), True
    server = FakePacsServer(default_studies(1)).start(pki.pacs_port, ssl_context=tls.server_context(pki))
    try:
        client = DimseClient(pki)
        (study,) = client.find_studies("ACC0001")
        series = client.find_series(study.study_instance_uid)
        result = client.retrieve_series(study.study_instance_uid, series[0].series_instance_uid, tmp_path / "s")
        assert result.received == 12

        pki.pacs_tls = False  # a plaintext client must not get through to a TLS-only PACS
        with pytest.raises(PacsError):
            DimseClient(pki).find_studies("ACC0001")
    finally:
        server.stop()


def test_untrusted_pacs_certificate_is_refused(pki, tmp_path):
    pki.pacs_port, pki.pacs_tls = free_port(), True
    server = FakePacsServer(default_studies(1)).start(pki.pacs_port, ssl_context=tls.server_context(pki))
    try:
        pki.tls_ca_file = None  # fall back to the system store, which does not know the test CA
        with pytest.raises(PacsError):
            DimseClient(pki).find_studies("ACC0001")
    finally:
        server.stop()


def test_ingress_requires_client_certificate(pki, conn):
    pki.scp_port, pki.scp_tls = free_port(), True
    server = IngressServer(pki)
    server.start()
    try:
        study = default_studies(1)[0]
        ae = AE(ae_title="CT_TLS")
        ae.acse_timeout = ae.network_timeout = 5
        ae.add_requested_context(CT)
        assert not ae.associate("127.0.0.1", pki.scp_port).is_established  # plaintext

        import ssl

        anonymous = ssl.create_default_context(cafile=str(pki.tls_ca_file))  # trusts us, presents nothing
        assert not ae.associate("127.0.0.1", pki.scp_port, tls_args=(anonymous, "127.0.0.1")).is_established

        assoc = ae.associate("127.0.0.1", pki.scp_port, tls_args=(tls.client_context(pki), "127.0.0.1"))
        assert assoc.is_established
        assert assoc.send_c_store(_instance(study, study.series[0], 1)).Status == 0
        assoc.release()
    finally:
        server.stop()


def test_dicom_delivery_over_tls(pki, tmp_path):
    received = []
    port = free_port()
    dest_ae = AE(ae_title="DEST")
    dest_ae.supported_contexts = AllStoragePresentationContexts
    server = dest_ae.start_server(
        ("127.0.0.1", port), block=False, ssl_context=tls.server_context(pki),
        evt_handlers=[(evt.EVT_C_STORE, lambda e: received.append(e.dataset.SOPInstanceUID) or 0)],
    )
    try:
        out = tmp_path / "out"
        out.mkdir()
        study = default_studies(1)[0]
        _instance(study, study.series[0], 1).save_as(out / "r.dcm", enforce_file_format=True)
        dest = {"type": "dicom", "host": "127.0.0.1", "port": port, "ae_title": "DEST", "tls": True}
        send_dicom(out, dest, pki)
        assert len(received) == 1
        with pytest.raises(DeliveryError):
            send_dicom(out, {**dest, "tls": False}, pki)
    finally:
        server.shutdown()
