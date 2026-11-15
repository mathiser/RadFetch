"""TLS contexts for DICOM connections, built from the RADFETCH_TLS_* settings."""

from __future__ import annotations

import ssl

from radfetch.config import Settings


def client_context(settings: Settings) -> ssl.SSLContext:
    """For connections we open. Verifies the peer against tls_ca_file (or the system store)
    and presents our certificate when one is configured, for peers that require mutual TLS."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=_str(settings.tls_ca_file))
    if settings.tls_cert_file:
        ctx.load_cert_chain(str(settings.tls_cert_file), _str(settings.tls_key_file))
    return ctx


def server_context(settings: Settings) -> ssl.SSLContext:
    """For our storage SCPs. With tls_ca_file set, clients must present a certificate signed by it."""
    if not settings.tls_cert_file:
        raise ValueError("RADFETCH_TLS_CERT_FILE (and RADFETCH_TLS_KEY_FILE) must be set to accept TLS connections")
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(str(settings.tls_cert_file), _str(settings.tls_key_file))
    if settings.tls_ca_file:
        ctx.load_verify_locations(str(settings.tls_ca_file))
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _str(path) -> str | None:
    return str(path) if path else None
