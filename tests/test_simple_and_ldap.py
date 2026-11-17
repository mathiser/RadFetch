"""Simple sign-in (one shared password) and LDAP sign-in against an in-memory directory."""

from __future__ import annotations

import time

import ldap3
import pytest
from fastapi.testclient import TestClient
from ldap3.core.exceptions import LDAPSocketOpenError

from radfetch import auth, store
from radfetch.api import create_app
from radfetch.ldap_auth import _BindRefused

# --- simple sign-in -----------------------------------------------------------------------


def test_simple_sign_in_needs_only_the_password(conn, settings):
    settings.simple_password = "a-shared-password"
    with TestClient(create_app(settings)) as client:
        config = client.get("/auth/config").json()
        assert config["simple"] is True and config["providers"] == []
        assert client.get("/jobs").status_code == 401  # not open, although nobody created an account
        assert client.post("/auth/login", json={"password": "not-the-password"}).status_code == 401
        assert client.post("/auth/login", json={"password": "a-shared-password"}).status_code == 200
        me = client.get("/whoami").json()
        assert (me["name"], me["role"]) == ("admin", "admin")
        assert client.post("/jobs", json={"accession_number": "A", "flow": "seg"}).status_code == 201


def test_changing_the_configured_password_takes_effect_at_restart(conn, settings):
    settings.simple_password = "the-first-password"
    with TestClient(create_app(settings)):
        pass
    settings.simple_password = "the-second-password"
    with TestClient(create_app(settings)) as client:
        assert client.post("/auth/login", json={"password": "the-first-password"}).status_code == 401
        assert client.post("/auth/login", json={"password": "the-second-password"}).status_code == 200
    assert conn.execute("select count(*) as n from users").fetchone()["n"] == 1


def test_a_restart_lifts_a_lock_out(conn, settings):
    settings.simple_password = "a-shared-password"
    with TestClient(create_app(settings)) as client:
        for _ in range(10):
            client.post("/auth/login", json={"password": "wrong-password"})
        assert client.post("/auth/login", json={"password": "a-shared-password"}).status_code == 401
    with TestClient(create_app(settings)) as client:
        assert client.post("/auth/login", json={"password": "a-shared-password"}).status_code == 200


def test_simple_password_must_not_be_trivial(settings):
    settings.simple_password = "short"
    with pytest.raises(ValueError, match="at least 10"):
        create_app(settings)


def test_other_accounts_still_work_next_to_simple_sign_in(conn, settings):
    settings.simple_password = "a-shared-password"
    with TestClient(create_app(settings)) as client:
        client.post("/auth/login", json={"password": "a-shared-password"})
        made = client.post("/users", json={"username": "sue", "role": "submitter"}).json()
        client.post("/auth/logout")
        r = client.post("/auth/login", json={"username": "sue", "password": made["generated_password"]})
        assert r.status_code == 200 and r.json()["role"] == "submitter"


# --- LDAP -----------------------------------------------------------------------------------

BASE = "dc=hospital,dc=example"
DIRECTORY = {
    f"cn=svc-radfetch,{BASE}": {"userPassword": "service-password", "objectClass": "person"},
    f"cn=Anna Jensen,ou=people,{BASE}": {
        "userPassword": "anna-password", "sAMAccountName": "anna", "uid": "anna", "objectClass": "person",
        "memberOf": [f"cn=RadFetch-Admins,ou=groups,{BASE}", f"cn=radiology,ou=groups,{BASE}"],
    },
    f"cn=Bo Berg,ou=people,{BASE}": {
        "userPassword": "bo-password", "sAMAccountName": "bo", "uid": "bo", "objectClass": "person",
        "memberOf": [f"cn=radiology,ou=groups,{BASE}"],
    },
    f"cn=Cy Dahl,ou=people,{BASE}": {
        "userPassword": "cy-password", "sAMAccountName": "cy", "uid": "cy", "objectClass": "person", "memberOf": [],
    },
    f"uid=dee,ou=people,{BASE}": {"userPassword": "dee-password", "uid": "dee", "objectClass": "person",
                                   "memberOf": [f"cn=radiology,ou=groups,{BASE}"]},
    # an OpenLDAP-style user without memberOf, whose group lists them as a member instead
    f"uid=eve,ou=people,{BASE}": {"userPassword": "eve-password", "uid": "eve", "sAMAccountName": "eve",
                                   "objectClass": "person"},
    f"cn=RadFetch-Admins,ou=groups,{BASE}": {"cn": "RadFetch-Admins", "objectClass": "groupOfNames",
                                              "member": [f"uid=eve,ou=people,{BASE}"]},
}


class FakeDirectory:
    def __init__(self):
        self.binds: list[str] = []
        self.down = False

    def connect(self, user, password):
        if self.down:
            raise LDAPSocketOpenError("directory unreachable")
        self.binds.append(user)
        conn = ldap3.Connection(ldap3.Server("fake"), user=user, password=password, client_strategy=ldap3.MOCK_SYNC)
        for dn, attrs in DIRECTORY.items():
            conn.strategy.add_entry(dn, attrs)
        if not conn.bind():
            raise _BindRefused("invalidCredentials")
        return conn


@pytest.fixture
def directory(settings):
    settings.ldap_url = "ldaps://dc1.hospital.example"
    settings.ldap_bind_dn, settings.ldap_bind_password = f"cn=svc-radfetch,{BASE}", "service-password"
    settings.ldap_user_base = BASE
    settings.group_role_map = '{"radfetch-admins": "admin", "cn=radiology,ou=groups,dc=hospital,dc=example": "viewer"}'
    return FakeDirectory()


@pytest.fixture
def client(conn, settings, directory):
    with TestClient(create_app(settings, ldap_connect=directory.connect)) as c:
        yield c


def login(client, username, password, code=None):
    return client.post("/auth/login", json={"username": username, "password": password} | ({"code": code} if code else {}))


def test_directory_account_signs_in_with_role_from_its_groups(conn, client, directory):
    assert client.get("/auth/config").json()["password_login"] is True
    r = login(client, "ANNA", "anna-password")  # typed in capitals; the directory's spelling is used
    assert r.status_code == 200
    me = client.get("/whoami").json()
    assert (me["name"], me["role"], me["has_password"]) == ("anna", "admin", False)  # group matched by CN, any case
    row = conn.execute("select * from users where username = 'anna'").fetchone()
    assert row["password_hash"] is None and row["oidc_subject"].startswith("ldap:")  # no password stored
    client.post("/auth/logout")
    assert login(client, "bo", "bo-password").json()["role"] == "viewer"  # group matched by full DN
    assert conn.execute("select count(*) as n from users").fetchone()["n"] == 2


def test_wrong_password_unknown_user_and_tricks_are_refused(conn, client, directory):
    assert login(client, "anna", "wrong-password").status_code == 401
    assert login(client, "nobody", "whatever-password").status_code == 401
    assert login(client, "anna", "").status_code == 401  # an empty password must never reach the directory
    assert login(client, "*", "anna-password").status_code == 401  # a wildcard is searched for literally
    assert login(client, "anna)(uid=*", "anna-password").status_code == 401
    assert client.get("/whoami").status_code == 401 and not store.users_exist(conn)


def test_person_without_a_mapped_group_is_refused_with_a_reason(conn, client):
    r = login(client, "cy", "cy-password")
    assert r.status_code == 403 and "not been given access" in r.json()["detail"]


def test_directory_outage_is_not_reported_as_a_wrong_password(conn, client, directory):
    directory.down = True
    r = login(client, "anna", "anna-password")
    assert r.status_code == 503 and "could not be reached" in r.json()["detail"]


def test_radfetch_password_accounts_never_go_to_the_directory(conn, client, directory):
    store.create_user(conn, "local", "admin", auth.hash_password("correct horse battery"))
    assert login(client, "local", "correct horse battery").status_code == 200
    assert login(client, "local", "wrong-password").status_code == 401
    assert directory.binds == []  # the break-glass administrator works even if the directory is down
    directory.down = True
    assert login(client, "local", "correct horse battery").status_code == 200


def test_repeated_wrong_passwords_lock_sign_in_here_too(conn, client):
    login(client, "anna", "anna-password")
    client.post("/auth/logout")
    for _ in range(10):
        login(client, "anna", "wrong-password")
    assert login(client, "anna", "anna-password").status_code == 401


def test_two_step_sign_in_works_on_top_of_the_directory_password(conn, settings, directory):
    settings.require_mfa = True
    with TestClient(create_app(settings, ldap_connect=directory.connect)) as client:
        assert login(client, "bo", "bo-password").json()["mfa_setup_required"] is True
        assert client.get("/jobs").status_code == 403
        client.post("/me/mfa/setup")
        secret = conn.execute("select totp_secret from users where username = 'bo'").fetchone()["totp_secret"]
        step = int(time.time() // 30)
        client.post("/me/mfa/confirm", json={"code": auth.totp_code(secret, step)})
        assert client.get("/jobs").status_code == 200
        client.post("/auth/logout")
        assert login(client, "bo", "bo-password").json()["mfa_required"] is True
        assert login(client, "bo", "bo-password", auth.totp_code(secret, step + 1)).status_code == 200


def test_sign_in_without_a_service_account(conn, settings, directory):
    settings.ldap_bind_dn, settings.ldap_bind_password, settings.ldap_user_base = "", None, ""
    settings.ldap_user_dn_template = "uid={username},ou=people," + BASE
    settings.ldap_username_attribute = "uid"
    with TestClient(create_app(settings, ldap_connect=directory.connect)) as client:
        assert login(client, "dee", "wrong-password").status_code == 401
        assert login(client, "dee", "dee-password").json()["role"] == "viewer"
        assert login(client, "dee,ou=people", "dee-password").status_code == 401  # cannot alter the DN


@pytest.mark.parametrize("change,message", [
    ({"ldap_url": "ldap://dc1.hospital.example"}, "not encrypted"),
    ({"ldap_user_base": ""}, "RADFETCH_LDAP_USER_BASE"),
    ({"ldap_user_filter": "(uid=x)"}, "must contain"),
    ({"ldap_bind_dn": ""}, "either a service account"),
])
def test_unsafe_or_incomplete_ldap_settings_stop_at_start(settings, directory, change, message):
    for key, value in change.items():
        setattr(settings, key, value)
    with pytest.raises(ValueError, match=message):
        create_app(settings)


def test_start_tls_or_an_explicit_override_allows_plain_ldap_urls(settings, directory):
    settings.ldap_url, settings.ldap_start_tls = "ldap://dc1.hospital.example", True
    create_app(settings)
    settings.ldap_start_tls, settings.ldap_allow_insecure = False, True
    create_app(settings)


def test_groups_can_be_searched_for_directories_without_memberof(conn, settings, directory):
    with TestClient(create_app(settings, ldap_connect=directory.connect)) as client:
        assert login(client, "eve", "eve-password").status_code == 403  # no memberOf: no mapped group
    settings.ldap_group_base = "ou=groups," + BASE
    with TestClient(create_app(settings, ldap_connect=directory.connect)) as client:
        assert login(client, "eve", "eve-password").json()["role"] == "admin"
