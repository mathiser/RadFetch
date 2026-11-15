from fastapi.testclient import TestClient

from radfetch.api import create_app


def test_api(conn, settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/flows").json() == ["allct", "seg"]
        assert client.post("/jobs", json={"accession_number": "ACC0001", "flow": "nope"}).status_code == 422

        r = client.post("/jobs", json={"accession_number": " ACC0001 ", "flow": "seg"}, headers={"X-Remote-User": "amy"})
        assert r.status_code == 201
        job = client.get(f"/jobs/{r.json()['id']}").json()
        assert job["state"] == "queued" and job["priority"] == 100 and job["accession_number"] == "ACC0001"
        assert job["requested_by"] == "amy" and job["events"][0]["kind"] == "submitted"

        b = client.post("/batches", json={"name": "trial", "flow": "seg", "accession_numbers": ["A", "B", "C"]})
        assert b.status_code == 201 and b.json()["jobs"] == {"queued": 3}
        bid = b.json()["id"]
        assert [(x["name"], x["jobs"]) for x in client.get("/batches").json()] == [("trial", {"queued": 3})]
        assert "<title>RadFetch</title>" in client.get("/").text
        conn.execute("update jobs set error = '=cmd()' where accession_number = 'B'")
        rows = client.get(f"/batches/{bid}/jobs.csv").text.splitlines()
        assert rows[0] == "accession_number,state,error,study_instance_uid,job_id,updated_at" and len(rows) == 4
        assert rows[2].startswith("B,queued,'=cmd(),")  # formula neutralised
        assert client.get(f"/jobs/{job['id']}").json()["runs"] == []
        assert client.post(f"/batches/{bid}/pause").json()["paused"] is True
        assert client.post(f"/batches/{bid}/resume").json()["paused"] is False
        assert len(client.get("/jobs", params={"batch_id": bid}).json()) == 3
        assert client.post(f"/batches/{bid}/cancel").json() == {"cancelled": 3}
        assert client.get(f"/batches/{bid}").json()["jobs"] == {"cancelled": 3}

        assert client.post(f"/jobs/{job['id']}/cancel").json() == {"cancelled": True}
        assert client.post(f"/jobs/{job['id']}/cancel").json() == {"cancelled": False}
        assert client.post(f"/jobs/{job['id']}/retry").json() == {"state": "queued"}
        assert client.post(f"/jobs/{job['id']}/retry").status_code == 409
        assert client.post(f"/jobs/{job['id']}/cancel").json() == {"cancelled": True}
        assert client.post(f"/batches/{bid}/retry-failed").json() == {"retried": 0}
        assert client.get("/jobs/00000000-0000-0000-0000-000000000000").status_code == 404
        assert client.get("/healthz").json() == {"ok": True, "jobs": {"cancelled": 4}, "status": {}}


def test_token_auth_and_metrics(conn, settings):
    from radfetch import store

    settings.api_token = "s3cret"
    store.set_status(conn, "gateway", breaker_open=True, consecutive_failures=4, batch_window_open=False, rate_budget_ok=True)
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200  # open, for container health checks
        assert client.get("/flows").status_code == 401
        assert client.post("/jobs", json={"accession_number": "A", "flow": "seg"}).status_code == 401
        assert client.get("/flows", headers={"Authorization": "Bearer wrong"}).status_code == 401
        auth = {"Authorization": "Bearer s3cret"}
        assert client.post("/jobs", json={"accession_number": "A", "flow": "seg"}, headers=auth).status_code == 201
        text = client.get("/metrics", headers=auth).text
        assert 'radfetch_jobs{state="queued"} 1' in text
        assert "radfetch_gateway_breaker_open 1" in text and "radfetch_gateway_consecutive_failures 4" in text
        assert "radfetch_cache_bytes 0" in text


def test_named_tokens_and_roles(conn, settings):
    from radfetch import store

    with TestClient(create_app(settings)) as client:
        assert client.get("/whoami").json() == {"name": None, "role": "operator"}  # open until a token exists
        viewer = {"Authorization": "Bearer " + store.create_token(conn, "ward-display", "viewer")}
        amy = {"Authorization": "Bearer " + store.create_token(conn, "amy", "operator")}

        assert client.get("/jobs").status_code == 401  # creating a token turned authentication on
        assert client.get("/whoami", headers=viewer).json() == {"name": "ward-display", "role": "viewer"}
        assert client.get("/jobs", headers=viewer).status_code == 200
        body = {"accession_number": "A1", "flow": "seg"}
        assert client.post("/jobs", json=body, headers=viewer).status_code == 403

        job_id = client.post("/jobs", json=body, headers=amy).json()["id"]
        client.post(f"/jobs/{job_id}/cancel", headers=amy)
        client.post(f"/jobs/{job_id}/retry", headers=amy)
        job = client.get(f"/jobs/{job_id}", headers=viewer).json()
        assert job["requested_by"] == "amy"
        assert [(e["kind"], e["detail"].get("by")) for e in job["events"]] == [
            ("submitted", "amy"), ("cancelled", "amy"), ("retried", "amy")]

        assert store.revoke_token(conn, "amy") and not store.revoke_token(conn, "amy")
        assert client.get("/jobs", headers=amy).status_code == 401
        assert [t["name"] for t in store.list_tokens(conn) if t["revoked_at"] is None] == ["ward-display"]
        stored = conn.execute("select token_hash from api_tokens").fetchall()
        assert all(len(r["token_hash"]) == 64 and not r["token_hash"].startswith("rf_") for r in stored)
