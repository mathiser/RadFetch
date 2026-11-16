"""Accounts, roles, individual permissions, flow restrictions and sign-in."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radfetch import auth, store
from radfetch.api import create_app

PW = "correct horse battery"


def make(conn, username, role, **kw):
    store.create_user(conn, username, role, auth.hash_password(PW), **kw)
    return {"Authorization": "Bearer " + store.create_token(conn, username)}


@pytest.fixture
def client(conn, settings):
    with TestClient(create_app(settings)) as c:
        yield c


def test_roles_build_on_each_other():
    order = list(auth.ROLES)
    assert order == ["monitor", "viewer", "submitter", "operator", "admin"]
    for lower, higher in zip(order, order[1:], strict=False):
        assert auth.ROLES[lower] < auth.ROLES[higher]
    assert set().union(*auth.ROLES.values()) == set(auth.PERMISSIONS)  # every permission is reachable
    assert auth.effective_permissions("viewer", ["jobs.submit"], ["identifiers.view"]) == {
        "jobs.view", "stats.view", "results.view", "jobs.submit"}


def test_password_hashing():
    stored = auth.hash_password(PW)
    assert stored.startswith("scrypt$") and PW not in stored
    assert auth.verify_password(PW, stored) and not auth.verify_password(PW + "x", stored)
    assert not auth.verify_password(PW, None) and not auth.verify_password(PW, "garbage")
    with pytest.raises(ValueError):
        auth.hash_password("short")


def test_each_role_can_do_what_it_should(conn, client):
    monitor, viewer = make(conn, "mon", "monitor"), make(conn, "vic", "viewer")
    sub, other_sub = make(conn, "sue", "submitter"), make(conn, "sam", "submitter")
    op, admin = make(conn, "olga", "operator"), make(conn, "ada", "admin")
    body = {"accession_number": "ACC1", "flow": "seg"}

    assert client.get("/jobs").status_code == 401  # accounts exist, so the API is no longer open
    assert client.post("/jobs", json=body, headers=viewer).status_code == 403
    job = client.post("/jobs", json=body, headers=sub).json()["id"]

    # A monitor sees the job and its state, but nothing identifying.
    seen = client.get(f"/jobs/{job}", headers=monitor).json()
    assert seen["state"] == "queued" and seen["accession_number"] is None
    assert all(e["detail"] == {} for e in seen["events"])
    assert client.get("/jobs", params={"search": "ACC"}, headers=monitor).status_code == 403
    assert client.get("/stats/overview", headers=monitor).status_code == 200
    assert client.get(f"/jobs/{job}", headers=viewer).json()["accession_number"] == "ACC1"
    assert [j["id"] for j in client.get("/jobs", params={"search": "acc"}, headers=viewer).json()] == [job]

    # A submitter manages only their own work; an operator manages everyone's.
    assert client.post(f"/jobs/{job}/cancel", headers=other_sub).status_code == 403
    assert client.post(f"/jobs/{job}/cancel", headers=sub).json() == {"cancelled": True}
    assert client.post(f"/jobs/{job}/retry", headers=op).json() == {"state": "queued"}

    batch = client.post("/batches", json={"name": "b", "flow": "seg", "accession_numbers": ["A"]}, headers=sub).json()
    assert client.post(f"/batches/{batch['id']}/pause", headers=other_sub).status_code == 403
    assert client.post(f"/batches/{batch['id']}/pause", headers=sub).json()["paused"] is True
    assert client.get(f"/batches/{batch['id']}/jobs.csv", headers=monitor).status_code == 403
    assert client.get(f"/batches/{batch['id']}/jobs.csv", headers=viewer).status_code == 200

    # Only an admin manages users and reads the audit log.
    assert client.get("/users", headers=op).status_code == 403
    assert client.get("/audit", headers=op).status_code == 403
    assert {u["username"] for u in client.get("/users", headers=admin).json()} == {"mon", "vic", "sue", "sam", "olga", "ada"}
    kinds = {e["kind"] for e in client.get("/audit", headers=admin).json()}
    assert {"user_created", "token_created", "batch_paused", "cancelled"} <= kinds


def test_individual_grants_denies_and_flow_restriction(conn, client):
    admin = make(conn, "ada", "admin")
    # A viewer who may also submit, but only the seg flow, and without seeing identifiers.
    r = client.post("/users", headers=admin, json={
        "username": "pat", "role": "viewer", "grants": ["jobs.submit", "jobs.manage_own"],
        "denies": ["identifiers.view"], "allowed_flows": ["seg"]})
    assert r.status_code == 201 and len(r.json()["generated_password"]) >= 16
    pat = {"Authorization": "Bearer " + client.post("/users/pat/tokens", json={}, headers=admin).json()["token"]}

    me = client.get("/whoami", headers=pat).json()
    assert me["permissions"] == ["jobs.manage_own", "jobs.submit", "jobs.view", "results.view", "stats.view"]
    assert me["allowed_flows"] == ["seg"] and client.get("/flows", headers=pat).json() == ["seg"]

    assert client.post("/jobs", json={"accession_number": "A", "flow": "allct"}, headers=pat).status_code == 403
    mine = client.post("/jobs", json={"accession_number": "A", "flow": "seg"}, headers=pat).json()["id"]
    hidden = client.post("/jobs", json={"accession_number": "B", "flow": "allct"}, headers=admin).json()["id"]
    assert [j["id"] for j in client.get("/jobs", headers=pat).json()] == [mine]
    assert client.get(f"/jobs/{hidden}", headers=pat).status_code == 404  # indistinguishable from absent
    assert client.post(f"/jobs/{hidden}/cancel", headers=pat).status_code == 404
    assert client.get(f"/jobs/{mine}", headers=pat).json()["accession_number"] is None
    assert client.get("/stats/overview", headers=pat).json()["jobs"] == {"queued": 1}

    # The audit log, when granted, follows the same limits: no identifiers, no other flows' jobs.
    assert client.get("/audit", headers=pat).status_code == 403
    client.patch("/users/pat", headers=admin, json={"grants": ["jobs.submit", "jobs.manage_own", "audit.view"]})
    seen = client.get("/audit", headers=pat).json()
    assert {e["job_id"] for e in seen if e["job_id"]} == {mine}
    assert all(set(e["detail"]) <= {"by"} for e in seen if e["job_id"])
    assert any(e["kind"] == "user_created" for e in seen)

    # Changing the adjustments takes effect on the next request.
    client.patch("/users/pat", headers=admin, json={"denies": [], "all_flows": True})
    assert client.get(f"/jobs/{hidden}", headers=pat).json()["accession_number"] == "B"

    assert client.post("/users", headers=admin, json={"username": "x", "role": "boss"}).status_code == 422
    assert client.patch("/users/pat", headers=admin, json={"grants": ["fly"]}).status_code == 422
    assert client.post("/users", headers=admin, json={"username": "pat", "role": "viewer"}).status_code == 409


def test_login_session_logout_and_lockout(conn, client):
    store.create_user(conn, "ada", "admin", auth.hash_password(PW))
    assert client.post("/auth/login", json={"username": "ada", "password": "wrong-password"}).status_code == 401
    assert client.post("/auth/login", json={"username": "nobody", "password": PW}).status_code == 401
    r = client.post("/auth/login", json={"username": "ada", "password": PW})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/whoami").json()["name"] == "ada"  # the cookie is sent from here on
    client.post("/auth/logout")
    assert client.get("/whoami").status_code == 401

    for _ in range(10):
        client.post("/auth/login", json={"username": "ada", "password": "wrong-password"})
    assert client.post("/auth/login", json={"username": "ada", "password": PW}).status_code == 401  # locked
    conn.execute("update users set locked_until = now() - interval '1 second'")
    assert client.post("/auth/login", json={"username": "ada", "password": PW}).status_code == 200


def test_own_password_and_tokens(conn, client):
    store.create_user(conn, "sue", "submitter", auth.hash_password(PW))
    store.create_user(conn, "ada", "admin", auth.hash_password(PW))
    client.post("/auth/login", json={"username": "sue", "password": PW})
    token = client.post("/me/tokens", json={"name": "laptop"}).json()["token"]
    assert [t["name"] for t in client.get("/me/tokens").json()] == ["laptop"]
    assert client.post("/me/password", json={"current_password": "nope-nope-nope", "new_password": PW + "2"}).status_code == 403
    assert client.post("/me/password", json={"current_password": PW, "new_password": "short"}).status_code == 422
    assert client.post("/me/password", json={"current_password": PW, "new_password": PW + "2"}).status_code == 200
    assert client.get("/whoami").status_code == 401  # changing the password signs every session out
    client.cookies.clear()
    bearer = {"Authorization": "Bearer " + token}
    assert client.get("/whoami", headers=bearer).json()["name"] == "sue"  # tokens survive a password change
    assert client.delete("/me/tokens/laptop", headers=bearer).json() == {"revoked": True}
    assert client.get("/whoami", headers=bearer).status_code == 401


def test_disabling_and_deleting_users(conn, client):
    admin = make(conn, "ada", "admin")
    sue = make(conn, "sue", "submitter")
    client.patch("/users/sue", headers=admin, json={"disabled": True})
    assert client.get("/whoami", headers=sue).status_code == 401
    assert client.post("/auth/login", json={"username": "sue", "password": PW}).status_code == 401
    client.patch("/users/sue", headers=admin, json={"disabled": False})
    assert client.get("/whoami", headers=sue).status_code == 200
    assert client.delete("/users/sue", headers=admin).json() == {"deleted": True}
    assert client.get("/whoami", headers=sue).status_code == 401


def test_the_last_admin_cannot_be_removed(conn, client):
    admin = make(conn, "ada", "admin")
    make(conn, "olga", "operator")
    for change in ({"role": "operator"}, {"disabled": True}, {"denies": ["users.manage"]}):
        r = client.patch("/users/ada", headers=admin, json=change)
        assert r.status_code == 409 and "administrator" in r.json()["detail"]
    assert client.delete("/users/ada", headers=admin).status_code == 409
    assert client.get("/users", headers=admin).status_code == 200  # nothing was changed

    # A second admin only counts once they can actually sign in.
    client.post("/users", headers=admin, json={"username": "bob", "role": "admin", "service_account": True})
    assert client.patch("/users/ada", headers=admin, json={"role": "operator"}).status_code == 409
    client.post("/users/bob/tokens", json={}, headers=admin)
    assert client.patch("/users/ada", headers=admin, json={"role": "operator"}).status_code == 200
