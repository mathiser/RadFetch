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
        assert client.post(f"/batches/{bid}/pause").json()["paused"] is True
        assert client.post(f"/batches/{bid}/resume").json()["paused"] is False
        assert len(client.get("/jobs", params={"batch_id": bid}).json()) == 3
        assert client.post(f"/batches/{bid}/cancel").json() == {"cancelled": 3}
        assert client.get(f"/batches/{bid}").json()["jobs"] == {"cancelled": 3}

        assert client.post(f"/jobs/{job['id']}/cancel").json() == {"cancelled": True}
        assert client.post(f"/jobs/{job['id']}/cancel").json() == {"cancelled": False}
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
