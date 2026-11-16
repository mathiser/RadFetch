"""Roles, permissions and password hashing.

A user's effective permissions are: the permissions of their role, plus their
individual grants, minus their individual denies. `allowed_flows` additionally
limits what they can see and act on to certain flows.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

PERMISSIONS = {
    "jobs.view": "See jobs, batches and their states",
    "identifiers.view": "See accession numbers, study UIDs, error texts and job histories",
    "stats.view": "See dashboards and metrics",
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
    ("viewer", ["identifiers.view"]),
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
    )


def validate_permissions(names: list[str]) -> None:
    unknown = sorted(set(names) - set(PERMISSIONS))
    if unknown:
        raise ValueError(f"unknown permission(s): {', '.join(unknown)}")


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
