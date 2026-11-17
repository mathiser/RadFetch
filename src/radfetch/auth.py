"""Roles, permissions and password hashing.

A user's effective permissions are: the permissions of their role, plus their
individual grants, minus their individual denies. `allowed_flows` additionally
limits what they can see and act on to certain flows.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass
from urllib.parse import quote

PERMISSIONS = {
    "jobs.view": "See jobs, batches and their states",
    "identifiers.view": "See accession numbers, study UIDs, error texts and job histories",
    "stats.view": "See dashboards and metrics",
    "results.view": "See, search and export the structured results flows report",
    "jobs.submit": "Submit one-off jobs",
    "batches.submit": "Submit batches",
    "jobs.manage_own": "Cancel, retry, pause and resume jobs and batches they submitted",
    "jobs.manage_all": "Cancel, retry, pause and resume anyone's jobs and batches",
    "audit.view": "See the system-wide audit log",
    "users.manage": "Create, change and remove users and their tokens",
}

# Each role includes everything of the role before it.
_LEVELS = [
    ("monitor", ["jobs.view", "stats.view"]),
    ("viewer", ["identifiers.view", "results.view"]),
    ("submitter", ["jobs.submit", "batches.submit", "jobs.manage_own"]),
    ("operator", ["jobs.manage_all"]),
    ("admin", ["audit.view", "users.manage"]),
]
ROLES: dict[str, frozenset[str]] = {}
_acc: set[str] = set()
for _name, _adds in _LEVELS:
    _acc |= set(_adds)
    ROLES[_name] = frozenset(_acc)

ROLE_DESCRIPTIONS = {
    "monitor": "Dashboards and job states only, without patient identifiers. For IT monitoring and wall displays.",
    "viewer": "Read-only, including accession numbers and job histories.",
    "submitter": "Can submit jobs and batches and manage their own.",
    "operator": "Can also manage everyone's jobs and batches.",
    "admin": "Can also manage users and see the audit log.",
}


@dataclass(frozen=True)
class Principal:
    """Who is making a request."""

    name: str | None
    role: str
    permissions: frozenset[str]
    allowed_flows: frozenset[str] | None = None  # None: every flow
    user_id: str | None = None
    sso: bool = False  # this sign-in came through the identity provider
    has_password: bool = True  # False for accounts whose password lives in a directory
    mfa_enabled: bool = False
    # Two-step sign-in is mandatory and this password account has not set it up yet:
    # until it has, the account can do nothing else.
    mfa_setup_required: bool = False

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def sees_flow(self, flow: str) -> bool:
        return self.allowed_flows is None or flow in self.allowed_flows


def effective_permissions(role: str, grants: list[str], denies: list[str]) -> frozenset[str]:
    return frozenset((ROLES[role] | set(grants)) - set(denies))


def principal_for(user: dict) -> Principal:
    flows = user["allowed_flows"]
    return Principal(
        name=user["username"],
        role=user["role"],
        permissions=effective_permissions(user["role"], user["grants"], user["denies"]),
        allowed_flows=None if flows is None else frozenset(flows),
        user_id=str(user["id"]),
        mfa_enabled=bool(user.get("totp_enabled")),
    )


def validate_permissions(names: list[str]) -> None:
    unknown = sorted(set(names) - set(PERMISSIONS))
    if unknown:
        raise ValueError(f"unknown permission(s): {', '.join(unknown)}")


USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$"  # allows e-mail style names from an identity provider


def valid_username(name: str) -> bool:
    import re

    return bool(re.match(USERNAME_PATTERN, name))


# --- passwords -----------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 10
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, bytes.fromhex(digest))


def generate_password() -> str:
    return secrets.token_urlsafe(15)


# --- two-step sign-in (TOTP, RFC 6238) -----------------------------------------------

TOTP_PERIOD = 30


def totp_new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def totp_code(secret: str, step: int) -> str:
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def totp_verify(secret: str | None, code: str, last_step: int | None, now: float | None = None) -> int | None:
    """The time step the code belongs to, if it is valid and newer than the last one accepted.

    Accepts the current 30-second step and one either side, for clock drift. Returning the
    step lets the caller record it, so the same code cannot be used twice.
    """
    code = "".join(ch for ch in code if ch.isdigit())
    if not secret or len(code) != 6:
        return None
    current = int((now if now is not None else time.time()) // TOTP_PERIOD)
    for step in (current, current - 1, current + 1):
        if (last_step is None or step > last_step) and hmac.compare_digest(totp_code(secret, step), code):
            return step
    return None


def totp_uri(secret: str, username: str, issuer: str = "RadFetch") -> str:
    """What an authenticator app scans."""
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


def new_recovery_codes(n: int = 10) -> list[str]:
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(n)]


def recovery_hash(code: str) -> str:
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()
