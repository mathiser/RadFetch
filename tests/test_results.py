"""Structured results: stored from a flow's results.json, queryable, exportable, and permission-checked."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from radfetch import auth, store
from radfetch.api import create_app
from radfetch.config import INTERACTIVE_PRIORITY
from radfetch.executor.worker import Executor
from radfetch.gateway.worker import Gateway

from conftest import FakeRuntime


def run_job(conn, settings, pacs, accession, content, flow="allct"):
    """Run one job whose container writes `content` (str or object) as results.json."""

    def container(spec):
        if content is not None:
            text = content if isinstance(content, str) else json.dumps(content)
            (spec.output_dir / "results.json").write_text(text)
        return 0

    job = store.submit_job(conn, accession, flow, INTERACTIVE_PRIORITY)
    gateway = Gateway(conn, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)
    while gateway.step():
        pass
    Executor(conn, FakeRuntime(container), settings, sleep=lambda s: None).step()
    return job


def test_result_is_stored_with_the_job(conn, settings, pacs):
    job = run_job(conn, settings, pacs, "ACC0001", {"volume_ml": 412.5, "nodules": [{"mm": 6}, {"mm": 11}], "grade": 3})
    assert store.get_job(conn, job)["state"] == "done"
    result = store.get_result(conn, job)
    assert result["data"]["nodules"][1]["mm"] == 11 and result["flow"] == "allct"
    assert "result_stored" in [e["kind"] for e in store.job_events(conn, job)]

    silent = run_job(conn, settings, pacs, "ACC0002", None)  # a flow need not report anything
    assert store.get_result(conn, silent) is None and store.get_job(conn, silent)["state"] == "done"


@pytest.mark.parametrize("content,reason", [
    ("{not json", "not valid JSON"),
    ("42", "must hold a JSON object or array"),
    (json.dumps({"x": "a\u0000b"}), "NUL"),
    (json.dumps({"big": "x" * 2000}), "larger than"),
])
def test_unusable_result_files_do_not_fail_the_job(conn, settings, pacs, content, reason):
    settings.results_max_bytes = 1000
    job = run_job(conn, settings, pacs, "ACC0001", content)
    assert store.get_job(conn, job)["state"] == "done" and store.get_result(conn, job) is None
    (event,) = [e for e in store.job_events(conn, job) if e["kind"] == "result_rejected"]
    assert reason in event["detail"]["reason"]


def test_results_file_cannot_point_outside_the_output(conn, settings, pacs, tmp_path):
    (tmp_path / "secret.json").write_text('{"leak": true}')
    (settings.flows_dir / "sneaky.yaml").write_text(
        "name: sneaky\nimage: x\nresults_file: ../../../../secret.json\nselect:\n  - modality: CT\n")
    job = run_job(conn, settings, pacs, "ACC0001", None, flow="sneaky")
    assert store.get_result(conn, job) is None


def test_rerun_replaces_the_result(conn, settings, pacs):
    job = run_job(conn, settings, pacs, "ACC0001", {"grade": 1})
    store.cancel_job(conn, job)  # done jobs cannot be cancelled; force a rerun through a failed state instead
    conn.execute("update jobs set state = 'failed' where id = %s", (job,))
    store.retry_job(conn, job, full=True)
    gateway = Gateway(conn, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)
    while gateway.step():
        pass

    def container(spec):
        (spec.output_dir / "results.json").write_text('{"grade": 2}')
        return 0

    Executor(conn, FakeRuntime(container), settings, sleep=lambda s: None).step()
    assert store.get_result(conn, job)["data"] == {"grade": 2}
    assert conn.execute("select count(*) as n from results").fetchone()["n"] == 1


def test_query_export_and_permissions(conn, settings, pacs):
    a = run_job(conn, settings, pacs, "ACC0001", {"grade": 3, "volume_ml": 410.0, "site": {"lobe": "RUL"}})
    run_job(conn, settings, pacs, "ACC0002", {"grade": 1, "volume_ml": 12.5, "note": "=HYPERLINK(1)"})
    run_job(conn, settings, pacs, "ACC0003", {"grade": 3, "volume_ml": 388.0, "site": {"lobe": "LLL"}}, flow="seg")

    def token(name, role, **kw):
        store.create_user(conn, name, role, auth.hash_password("correct horse battery"), **kw)
        return {"Authorization": "Bearer " + store.create_token(conn, name)}

    viewer, monitor = token("vic", "viewer"), token("mon", "monitor")
    blind = token("stat", "viewer", denies=["identifiers.view"])
    seg_only = token("seg", "viewer", allowed_flows=["seg"])
    with TestClient(create_app(settings)) as client:
        assert client.get("/results", headers=monitor).status_code == 403
        assert client.get(f"/jobs/{a}/result", headers=monitor).status_code == 403
        assert client.get(f"/jobs/{a}/result", headers=viewer).json()["data"]["site"] == {"lobe": "RUL"}

        everything = client.get("/results", headers=viewer).json()
        assert [r["accession_number"] for r in everything] == ["ACC0003", "ACC0002", "ACC0001"]
        assert client.get("/results/flows", headers=viewer).json() == [{"flow": "allct", "n": 2}, {"flow": "seg", "n": 1}]

        # "containing" queries, including nested values, need no schema
        grade3 = client.get("/results", params={"contains": '{"grade": 3}'}, headers=viewer).json()
        assert sorted(r["accession_number"] for r in grade3) == ["ACC0001", "ACC0003"]
        nested = client.get("/results", params={"contains": '{"site": {"lobe": "LLL"}}'}, headers=viewer).json()
        assert [r["accession_number"] for r in nested] == ["ACC0003"]
        assert client.get("/results", params={"contains": "{oops"}, headers=viewer).status_code == 422

        rows = client.get("/results.csv", params={"flow": "allct"}, headers=viewer).text.splitlines()
        assert rows[0] == "accession_number,study_instance_uid,job_id,flow_version,created_at,grade,note,site,volume_ml"
        assert rows[1].startswith("ACC0002,") and rows[1].endswith(",1,'=HYPERLINK(1),,12.5")  # formula neutralised
        assert rows[2].endswith(',3,,"{""lobe"": ""RUL""}",410.0')  # nested value as JSON text

        # without identifiers.view the documents are there but not whose they are
        assert {r["accession_number"] for r in client.get("/results", headers=blind).json()} == {None}
        assert client.get("/results.csv", params={"flow": "allct"}, headers=blind).text.splitlines()[1].startswith(",,")
        # a flow restriction applies here too
        assert [r["flow"] for r in client.get("/results", headers=seg_only).json()] == ["seg"]
        assert client.get(f"/jobs/{a}/result", headers=seg_only).status_code == 404
