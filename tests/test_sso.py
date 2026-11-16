"""Single sign-on against a small OpenID Connect provider run inside the test."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from radfetch import auth, store
from radfetch.api import create_app


class Provider:
    """Signs in whoever `identity` says, without asking. Knobs let a test make it misbehave."""

    def __init__(self, port: int):
        self.issuer = f"http://127.0.0.1:{port}"
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.signing_key = self.key
        self.identity = {"sub": "u-1", "preferred_username": "anna", "groups": ["radiology"], "amr": ["pwd", "mfa"]}
        self.audience = "radfetch"
        self.nonce_override = None
        self.codes: dict[str, dict] = {}
        self.authorize_requests: list[dict] = []
        app = FastAPI()

        @app.get("/.well-known/openid-configuration")
        def discovery():
            return {"issuer": self.issuer, "authorization_endpoint": self.issuer + "/authorize",
                    "token_endpoint": self.issuer + "/token", "jwks_uri": self.issuer + "/jwks",
                    "end_session_endpoint": self.issuer + "/logout"}

        @app.get("/jwks")
        def jwks():
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}

        @app.get("/authorize")
        def authorize(client_id: str, redirect_uri: str, state: str, nonce: str, code_challenge: str,
                      code_challenge_method: str, scope: str, response_type: str, acr_values: str = ""):
            self.authorize_requests.append({"scope": scope, "acr_values": acr_values, "redirect_uri": redirect_uri})
            code = f"code-{len(self.codes)}"
            self.codes[code] = {"nonce": nonce, "challenge": code_challenge, "client_id": client_id}
            return RedirectResponse(f"{redirect_uri}?code={code}&state={state}", 302)

        @app.post("/token")
        def token(code: str = Form(), code_verifier: str = Form(), client_id: str = Form(),
                  client_secret: str = Form(""), grant_type: str = Form(), redirect_uri: str = Form()):
            issued = self.codes.pop(code, None)  # a code works once
            challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
            if issued is None or challenge != issued["challenge"] or client_secret != "s3cret":
                raise HTTPException(400, "invalid_grant")
            now = int(time.time())
            claims = {"iss": self.issuer, "aud": self.audience, "iat": now, "exp": now + 300,
                      "nonce": self.nonce_override or issued["nonce"], **self.identity}
            return {"access_token": "at", "token_type": "Bearer",
                    "id_token": jwt.encode(claims, self.signing_key, algorithm="RS256", headers={"kid": "k1"})}

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture
def provider(settings):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    p = Provider(port)
    settings.public_url = "http://testserver"
    settings.oidc_issuer, settings.oidc_client_id, settings.oidc_client_secret = p.issuer, "radfetch", "s3cret"
    settings.oidc_role_map = json.dumps({"radiology": "viewer", "ai-team": "operator", "radfetch-admins": "admin"})
    yield p
    p.stop()


@pytest.fixture
def client(conn, settings, provider):
    with TestClient(create_app(settings)) as c:
        yield c


def sso(client, tamper=None):
    """Walk the browser's path: RadFetch -> provider -> back to RadFetch. Returns the final redirect target."""
    to_provider = client.get("/auth/oidc/login", follow_redirects=False)
    assert to_provider.status_code == 302
    back = httpx.get(to_provider.headers["location"], follow_redirects=False).headers["location"]
    url = urlsplit(back)
    assert (url.scheme, url.netloc, url.path) == ("http", "testserver", "/auth/oidc/callback")
    query = url.query if tamper is None else tamper(parse_qs(url.query))
    done = client.get(f"/auth/oidc/callback?{query}", follow_redirects=False)
    assert done.status_code == 303
    return done.headers["location"]


def test_first_sign_in_creates_the_account_with_the_mapped_role(conn, client, provider):
    config = client.get("/auth/config").json()
    assert (config["sso"], config["sso_label"], config["password_login"]) == (True, "Sign in with single sign-on", True)
    assert "not_authorized" in config["messages"]
    assert sso(client) == "/"
    me = client.get("/whoami").json()
    assert (me["name"], me["role"], me["sso"]) == ("anna", "viewer", True)
    user = conn.execute("select * from users where username = 'anna'").fetchone()
    assert user["sso"] and user["password_hash"] is None and user["oidc_subject"] == provider.issuer + "|u-1"
    request = provider.authorize_requests[0]
    assert request["scope"] == "openid profile email" and request["redirect_uri"] == "http://testserver/auth/oidc/callback"

    r = client.post("/auth/logout").json()
    assert r["sso_logout_url"].startswith(provider.issuer + "/logout?client_id=radfetch")
    assert client.get("/whoami").status_code == 401


def test_role_follows_the_groups_at_every_sign_in(conn, client, provider):
    sso(client)
    # the administrator's individual settings must survive role changes
    conn.execute("update users set denies = '{identifiers.view}', allowed_flows = '{seg}' where username = 'anna'")
    provider.identity["groups"] = ["radiology", "ai-team", "something-else"]
    sso(client)
    me = client.get("/whoami").json()
    assert me["role"] == "operator"  # the highest of the mapped roles
    assert "identifiers.view" not in me["permissions"] and me["allowed_flows"] == ["seg"]
    assert conn.execute("select count(*) as n from users").fetchone()["n"] == 1  # same account, not a second one


def test_same_person_is_recognised_after_a_username_change(conn, client, provider):
    sso(client)
    provider.identity["preferred_username"] = "anna.jensen"
    sso(client)
    assert client.get("/whoami").json()["name"] == "anna"  # matched on the provider's subject
    assert conn.execute("select count(*) as n from users").fetchone()["n"] == 1


def test_people_without_a_mapped_group_are_refused_unless_given_access(conn, settings, client, provider):
    provider.identity = {"sub": "u-2", "preferred_username": "bo", "groups": ["catering"]}
    assert sso(client) == "/?sso_error=not_authorized"
    assert client.get("/whoami").status_code == 401 and not store.users_exist(conn)

    # an administrator creates an account for single sign-on beforehand: it is linked at first sign-in
    store.create_user(conn, "bo", "submitter", sso=True)
    assert sso(client) == "/"
    assert client.get("/whoami").json()["role"] == "submitter"
    assert "sso_refused" in {e["kind"] for e in store.audit_log(conn)}


def test_default_role_admits_everyone_the_provider_knows(conn, settings, provider):
    settings.oidc_default_role = "monitor"
    provider.identity = {"sub": "u-3", "preferred_username": "cy@hospital.example", "groups": []}
    with TestClient(create_app(settings)) as client:
        assert sso(client) == "/"
        assert client.get("/whoami").json()["role"] == "monitor"


def test_password_and_service_accounts_are_never_taken_over(conn, client, provider):
    store.create_user(conn, "anna", "admin", auth.hash_password("correct horse battery"))
    assert sso(client) == "/?sso_error=username_taken"
    assert client.get("/whoami").status_code == 401
    assert conn.execute("select oidc_subject from users where username = 'anna'").fetchone()["oidc_subject"] is None


def test_disabled_account_is_refused(conn, client, provider):
    sso(client)
    client.post("/auth/logout")
    conn.execute("update users set disabled = true")
    assert sso(client) == "/?sso_error=disabled"


def test_a_sign_in_attempt_works_once_and_expires(conn, client, provider):
    to_provider = client.get("/auth/oidc/login", follow_redirects=False).headers["location"]
    back = urlsplit(httpx.get(to_provider, follow_redirects=False).headers["location"])
    assert client.get(f"/auth/oidc/callback?{back.query}", follow_redirects=False).headers["location"] == "/"
    client.cookies.clear()
    again = client.get(f"/auth/oidc/callback?{back.query}", follow_redirects=False)
    assert again.headers["location"] == "/?sso_error=state"  # replaying the provider's answer does nothing

    def unknown_state(q):
        return f"code={q['code'][0]}&state=made-up"

    assert sso(client, unknown_state) == "/?sso_error=state"
    to_provider = client.get("/auth/oidc/login", follow_redirects=False).headers["location"]
    back = urlsplit(httpx.get(to_provider, follow_redirects=False).headers["location"])
    conn.execute("update oidc_logins set created_at = now() - interval '11 minutes'")
    late = client.get(f"/auth/oidc/callback?{back.query}", follow_redirects=False)
    assert late.headers["location"] == "/?sso_error=state"
    refused = client.get("/auth/oidc/callback?error=access_denied", follow_redirects=False)
    assert refused.headers["location"] == "/?sso_error=denied"


@pytest.mark.parametrize("break_it", ["wrong_key", "wrong_audience", "wrong_nonce"])
def test_tokens_that_do_not_verify_are_refused(conn, client, provider, break_it):
    if break_it == "wrong_key":
        provider.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif break_it == "wrong_audience":
        provider.audience = "another-application"
    else:
        provider.nonce_override = "not-the-nonce-we-sent"
    assert sso(client) == "/?sso_error=token"
    assert client.get("/whoami").status_code == 401 and not store.users_exist(conn)


def test_second_factor_at_the_provider_can_be_required(conn, settings, provider):
    settings.oidc_require_amr, settings.oidc_acr_values = "mfa", "urn:example:mfa"
    with TestClient(create_app(settings)) as client:
        assert sso(client) == "/"
        assert provider.authorize_requests[-1]["acr_values"] == "urn:example:mfa"
        client.post("/auth/logout")
        provider.identity["amr"] = ["pwd"]  # signed in with a password only
        assert sso(client) == "/?sso_error=mfa"
        assert client.get("/whoami").status_code == 401


def test_password_sign_in_can_be_turned_off_and_mandatory_mfa_spares_sso(conn, settings, provider):
    settings.password_login, settings.require_mfa = False, True
    store.create_user(conn, "local", "admin", auth.hash_password("correct horse battery"))
    with TestClient(create_app(settings)) as client:
        assert client.get("/auth/config").json()["password_login"] is False
        r = client.post("/auth/login", json={"username": "local", "password": "correct horse battery"})
        assert r.status_code == 403
        assert sso(client) == "/"
        assert client.get("/jobs").status_code == 200  # no RadFetch second factor on top of the provider's


def test_misconfiguration_is_caught_at_start(settings, provider):
    settings.public_url = ""
    with pytest.raises(ValueError, match="RADFETCH_PUBLIC_URL"):
        create_app(settings)
    settings.public_url, settings.oidc_role_map = "http://testserver", '{"g": "emperor"}'
    with pytest.raises(ValueError, match="unknown role"):
        create_app(settings)


def test_the_only_administrator_is_not_demoted_by_a_group_change(conn, client, provider):
    provider.identity["groups"] = ["radfetch-admins"]
    sso(client)
    assert client.get("/whoami").json()["role"] == "admin"
    provider.identity["groups"] = ["radiology"]  # removed from the admin group in the directory
    sso(client)
    assert client.get("/whoami").json()["role"] == "admin"  # kept: nobody else could manage users
    store.create_user(conn, "ada", "admin", auth.hash_password("correct horse battery"))
    sso(client)
    assert client.get("/whoami").json()["role"] == "viewer"  # with another administrator, the change applies


def test_api_is_not_open_before_the_first_sign_in(conn, client):
    assert not store.users_exist(conn)
    assert client.get("/jobs").status_code == 401 and client.get("/whoami").status_code == 401
