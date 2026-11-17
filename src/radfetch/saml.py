"""Single sign-on with SAML 2.0 (service-provider-initiated, as used by ADFS and many others).

RadFetch sends the browser to the identity provider with an authentication request
(HTTP-Redirect binding). The provider posts a signed response back to
/auth/saml/acs (HTTP-POST binding).

What is trusted is only what the XML signature covers: the response is verified
against the provider's certificate, and everything is then read from the element
the verifier returns as signed, never from the document as submitted. That is the
defence against signature-wrapping attacks.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import secrets
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx
from lxml import etree
from signxml import SignatureConfiguration, SignatureMethod, XMLVerifier

from radfetch import directory, store
from radfetch.config import Settings
from radfetch.directory import SignInError

log = logging.getLogger("radfetch.saml")

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "md": "urn:oasis:names:tc:SAML:2.0:metadata",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}
REDIRECT = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
SKEW = dt.timedelta(seconds=120)

# Public-key signatures only: an HMAC "signature" would prove nothing about who signed.
_ASYMMETRIC = frozenset(m for m in SignatureMethod if not m.name.startswith("HMAC") and "SHA1" not in m.name)


def _parser() -> etree.XMLParser:
    # No entity expansion, no DTD loading, no network: the input comes from a browser.
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False)


def _parse(data: bytes) -> etree._Element:
    root = etree.fromstring(data, _parser())
    if root.getroottree().docinfo.doctype:
        raise SignInError("token", "the response contains a DOCTYPE")
    return root


@dataclass(frozen=True)
class IdpMetadata:
    entity_id: str
    sso_url: str
    certificates: list[str]  # base64 DER, as in the metadata


class Saml:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.s = settings
        self._http = http or httpx.Client(timeout=15)
        self._idp: IdpMetadata | None = None
        self._idp_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.s.saml_idp_metadata)

    @property
    def entity_id(self) -> str:
        return self.s.saml_entity_id or self.s.public_url.rstrip("/") + "/auth/saml/metadata"

    @property
    def acs_url(self) -> str:
        return self.s.public_url.rstrip("/") + "/auth/saml/acs"

    def check_config(self) -> None:
        if self.enabled and not self.s.public_url:
            raise ValueError("RADFETCH_PUBLIC_URL must be set for SAML: the provider posts its answer back to it")

    # --- metadata ----------------------------------------------------------------------

    def idp(self) -> IdpMetadata:
        """The provider's entity id, sign-in address and signing certificates, re-read hourly so
        that a certificate renewal at the provider is picked up without a restart."""
        if self._idp is None or time.monotonic() - self._idp_at > 3600:
            source = self.s.saml_idp_metadata
            try:
                if source.lower().startswith(("http://", "https://")):
                    r = self._http.get(source)
                    r.raise_for_status()
                    data = r.content
                else:
                    data = Path(source).read_bytes()
                root = _parse(data)
            except (httpx.HTTPError, OSError, etree.XMLSyntaxError) as exc:
                if self._idp is not None:  # keep working on what we have
                    log.warning("could not refresh the SAML metadata: %s", exc)
                    self._idp_at = time.monotonic()
                    return self._idp
                raise SignInError("provider", f"cannot read the SAML metadata: {exc}") from exc
            entity = root if root.tag == f"{{{NS['md']}}}EntityDescriptor" else root.find(".//md:EntityDescriptor", NS)
            sso = entity.find("md:IDPSSODescriptor", NS) if entity is not None else None
            if sso is None:
                raise SignInError("provider", "the SAML metadata describes no identity provider")
            location = next(
                (e.get("Location") for e in sso.findall("md:SingleSignOnService", NS) if e.get("Binding") == REDIRECT),
                None,
            )
            certs = [
                "".join((c.text or "").split())
                for kd in sso.findall("md:KeyDescriptor", NS)
                if kd.get("use") in (None, "signing")
                for c in kd.findall(".//ds:X509Certificate", NS)
            ]
            if not location or not certs:
                raise SignInError("provider", "the SAML metadata lacks a redirect sign-in address or a signing certificate")
            self._idp, self._idp_at = IdpMetadata(entity.get("entityID"), location, certs), time.monotonic()
        return self._idp

    def sp_metadata(self) -> str:
        """What to give the identity provider's administrator to register RadFetch."""
        return (
            '<?xml version="1.0"?>\n'
            f'<md:EntityDescriptor xmlns:md="{NS["md"]}" entityID="{_attr(self.entity_id)}">'
            '<md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" '
            'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            "<md:NameIDFormat>urn:oasis:names:tc:SAML:2.0:nameid-format:persistent</md:NameIDFormat>"
            f'<md:AssertionConsumerService Binding="{POST}" Location="{_attr(self.acs_url)}" index="0" isDefault="true"/>'
            "</md:SPSSODescriptor></md:EntityDescriptor>\n"
        )

    # --- the two halves of a sign-in -------------------------------------------------------

    def begin(self, conn: store.Conn) -> str:
        if not self.enabled:
            raise SignInError("not_configured")
        idp = self.idp()
        request_id = "_" + secrets.token_hex(20)
        relay = secrets.token_urlsafe(24)
        store.save_oidc_login(conn, relay, request_id, "saml")  # same single-use store as OpenID Connect
        issued = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        xml = (
            f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}" ID="{request_id}" '
            f'Version="2.0" IssueInstant="{issued}" Destination="{_attr(idp.sso_url)}" '
            f'ProtocolBinding="{POST}" AssertionConsumerServiceURL="{_attr(self.acs_url)}">'
            f"<saml:Issuer>{_text(self.entity_id)}</saml:Issuer>"
            '<samlp:NameIDPolicy AllowCreate="true"/></samlp:AuthnRequest>'
        )
        deflated = zlib.compress(xml.encode())[2:-4]  # raw DEFLATE, as the redirect binding requires
        query = urlencode({"SAMLRequest": base64.b64encode(deflated).decode(), "RelayState": relay})
        return idp.sso_url + ("&" if "?" in idp.sso_url else "?") + query

    def complete(self, conn: store.Conn, saml_response: str, relay_state: str) -> tuple[str, str, list[str]]:
        """Verify the provider's answer. Returns (subject, username, groups)."""
        if not self.enabled:
            raise SignInError("not_configured")
        pending = store.take_oidc_login(conn, relay_state or "")
        if pending is None or pending["verifier"] != "saml":
            raise SignInError("state")
        idp = self.idp()
        try:
            root = _parse(base64.b64decode(saml_response, validate=False))
        except (ValueError, etree.XMLSyntaxError) as exc:
            raise SignInError("token", f"unreadable response: {exc}") from exc
        if root.tag != f"{{{NS['samlp']}}}Response":
            raise SignInError("token", "not a SAML response")

        status = root.find("samlp:Status/samlp:StatusCode", NS)
        if status is None or status.get("Value") != SUCCESS:
            raise SignInError("denied", f"status {status.get('Value') if status is not None else 'missing'}")

        assertion = self._verified_assertion(root, idp)
        now = dt.datetime.now(dt.UTC)

        # From here on, only `assertion` (which the signature covers) is read.
        if (assertion.findtext("saml:Issuer", namespaces=NS) or "").strip() != idp.entity_id:
            raise SignInError("token", "the assertion was issued by someone else")
        conditions = assertion.find("saml:Conditions", NS)
        if conditions is None:
            raise SignInError("token", "the assertion has no conditions")
        if (nb := _time(conditions.get("NotBefore"))) and now + SKEW < nb:
            raise SignInError("token", "the assertion is not valid yet")
        if (na := _time(conditions.get("NotOnOrAfter"))) and now - SKEW >= na:
            raise SignInError("token", "the assertion has expired")
        audiences = [a.text.strip() for a in conditions.findall("saml:AudienceRestriction/saml:Audience", NS) if a.text]
        if self.entity_id not in audiences:
            raise SignInError("token", f"the assertion is meant for {audiences}, not for RadFetch")

        subject = assertion.find("saml:Subject", NS)
        confirmed = False
        for sc in subject.findall("saml:SubjectConfirmation", NS) if subject is not None else []:
            data = sc.find("saml:SubjectConfirmationData", NS)
            if sc.get("Method") != BEARER or data is None:
                continue
            expires = _time(data.get("NotOnOrAfter"))
            if (
                data.get("Recipient") == self.acs_url
                and data.get("InResponseTo") == pending["nonce"]  # answers the request we made
                and expires is not None
                and now - SKEW < expires
            ):
                confirmed = True
        if not confirmed:
            raise SignInError("token", "the assertion does not answer our request")

        if self.s.saml_require_authn_context:
            contexts = [
                c.text.strip()
                for c in assertion.findall("saml:AuthnStatement/saml:AuthnContext/saml:AuthnContextClassRef", NS)
                if c.text
            ]
            if self.s.saml_require_authn_context not in contexts:
                raise SignInError("mfa", f"authentication context is {contexts}")

        name_id = (subject.findtext("saml:NameID", namespaces=NS) or "").strip()
        attributes: dict[str, list[str]] = {}
        for attr in assertion.findall("saml:AttributeStatement/saml:Attribute", NS):
            vals = [v.text.strip() for v in attr.findall("saml:AttributeValue", NS) if v.text]
            for key in {attr.get("Name"), attr.get("FriendlyName")} - {None}:
                attributes.setdefault(key, []).extend(vals)
        username = (attributes.get(self.s.saml_username_attribute) or [""])[0] if self.s.saml_username_attribute else name_id
        if not name_id and not username:
            raise SignInError("username", "the assertion names nobody")
        groups = attributes.get(self.s.saml_groups_attribute, [])
        return f"saml:{idp.entity_id}|{name_id or username}", username, groups

    def _verified_assertion(self, root: etree._Element, idp: IdpMetadata) -> etree._Element:
        """The one assertion the provider's signature vouches for.

        The signature may be on the whole response or on the assertion. Either way the
        element returned comes out of the verifier, not out of the submitted document.
        """
        assertions = root.findall("saml:Assertion", NS)
        if root.find("saml:EncryptedAssertion", NS) is not None:
            raise SignInError("token", "encrypted assertions are not supported; turn assertion encryption off")
        if len(assertions) != 1:
            raise SignInError("token", f"expected one assertion, found {len(assertions)}")

        config = SignatureConfiguration(location="./", signature_methods=_ASYMMETRIC)
        last = "no signature"
        for target, expect in ((root, "Response"), (assertions[0], "Assertion")):
            if target.find("ds:Signature", NS) is None:
                continue
            data = etree.tostring(target)
            for cert in idp.certificates:
                try:
                    result = XMLVerifier().verify(data, x509_cert=cert, expect_config=config, parser=_parser())
                except Exception as exc:  # wrong certificate, altered content, unsupported algorithm
                    last = str(exc)
                    continue
                signed = result.signed_xml
                if signed is None or etree.QName(signed).localname != expect or signed.get("ID") != target.get("ID"):
                    last = "the signature covers something else"
                    continue
                if expect == "Assertion":
                    return signed
                inner = signed.findall("saml:Assertion", NS)
                if len(inner) == 1:
                    return inner[0]
                last = "the signed response does not hold exactly one assertion"
        raise SignInError("token", f"signature not verified: {last}")

    def sign_in(self, conn: store.Conn, saml_response: str, relay_state: str):
        subject, username, groups = self.complete(conn, saml_response, relay_state)
        return directory.admit(conn, self.s, subject, username, groups)


def _time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
