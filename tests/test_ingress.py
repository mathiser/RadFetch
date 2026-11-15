"""Push ingress: real C-STORE into the SCP, then the processor turns quiet studies into jobs."""

from __future__ import annotations

import socket

import pytest
from pynetdicom import AE

from radfetch import store
from radfetch.config import INTERACTIVE_PRIORITY
from radfetch.devtools.fakepacs import CT, SC, FakeSeries, FakeStudy, _instance
from radfetch.executor.worker import Executor
from radfetch.gateway.worker import Gateway
from radfetch.ingress import IngressProcessor, IngressServer
from radfetch.janitor import Janitor

from conftest import FakeRuntime, MemoryPacs

TRIGGERED = """
name: pushed
version: "7"
select:
  - modality: CT
    min_instances: 3
image: x
trigger:
  calling_ae_title: "^CT_"
"""


@pytest.fixture
def scp(conn, settings):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        settings.scp_port = s.getsockname()[1]
    settings.scp_stable_s = 0
    (settings.flows_dir / "pushed.yaml").write_text(TRIGGERED)
    server = IngressServer(settings)
    server.start()
    yield settings
    server.stop()


def study(accession="PUSH1"):
    return FakeStudy(accession, [FakeSeries("CT", "Thorax", 4), FakeSeries("OT", "Report", 1)])


def send(settings, st: FakeStudy, calling="CT_SCANNER", only=None, count=None):
    ae = AE(ae_title=calling)
    ae.add_requested_context(CT)
    ae.add_requested_context(SC)
    assoc = ae.associate("127.0.0.1", settings.scp_port)
    assert assoc.is_established
    statuses = []
    for series in st.series:
        if only and series is not only:
            continue
        for i in range(count or series.instances):
            statuses.append(assoc.send_c_store(_instance(st, series, i + 1)).Status)
    assoc.release()
    return statuses


def test_pushed_study_becomes_ready_job(conn, scp, runtime):
    st = study()
    assert set(send(scp, st)) == {0}
    processor = IngressProcessor(conn, scp)
    assert processor.step() and not processor.step()

    (job,) = store.list_jobs(conn)
    assert job["state"] == "ready" and job["source"] == "push" and job["flow"] == "pushed"
    assert job["accession_number"] == "PUSH1" and job["flow_version"] == "7"
    assert job["requested_by"] == "push:CT_SCANNER" and job["priority"] == 50
    assert store.job_series_uids(conn, job["id"]) == [st.series[0].uid]  # the report series was not selected
    assert not (scp.incoming_dir / st.uid).exists() and list((scp.incoming_dir / ".processing").iterdir()) == []

    Executor(conn, runtime, scp).step()
    spec = runtime.started[0]
    assert len(list((spec.input_dir / st.series[0].uid).glob("*.dcm"))) == 4
    assert store.get_job(conn, job["id"])["state"] == "done"  # this flow has no destinations


def test_study_not_processed_until_quiet(conn, scp):
    scp.scp_stable_s = 3600
    send(scp, study())
    assert not IngressProcessor(conn, scp).step()
    assert store.metrics(conn)["incoming_studies"] == 1


def test_non_matching_pushes_are_dropped(conn, scp):
    wrong_sender, too_small = study("A"), study("B")
    send(scp, wrong_sender, calling="MR_SCANNER")
    send(scp, too_small, count=1)
    processor = IngressProcessor(conn, scp)
    while processor.step():
        pass
    assert store.list_jobs(conn) == []
    assert not (scp.incoming_dir / wrong_sender.uid).exists()
    ignored = conn.execute("select count(*) as n from events where kind = 'push_ignored'").fetchone()["n"]
    assert ignored == 2


def test_flows_without_trigger_are_not_started_by_push(conn, scp):
    (scp.flows_dir / "pushed.yaml").unlink()  # leaves seg and allct, neither has a trigger
    send(scp, study())
    assert IngressProcessor(conn, scp).step()
    assert store.list_jobs(conn) == []


def test_crash_mid_processing_is_recovered(conn, scp):
    st = study()
    send(scp, st)
    processor = IngressProcessor(conn, scp)
    processor._triggered_flows = lambda: 1 / 0  # fail after the directory was moved aside
    with pytest.raises(ZeroDivisionError):
        processor.step()
    assert (scp.incoming_dir / st.uid).is_dir()  # put back
    assert store.list_jobs(conn) == []

    scp.scp_stable_s = 0
    conn.execute("update incoming_studies set last_received_at = now() - interval '1 second'")
    assert IngressProcessor(conn, scp).step()
    assert [j["state"] for j in store.list_jobs(conn)] == ["ready"]


def test_startup_recovers_orphaned_processing_dir(conn, scp):
    st = study()
    send(scp, st)
    orphan = scp.incoming_dir / ".processing" / f"{st.uid}.deadbeef"
    orphan.parent.mkdir(exist_ok=True)
    (scp.incoming_dir / st.uid).rename(orphan)  # as if the process died right after the rename
    processor = IngressProcessor(conn, scp)
    assert processor.step()
    assert [j["state"] for j in store.list_jobs(conn)] == ["ready"]


def test_malformed_uid_is_refused(conn, scp):
    st = study()
    st.uid = "1.2/../../evil"
    assert set(send(scp, st)) == {0xC000}
    assert store.metrics(conn)["incoming_studies"] == 0
    assert [p.name for p in scp.incoming_dir.iterdir() if not p.name.startswith(".")] == []


def test_push_unblocks_pull_job_waiting_for_same_series(conn, scp):
    """A pull job is waiting for a series; the same series then arrives by push."""
    st = study("ACC0001")
    st.series[0].uid = "1.2.1.1"  # the series MemoryPacs reports for ACC0001
    st.uid = "1.2.1"
    pull = store.submit_job(conn, "ACC0001", "seg", INTERACTIVE_PRIORITY)
    pacs = MemoryPacs()
    gateway = Gateway(conn, pacs, scp, disk_fraction=lambda: 1.0)  # disk full: resolves but cannot retrieve
    while gateway.step():
        pass
    assert store.get_job(conn, pull)["state"] == "retrieving"

    send(scp, st, only=st.series[0])
    IngressProcessor(conn, scp).step()
    assert store.get_job(conn, pull)["state"] == "ready" and pacs.retrieved == []


def test_pushed_series_survive_janitor_while_needed(conn, scp):
    scp.cache_ttl_hours = 0
    st = study()
    send(scp, st)
    IngressProcessor(conn, scp).step()
    Janitor(conn, scp, disk_fraction=lambda: 0.1).sweep()
    assert (scp.cache_dir / st.series[0].uid).is_dir()


def test_allow_list_rejects_other_senders(conn, settings):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        settings.scp_port = s.getsockname()[1]
    settings.scp_allowed_ae_titles = "CT_SCANNER, CT_ROOM2"
    server = IngressServer(settings)
    server.start()
    try:
        ae = AE(ae_title="INTRUDER")
        ae.add_requested_context(CT)
        assert not ae.associate("127.0.0.1", settings.scp_port).is_established
        assert set(send(settings, study(), calling="CT_ROOM2")) == {0}
    finally:
        server.stop()
