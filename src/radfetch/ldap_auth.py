"""Sign-in with a directory username and password over LDAP (Active Directory, OpenLDAP).

The person types their directory credentials into RadFetch's own form. RadFetch finds
them in the directory, checks the password by binding as them, and reads their groups.
The password is used for that one bind and never stored.
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass

import ldap3
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn, parse_dn

from radfetch.config import Settings
from radfetch.directory import SignInError

log = logging.getLogger("radfetch.ldap")


@dataclass(frozen=True)
class LdapIdentity:
    subject: str
    username: str
    groups: list[str]  # each group as its full DN and as its bare CN


class Ldap:
    def __init__(self, settings: Settings, connect=None):
        self.s = settings
        # `connect(user, password)` returns a bound ldap3 connection or raises; replaced in tests.
        self._connect = connect or self._real_connect

    @property
    def enabled(self) -> bool:
        return bool(self.s.ldap_url)

    def check_config(self) -> None:
        """Raise ValueError early for a set-up that cannot work, or that would expose passwords."""
        if not self.enabled:
            return
        secure = self.s.ldap_url.lower().startswith("ldaps://") or self.s.ldap_start_tls
        if not secure and not self.s.ldap_allow_insecure:
            raise ValueError(
                "RADFETCH_LDAP_URL is not encrypted: use ldaps://, or set RADFETCH_LDAP_START_TLS=true. "
                "Plain ldap:// sends every password in clear text."
            )
        if self.s.ldap_bind_dn:
            if not self.s.ldap_user_base:
                raise ValueError("RADFETCH_LDAP_USER_BASE must be set when a service account looks people up")
            if "{username}" not in self.s.ldap_user_filter:
                raise ValueError("RADFETCH_LDAP_USER_FILTER must contain {username}")
        elif "{username}" not in self.s.ldap_user_dn_template:
            raise ValueError(
                "LDAP sign-in needs either a service account (RADFETCH_LDAP_BIND_DN) or "
                "RADFETCH_LDAP_USER_DN_TEMPLATE containing {username}"
            )

    # --- connections -----------------------------------------------------------------

    def _real_connect(self, user: str, password: str) -> ldap3.Connection:
        tls = ldap3.Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=str(self.s.ldap_ca_file) if self.s.ldap_ca_file else None,
        )
        # ldap3 needs whole seconds here: a fractional receive timeout fails deep inside the socket code.
        timeout = max(1, int(round(self.s.ldap_timeout_s)))
        server = ldap3.Server(self.s.ldap_url, tls=tls, connect_timeout=timeout, get_info=ldap3.NONE)
        conn = ldap3.Connection(server, user=user, password=password, receive_timeout=timeout, raise_exceptions=False)
        if self.s.ldap_start_tls:
            conn.open()
            if not conn.start_tls():
                raise LDAPException("StartTLS was refused by the directory")
        if not conn.bind():
            conn.unbind()
            raise _BindRefused(conn.result.get("description", "bind refused"))
        return conn

    # --- sign-in ---------------------------------------------------------------------

    def authenticate(self, username: str, password: str) -> LdapIdentity | None:
        """The person's identity if the directory accepts the password; None if it does not.

        Raises SignInError("provider") when the directory cannot be reached, so that an
        outage is not reported to the person as a wrong password.
        """
        # An empty password makes many directories perform an anonymous bind, which "succeeds".
        if not username or not password or "\x00" in username + password:
            return None
        try:
            if self.s.ldap_bind_dn:
                return self._search_then_bind(username, password)
            return self._direct_bind(username, password)
        except _BindRefused:
            return None
        except LDAPException as exc:
            raise SignInError("provider", f"LDAP: {exc}") from exc

    def _search_then_bind(self, username: str, password: str) -> LdapIdentity | None:
        try:
            service = self._connect(self.s.ldap_bind_dn, self.s.ldap_bind_password or "")
        except _BindRefused as exc:
            raise SignInError("provider", f"the LDAP service account was refused: {exc}") from exc
        try:
            entry = self._find(service, self.s.ldap_user_base, username)
            if entry is None:
                return None
            self._connect(entry["dn"], password).unbind()  # raises _BindRefused on a wrong password
            return self._identity(entry, username, self._searched_groups(service, entry["dn"], username))
        finally:
            service.unbind()

    def _direct_bind(self, username: str, password: str) -> LdapIdentity | None:
        template = self.s.ldap_user_dn_template
        # In a DN the name must be escaped as a DN value; in user@domain form it is used as typed.
        safe = escape_rdn(username) if "=" in template else username
        bind_as = template.format(username=safe)
        conn = self._connect(bind_as, password)
        try:
            entry = None
            if self.s.ldap_user_base:
                entry = self._find(conn, self.s.ldap_user_base, username)
            elif "=" in template:
                entry = self._read(conn, bind_as)
            entry = entry or {"dn": bind_as, "attributes": {}}
            return self._identity(entry, username, self._searched_groups(conn, entry["dn"], username))
        finally:
            conn.unbind()

    def _attributes(self) -> list[str]:
        return [self.s.ldap_username_attribute, self.s.ldap_groups_attribute, "objectGUID", "entryUUID"]

    def _find(self, conn: ldap3.Connection, base: str, username: str) -> dict | None:
        # escape_filter_chars stops a typed name such as "*)(uid=*" from changing the search.
        search_filter = self.s.ldap_user_filter.format(username=escape_filter_chars(username))
        conn.search(base, search_filter, attributes=self._attributes(), size_limit=2)
        entries = [e for e in conn.response or [] if e.get("type") == "searchResEntry"]
        if len(entries) != 1:  # nobody, or an ambiguous filter: never guess
            return None
        return entries[0]

    def _searched_groups(self, conn: ldap3.Connection, user_dn: str, username: str) -> list[str]:
        """Groups found by searching, when RADFETCH_LDAP_GROUP_BASE is set."""
        if not self.s.ldap_group_base:
            return []
        search_filter = self.s.ldap_group_filter.format(
            user_dn=escape_filter_chars(user_dn), username=escape_filter_chars(username)
        )
        conn.search(self.s.ldap_group_base, search_filter, attributes=["cn"])
        return [e["dn"] for e in conn.response or [] if e.get("type") == "searchResEntry"]

    def _read(self, conn: ldap3.Connection, dn: str) -> dict | None:
        conn.search(dn, "(objectClass=*)", search_scope=ldap3.BASE, attributes=self._attributes())
        entries = [e for e in conn.response or [] if e.get("type") == "searchResEntry"]
        return entries[0] if entries else None

    def _identity(self, entry: dict, typed: str, searched: list[str] | None = None) -> LdapIdentity:
        attrs = entry.get("attributes") or {}

        def values(name: str) -> list[str]:
            v = attrs.get(name)
            if v is None or v == []:
                return []
            return [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]

        # The directory's own spelling of the name, so "Anna" and "anna" are one account.
        username = (values(self.s.ldap_username_attribute) or [typed])[0]
        stable = (values("objectGUID") or values("entryUUID") or [entry["dn"].lower()])[0]
        groups: list[str] = []
        for group in [*values(self.s.ldap_groups_attribute), *(searched or [])]:
            groups.append(group)
            try:
                kind, value, _ = parse_dn(group)[0]
                if kind.lower() == "cn":
                    groups.append(value)
            except Exception:  # not a DN: a plain group name
                pass
        return LdapIdentity(f"ldap:{self.s.ldap_url}|{stable}", username, groups)


class _BindRefused(Exception):
    """The directory said no to these credentials."""
