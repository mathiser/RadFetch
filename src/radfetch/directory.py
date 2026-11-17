"""What every sign-in through a directory shares: turning groups into a role, and the
messages shown when a person is not let in."""

from __future__ import annotations

import json

from radfetch import auth, store
from radfetch.config import Settings


class SignInError(Exception):
    """Sign-in through a directory did not succeed. `code` is safe to show to the person."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


MESSAGES = {
    "not_configured": "Single sign-on is not set up.",
    "provider": "The identity provider could not be reached or answered unexpectedly.",
    "state": "This sign-in attempt has expired or was already used. Please try again.",
    "token": "The identity provider's answer could not be verified.",
    "mfa": "Sign-in was refused because a second factor is required and the identity provider did not confirm one.",
    "username": "The identity provider did not supply a usable username.",
    "username_taken": "An account with your username already exists and is not set up for sign-in through the "
    "directory. Ask an administrator.",
    "not_authorized": "Your directory account is valid, but you have not been given access to RadFetch.",
    "disabled": "Your RadFetch account is disabled.",
    "denied": "Sign-in was cancelled or refused at the identity provider.",
}


def role_map(settings: Settings) -> dict[str, str]:
    raw = (settings.group_role_map or settings.oidc_role_map).strip()
    if not raw:
        return {}
    try:
        mapping = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"RADFETCH_GROUP_ROLE_MAP is not valid JSON: {exc}") from exc
    unknown = sorted(set(mapping.values()) - set(auth.ROLES))
    if unknown:
        raise ValueError(f"RADFETCH_GROUP_ROLE_MAP names unknown role(s): {', '.join(unknown)}")
    return mapping


def default_role(settings: Settings) -> str | None:
    role = settings.external_default_role or settings.oidc_default_role
    if role and role not in auth.ROLES:
        raise ValueError(f"RADFETCH_EXTERNAL_DEFAULT_ROLE is not a role: {role}")
    return role or None


def role_for(settings: Settings, groups: list[str]) -> str | None:
    """The highest role any of the person's groups maps to. Group names compare case-insensitively."""
    order = list(auth.ROLES)
    have = {g.lower() for g in groups}
    roles = [role for group, role in role_map(settings).items() if group.lower() in have]
    return max(roles, key=order.index) if roles else None


def admit(conn: store.Conn, settings: Settings, subject: str, username: str, groups: list[str]):
    """Find or create the RadFetch account for a person a directory has authenticated."""
    if not auth.valid_username(username):
        raise SignInError("username", f"username is {username!r}")
    try:
        return store.sso_sign_in(conn, subject, username, role_for(settings, groups), default_role(settings))
    except ValueError as exc:
        raise SignInError(str(exc) if str(exc) in MESSAGES else "not_authorized") from exc
