"""Single sign-on with OpenID Connect (authorization code flow with PKCE).

RadFetch sends the browser to the identity provider, gets a code back, exchanges
it for an ID token, verifies the token's signature and claims, and then maps the
person to a RadFetch account and role.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

from radfetch import directory, store
from radfetch.config import Settings
from radfetch.directory import MESSAGES, SignInError

__all__ = ["MESSAGES", "Oidc", "SsoError"]

# Asymmetric algorithms only: with these, nobody holding just the client secret can mint a token.
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]


SsoError = SignInError  # the name this module used before SAML and LDAP shared it


@dataclass(frozen=True)
class Identity:
    subject: str  # issuer|sub: unique and stable for a person
    username: str
    groups: list[str]


class Oidc:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.s = settings
        self._http = http or httpx.Client(timeout=15)
        self._discovery: dict | None = None
        self._discovery_at = 0.0
        self._jwks: jwt.PyJWKClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.s.oidc_issuer and self.s.oidc_client_id)

    @property
    def redirect_uri(self) -> str:
        return self.s.public_url.rstrip("/") + "/auth/oidc/callback"

    def check_config(self) -> None:
        """Raise ValueError early for a set-up that cannot work."""
        if not self.enabled:
            return
        if not self.s.public_url:
            raise ValueError("RADFETCH_PUBLIC_URL must be set for single sign-on: the provider redirects back to it")

    # --- provider metadata -----------------------------------------------------------------

    def discovery(self) -> dict:
        if self._discovery is None or time.monotonic() - self._discovery_at > 3600:
            url = self.s.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
            try:
                r = self._http.get(url)
                r.raise_for_status()
                doc = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise SsoError("provider", f"cannot read {url}: {exc}") from exc
            # The document must describe the issuer we were configured with, not some other one.
            if doc.get("issuer", "").rstrip("/") != self.s.oidc_issuer.rstrip("/"):
                raise SsoError("provider", "the provider's metadata names a different issuer")
            self._discovery, self._discovery_at = doc, time.monotonic()
            self._jwks = jwt.PyJWKClient(doc["jwks_uri"], cache_keys=True, lifespan=3600)
        return self._discovery

    # --- the two halves of a sign-in ---------------------------------------------------------

    def begin(self, conn: store.Conn) -> str:
        """Start a sign-in: remember state, nonce and PKCE verifier, and return where to send the browser."""
        if not self.enabled:
            raise SsoError("not_configured")
        doc = self.discovery()
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        store.save_oidc_login(conn, state, nonce, verifier)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {
            "response_type": "code",
            "client_id": self.s.oidc_client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.s.oidc_scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if self.s.oidc_acr_values:
            params["acr_values"] = self.s.oidc_acr_values
        return doc["authorization_endpoint"] + "?" + urlencode(params)

    def complete(self, conn: store.Conn, code: str, state: str) -> Identity:
        """Finish a sign-in: exchange the code, verify the ID token, and return who it is."""
        if not self.enabled:
            raise SsoError("not_configured")
        pending = store.take_oidc_login(conn, state)
        if pending is None:
            raise SsoError("state")
        doc = self.discovery()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.s.oidc_client_id,
            "code_verifier": pending["verifier"],
        }
        if self.s.oidc_client_secret:
            data["client_secret"] = self.s.oidc_client_secret
        try:
            r = self._http.post(doc["token_endpoint"], data=data)
            r.raise_for_status()
            id_token = r.json()["id_token"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise SsoError("provider", f"token exchange failed: {exc}") from exc

        try:
            key = self._jwks.get_signing_key_from_jwt(id_token).key
            claims = jwt.decode(
                id_token,
                key,
                algorithms=ALGORITHMS,
                audience=self.s.oidc_client_id,
                issuer=doc["issuer"],
                options={"require": ["exp", "iat", "sub"]},
                leeway=60,
            )
        except jwt.PyJWTError as exc:
            raise SsoError("token", str(exc)) from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), pending["nonce"]):
            raise SsoError("token", "nonce mismatch")

        if self.s.oidc_require_amr:
            amr = claims.get("amr") or []
            if self.s.oidc_require_amr not in ([amr] if isinstance(amr, str) else amr):
                raise SsoError("mfa", f"amr is {amr}")
        if self.s.oidc_require_acr and claims.get("acr") != self.s.oidc_require_acr:
            raise SsoError("mfa", f"acr is {claims.get('acr')}")

        username = str(claims.get(self.s.oidc_username_claim) or "").strip()
        groups = claims.get(self.s.oidc_groups_claim) or []
        if isinstance(groups, str):
            groups = [groups]
        return Identity(f"{doc['issuer']}|{claims['sub']}", username, [str(g) for g in groups])

    def sign_in(self, conn: store.Conn, code: str, state: str):
        """The whole second half: returns the RadFetch account row for the authenticated person."""
        identity = self.complete(conn, code, state)
        return directory.admit(conn, self.s, identity.subject, identity.username, identity.groups)

    def logout_url(self) -> str | None:
        """Where to send the browser to also sign out at the provider, if it offers that."""
        try:
            end = self.discovery().get("end_session_endpoint")
        except SsoError:
            return None
        if not end:
            return None
        params = {"client_id": self.s.oidc_client_id, "post_logout_redirect_uri": self.s.public_url.rstrip("/") + "/"}
        return end + "?" + urlencode(params)
