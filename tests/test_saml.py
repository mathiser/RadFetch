"""SAML 2.0 sign-in against an identity provider simulated in the test, including attacks on it."""

from __future__ import annotations

import base64
import datetime as dt
import zlib
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from lxml import etree
from signxml import CanonicalizationMethod, XMLSigner

from radfetch import store
from radfetch.api import create_app

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}
IDP = "https://idp.hospital.example/adfs/services/trust"
SSO_URL = "https://idp.hospital.example/adfs/ls/"
SP = "http://testserver/auth/saml/metadata"
ACS = "http://testserver/auth/saml/acs"


def keypair(name="idp"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30)).sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


def der_b64(cert_pem: str) -> str:
    return "".join(line for line in cert_pem.splitlines() if "CERTIFICATE" not in line)


class Idp:
    """Builds and signs the answers an identity provider would post back."""

    def __init__(self):
        self.key, self.cert = keypair()

    def metadata(self) -> str:
        return (
            f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            f'xmlns:ds="http://www.w3.org/2000/09/xmldsig#" entityID="{IDP}">'
            '<md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            f'<md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data><ds:X509Certificate>{der_b64(self.cert)}'
            "</ds:X509Certificate></ds:X509Data></ds:KeyInfo></md:KeyDescriptor>"
            '<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
            f'Location="{SSO_URL}"/></md:IDPSSODescriptor></md:EntityDescriptor>'
        )

    def assertion(self, request_id, *, assertion_id="_a1", name_id="anna", groups=("radiology",), audience=SP,
                  recipient=ACS, issuer=IDP, lifetime=300, context="urn:oasis:names:tc:SAML:2.0:ac:classes:Password",
                  in_response_to=None) -> etree._Element:
        now = dt.datetime.now(dt.UTC)
        t = lambda d: (now + dt.timedelta(seconds=d)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        values = "".join(f"<saml:AttributeValue>{g}</saml:AttributeValue>" for g in groups)
        irt = in_response_to or request_id
        return etree.fromstring(
            f'<saml:Assertion xmlns:saml="{NS["saml"]}" ID="{assertion_id}" Version="2.0" IssueInstant="{t(0)}">'
            f"<saml:Issuer>{issuer}</saml:Issuer>"
            f"<saml:Subject><saml:NameID>{name_id}</saml:NameID>"
            '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
            f'<saml:SubjectConfirmationData InResponseTo="{irt}" Recipient="{recipient}" NotOnOrAfter="{t(lifetime)}"/>'
            "</saml:SubjectConfirmation></saml:Subject>"
            f'<saml:Conditions NotBefore="{t(-60)}" NotOnOrAfter="{t(lifetime)}">'
            f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>"
            "</saml:Conditions>"
            f'<saml:AuthnStatement AuthnInstant="{t(0)}"><saml:AuthnContext>'
            f"<saml:AuthnContextClassRef>{context}</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
            f'<saml:AttributeStatement><saml:Attribute Name="groups">{values}</saml:Attribute></saml:AttributeStatement>'
            "</saml:Assertion>"
        )

    def sign(self, element: etree._Element, key=None, cert=None) -> etree._Element:
        signer = XMLSigner(c14n_algorithm=CanonicalizationMethod.EXCLUSIVE_XML_CANONICALIZATION_1_0)
        return signer.sign(element, key=key or self.key, cert=cert or self.cert, reference_uri="#" + element.get("ID"))

    @staticmethod
    def response(request_id, *assertions, status="urn:oasis:names:tc:SAML:2.0:status:Success",
                 response_id="_r1") -> etree._Element:
        root = etree.fromstring(
            f'<samlp:Response xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}" ID="{response_id}" Version="2.0" '
            f'IssueInstant="2026-01-01T00:00:00Z" Destination="{ACS}" InResponseTo="{request_id}">'
            f'<saml:Issuer>{IDP}</saml:Issuer><samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>'
            "</samlp:Response>"
        )
        for a in assertions:
            root.append(a)
        return root


@pytest.fixture
def idp(settings, tmp_path):
    provider = Idp()
    (tmp_path / "idp.xml").write_text(provider.metadata())
    settings.public_url = "http://testserver"
    settings.saml_idp_metadata = str(tmp_path / "idp.xml")
    settings.group_role_map = '{"radiology": "viewer", "radfetch-admins": "admin"}'
    return provider


@pytest.fixture
def client(conn, settings, idp):
    with TestClient(create_app(settings)) as c:
        yield c


def start(client) -> tuple[str, str]:
    """Begin a sign-in: returns (the request's ID, RelayState), checking the request on the way."""
    r = client.get("/auth/saml/login", follow_redirects=False)
    assert r.status_code == 302
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == SSO_URL
    query = parse_qs(url.query)
    request = etree.fromstring(zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15))
    assert request.get("AssertionConsumerServiceURL") == ACS and request.get("Destination") == SSO_URL
    assert request.findtext("saml:Issuer", namespaces=NS) == SP
    return request.get("ID"), query["RelayState"][0]


def post(client, response: etree._Element | bytes, relay: str) -> str:
    body = response if isinstance(response, bytes) else etree.tostring(response)
    r = client.post("/auth/saml/acs", data={"SAMLResponse": base64.b64encode(body).decode(), "RelayState": relay},
                    follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def test_signed_assertion_signs_in_with_role_from_groups(conn, client, idp):
    config = client.get("/auth/config").json()
    assert {"label": "Sign in with single sign-on", "url": "/auth/saml/login"} in config["providers"]
    metadata = client.get("/auth/saml/metadata")
    assert metadata.status_code == 200 and f'entityID="{SP}"' in metadata.text and ACS in metadata.text

    request_id, relay = start(client)
    assert post(client, idp.response(request_id, idp.sign(idp.assertion(request_id))), relay) == "/"
    me = client.get("/whoami").json()
    assert (me["name"], me["role"], me["sso"]) == ("anna", "viewer", True)
    row = conn.execute("select * from users where username = 'anna'").fetchone()
    assert row["sso"] and row["password_hash"] is None and row["oidc_subject"] == f"saml:{IDP}|anna"


def test_signature_on_the_whole_response_is_accepted_too(conn, client, idp):
    request_id, relay = start(client)
    signed = idp.sign(idp.response(request_id, idp.assertion(request_id, groups=["radfetch-admins"])))
    assert post(client, signed, relay) == "/"
    assert client.get("/whoami").json()["role"] == "admin"


def test_unsigned_or_wrongly_signed_answers_are_refused(conn, client, idp):
    request_id, relay = start(client)
    assert post(client, idp.response(request_id, idp.assertion(request_id)), relay) == "/?sso_error=token"

    other_key, other_cert = keypair("attacker")
    request_id, relay = start(client)
    forged = idp.sign(idp.assertion(request_id, groups=["radfetch-admins"]), key=other_key, cert=other_cert)
    assert post(client, idp.response(request_id, forged), relay) == "/?sso_error=token"
    assert client.get("/whoami").status_code == 401 and not store.users_exist(conn)


def test_content_changed_after_signing_is_refused(conn, client, idp):
    request_id, relay = start(client)
    signed = idp.sign(idp.assertion(request_id))
    signed.find(".//saml:AttributeValue", NS).text = "radfetch-admins"  # promote ourselves
    assert post(client, idp.response(request_id, signed), relay) == "/?sso_error=token"


def test_signature_wrapping_attacks_are_refused(conn, client, idp):
    # 1. The genuine signed assertion is hidden in an extension; a forged one is put where it is read.
    request_id, relay = start(client)
    genuine = idp.sign(idp.assertion(request_id, name_id="bo"))
    forged = idp.assertion(request_id, assertion_id="_evil", name_id="anna", groups=["radfetch-admins"])
    response = idp.response(request_id, forged)
    ext = etree.SubElement(response, f"{{{NS['samlp']}}}Extensions")
    ext.append(genuine)
    assert post(client, response, relay) == "/?sso_error=token"

    # 2. The forged assertion carries the genuine signature, with the genuine assertion (minus its own
    #    signature, so its digest still matches) tucked into the signature. The signature then verifies,
    #    but over the genuine assertion, not the forged one that would be read.
    request_id, relay = start(client)
    genuine = idp.sign(idp.assertion(request_id, name_id="bo"))
    forged = idp.assertion(request_id, assertion_id="_evil", name_id="anna", groups=["radfetch-admins"])
    signature = genuine.find("ds:Signature", NS)
    genuine.remove(signature)
    forged.insert(1, signature)
    etree.SubElement(signature, f"{{{NS['ds']}}}Object").append(genuine)  # a schema-valid spot
    assert post(client, idp.response(request_id, forged), relay) == "/?sso_error=token"

    # 3. Same, but the forged assertion takes the genuine one's ID, so the reference is ambiguous.
    request_id, relay = start(client)
    genuine = idp.sign(idp.assertion(request_id, name_id="bo"))
    forged = idp.assertion(request_id, name_id="anna", groups=["radfetch-admins"])  # same ID "_a1"
    signature = genuine.find("ds:Signature", NS)
    genuine.remove(signature)
    forged.insert(1, signature)
    etree.SubElement(signature, f"{{{NS['ds']}}}Object").append(genuine)
    assert post(client, idp.response(request_id, forged), relay) == "/?sso_error=token"

    # 4. Two assertions, one signed.
    request_id, relay = start(client)
    two = idp.response(request_id, idp.sign(idp.assertion(request_id, name_id="bo")),
                       idp.assertion(request_id, assertion_id="_evil", groups=["radfetch-admins"]))
    assert post(client, two, relay) == "/?sso_error=token"
    assert client.get("/whoami").status_code == 401 and not store.users_exist(conn)


@pytest.mark.parametrize("change", [
    {"audience": "https://another-application.example"},
    {"recipient": "https://another-application.example/acs"},
    {"issuer": "https://someone-else.example"},
    {"lifetime": -600},
    {"in_response_to": "_a-request-we-never-made"},
])
def test_assertions_meant_for_something_else_are_refused(conn, client, idp, change):
    request_id, relay = start(client)
    signed = idp.sign(idp.assertion(request_id, **change))
    assert post(client, idp.response(request_id, signed), relay) == "/?sso_error=token"


def test_an_answer_works_once_and_only_for_its_own_attempt(conn, client, idp):
    request_id, relay = start(client)
    answer = idp.response(request_id, idp.sign(idp.assertion(request_id)))
    assert post(client, answer, relay) == "/"
    client.post("/auth/logout")
    assert post(client, answer, relay) == "/?sso_error=state"  # replayed
    assert post(client, answer, "made-up") == "/?sso_error=state"
    # an answer to one attempt cannot be used to finish another
    _, other_relay = start(client)
    assert post(client, answer, other_relay) == "/?sso_error=token"


def test_refusal_and_unusable_input(conn, client, idp):
    request_id, relay = start(client)
    denied = idp.response(request_id, status="urn:oasis:names:tc:SAML:2.0:status:Requester")
    assert post(client, denied, relay) == "/?sso_error=denied"

    request_id, relay = start(client)
    xxe = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
           b'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">&x;</samlp:Response>')
    assert post(client, xxe, relay) == "/?sso_error=token"

    request_id, relay = start(client)
    encrypted = idp.response(request_id)
    etree.SubElement(encrypted, f"{{{NS['saml']}}}EncryptedAssertion")
    assert post(client, encrypted, relay) == "/?sso_error=token"

    _, relay = start(client)
    assert post(client, b"this is not xml", relay) == "/?sso_error=token"


def test_second_factor_context_can_be_required(conn, settings, idp):
    settings.saml_require_authn_context = "http://schemas.microsoft.com/claims/multipleauthn"
    with TestClient(create_app(settings)) as client:
        request_id, relay = start(client)
        assert post(client, idp.response(request_id, idp.sign(idp.assertion(request_id))), relay) == "/?sso_error=mfa"
        request_id, relay = start(client)
        mfa = idp.assertion(request_id, context="http://schemas.microsoft.com/claims/multipleauthn")
        assert post(client, idp.response(request_id, idp.sign(mfa)), relay) == "/"


def test_username_from_an_attribute_and_people_without_access(conn, settings, idp):
    settings.saml_username_attribute = "groups"  # any attribute works; groups is the one the test idp sends
    with TestClient(create_app(settings)) as client:
        request_id, relay = start(client)
        nobody = idp.assertion(request_id, groups=["catering"])
        assert post(client, idp.response(request_id, idp.sign(nobody)), relay) == "/?sso_error=not_authorized"
        request_id, relay = start(client)
        assert post(client, idp.response(request_id, idp.sign(idp.assertion(request_id))), relay) == "/"
        assert client.get("/whoami").json()["name"] == "radiology"


def test_metadata_needs_a_public_url(settings, idp):
    settings.public_url = ""
    with pytest.raises(ValueError, match="RADFETCH_PUBLIC_URL"):
        create_app(settings)


def test_over_https_a_sign_in_cannot_be_finished_in_another_browser(conn, settings, idp):
    """Login CSRF over HTTPS, where the browser can carry the attempt's cookie back on the POST."""
    settings.public_url = "https://radfetch.example"
    sp, acs = "https://radfetch.example/auth/saml/metadata", "https://radfetch.example/auth/saml/acs"
    app = create_app(settings)
    with TestClient(app, base_url="https://radfetch.example") as attacker, \
            TestClient(app, base_url="https://radfetch.example") as victim:
        r = attacker.get("/auth/saml/login", follow_redirects=False)
        assert "samesite=none" in r.headers["set-cookie"].lower() and "secure" in r.headers["set-cookie"].lower()
        query = parse_qs(urlsplit(r.headers["location"]).query)
        request_id = etree.fromstring(zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15)).get("ID")
        answer = idp.response(request_id, idp.sign(idp.assertion(request_id, audience=sp, recipient=acs)))
        form = {"SAMLResponse": base64.b64encode(etree.tostring(answer)).decode(), "RelayState": query["RelayState"][0]}
        refused = victim.post("/auth/saml/acs", data=form, follow_redirects=False)
        assert refused.headers["location"] == "/?sso_error=state" and victim.get("/whoami").status_code == 401
        accepted = attacker.post("/auth/saml/acs", data=form, follow_redirects=False)
        assert accepted.headers["location"] == "/" and attacker.get("/whoami").json()["name"] == "anna"
