"""Two-step sign-in for password accounts: set-up, sign-in, recovery codes, policy, reset."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from radfetch import auth, store
from radfetch.api import create_app

PW = "correct horse battery"


def code_for(conn, username, offset=0):
    secret = conn.execute("select totp_secret from users where username = %s", (username,)).fetchone()["totp_secret"]
    return auth.totp_code(secret, int(time.time() // 30) + offset)


def login(client, username, code=None):
    body = {"username": username, "password": PW} | ({"code": code} if code else {})
    return client.post("/auth/login", json=body)


@pytest.fixture
def client(conn, settings):
    store.create_user(conn, "sue", "submitter", auth.hash_password(PW))
    store.create_user(conn, "ada", "admin", auth.hash_password(PW))
    with TestClient(create_app(settings)) as c:
        yield c


def enroll(client, conn, username="sue"):
    login(client, username)
    setup = client.post("/me/mfa/setup").json()
    codes = client.post("/me/mfa/confirm", json={"code": code_for(conn, username)}).json()["recovery_codes"]
    client.post("/auth/logout")
    return setup, codes


def test_totp_matches_the_published_test_vectors():
    # RFC 6238 appendix B, SHA-1, secret "12345678901234567890", truncated to 6 digits
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert auth.totp_code(secret, 59 // 30) == "287082"
    assert auth.totp_code(secret, 1111111109 // 30) == "081804"
    assert auth.totp_code(secret, 1234567890 // 30) == "005924"
    assert auth.totp_verify(secret, "287 082", None, now=59) == 1
    assert auth.totp_verify(secret, "287082", None, now=59 + 30) == 1  # one step of clock drift
    assert auth.totp_verify(secret, "287082", None, now=59 + 90) is None
    assert auth.totp_verify(secret, "287082", 1, now=59) is None  # already used


def test_set_up_then_sign_in_needs_the_code(conn, client):
    login(client, "sue")
    setup = client.post("/me/mfa/setup").json()
    assert setup["uri"].startswith("otpauth://totp/RadFetch:sue?secret=" + setup["secret"])
    assert setup["qr_svg"].startswith("<svg")
    assert client.post("/me/mfa/confirm", json={"code": "000000"}).status_code == 422
    assert client.get("/whoami").json()["mfa_enabled"] is False  # not on until confirmed
    codes = client.post("/me/mfa/confirm", json={"code": code_for(conn, "sue")}).json()["recovery_codes"]
    assert len(codes) == 10 and client.get("/whoami").json()["mfa_enabled"] is True
    client.post("/auth/logout")

    r = login(client, "sue")
    assert r.status_code == 401 and r.json()["mfa_required"] is True and client.get("/whoami").status_code == 401
    assert login(client, "sue", "123456").status_code == 401
    assert login(client, "sue", code_for(conn, "sue", 1)).status_code == 200
    client.post("/auth/logout")
    assert login(client, "sue", code_for(conn, "sue", 1)).status_code == 401  # a code works once
    # a wrong password never reveals whether the account has a second factor
    r = client.post("/auth/login", json={"username": "sue", "password": "wrong-password"})
    assert r.status_code == 401 and "mfa_required" not in r.json()


def test_recovery_codes_work_once(conn, client):
    _, codes = enroll(client, conn)
    assert login(client, "sue", codes[0]).status_code == 200
    client.post("/auth/logout")
    assert login(client, "sue", codes[0]).status_code == 401
    assert login(client, "sue", codes[1].upper()).status_code == 200
    stored = conn.execute("select recovery_codes from users where username = 'sue'").fetchone()["recovery_codes"]
    assert len(stored) == 8 and all(len(h) == 64 for h in stored)  # hashes, and two fewer


def test_wrong_codes_lock_the_account(conn, client):
    enroll(client, conn)
    for _ in range(10):
        login(client, "sue", "000000")
    assert login(client, "sue", code_for(conn, "sue", 1)).status_code == 401


def test_removing_and_administrator_reset(conn, client):
    enroll(client, conn)
    login(client, "sue", code_for(conn, "sue", 1))
    assert client.post("/me/mfa/remove", json={"password": "wrong-password"}).status_code == 403
    assert client.post("/me/mfa/remove", json={"password": PW}).json() == {"removed": True}
    client.post("/auth/logout")
    assert login(client, "sue").status_code == 200  # no code needed any more
    client.post("/auth/logout")

    enroll(client, conn)
    login(client, "ada")
    assert client.patch("/users/sue", json={"reset_mfa": True}).status_code == 200
    client.post("/auth/logout")
    assert login(client, "sue").status_code == 200
    assert {e["kind"] for e in store.audit_log(conn)} >= {"mfa_enabled", "mfa_removed"}


def test_mandatory_two_step_sign_in(conn, settings):
    settings.require_mfa = True
    store.create_user(conn, "sue", "submitter", auth.hash_password(PW))
    token = {"Authorization": "Bearer " + store.create_token(conn, "sue")}
    with TestClient(create_app(settings)) as client:
        r = login(client, "sue")
        assert r.status_code == 200 and r.json()["mfa_setup_required"] is True
        # signed in, but nothing works until the second factor is set up
        assert client.get("/jobs").status_code == 403
        assert client.post("/jobs", json={"accession_number": "A", "flow": "seg"}).status_code == 403
        assert client.get("/whoami").json()["mfa_setup_required"] is True
        client.post("/me/mfa/setup")
        client.post("/me/mfa/confirm", json={"code": code_for(conn, "sue")})
        assert client.get("/jobs").status_code == 200
        assert client.post("/me/mfa/remove", json={"password": PW}).status_code == 409  # cannot be removed
        client.cookies.clear()
        assert client.get("/jobs", headers=token).status_code == 200  # API tokens are for programs: unaffected
