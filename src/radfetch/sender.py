"""The sender: delivers run output to its destinations, retried independently of inference."""

from __future__ import annotations

import logging
import os
import shutil
import socket
import time
import uuid
from pathlib import Path
from typing import Callable

import httpx
from pydicom import dcmread
from pydicom.errors import InvalidDicomError
from pynetdicom import AE

from radfetch import store, tls
from radfetch.config import Settings
from radfetch.loop import run_steps

log = logging.getLogger("radfetch.sender")


class DeliveryError(Exception):
    pass


def send_directory(out_dir: Path, dest: dict, job_id: str) -> None:
    target = Path(dest["path"]) / job_id
    tmp = target.with_name(target.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(target, ignore_errors=True)
    try:
        shutil.copytree(out_dir, tmp)
        os.replace(tmp, target)
    except OSError as exc:
        raise DeliveryError(str(exc)) from exc


def send_dicom(out_dir: Path, dest: dict, settings: Settings) -> None:
    """C-STORE every DICOM file under out_dir. Files that are not DICOM are skipped."""
    datasets = []
    for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
        try:
            datasets.append(dcmread(path))
        except InvalidDicomError:
            continue
    if not datasets:
        raise DeliveryError("run produced no DICOM files")
    ae = AE(ae_title=settings.local_ae_title)
    ae.acse_timeout, ae.dimse_timeout = settings.acse_timeout_s, settings.dimse_timeout_s
    for sop_class, syntax in sorted({(ds.SOPClassUID, ds.file_meta.TransferSyntaxUID) for ds in datasets}):
        ae.add_requested_context(sop_class, syntax)
    try:
        tls_args = (tls.client_context(settings), dest["host"]) if dest.get("tls") else None
        assoc = ae.associate(dest["host"], dest["port"], ae_title=dest["ae_title"], tls_args=tls_args)
    except Exception as exc:
        raise DeliveryError(f"cannot connect to {dest['ae_title']}: {exc}") from exc
    if not assoc.is_established:
        raise DeliveryError(f"association with {dest['ae_title']} not established")
    try:
        for ds in datasets:
            try:
                status = assoc.send_c_store(ds)
            except Exception as exc:  # e.g. no accepted presentation context
                raise DeliveryError(f"C-STORE of {ds.SOPInstanceUID} failed: {exc}") from exc
            # 0xB000-range are warnings: stored, with coercion or similar.
            if not status or (status.Status != 0x0000 and status.Status >> 12 != 0xB):
                code = f"0x{status.Status:04X}" if status else "no response"
                raise DeliveryError(f"C-STORE of {ds.SOPInstanceUID} failed: {code}")
    finally:
        assoc.release()


def send_stow(out_dir: Path, dest: dict, settings: Settings) -> None:
    """STOW-RS every DICOM file under out_dir in one multipart request, streamed from disk."""
    files = []
    for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
        try:
            dcmread(path, stop_before_pixels=True)
        except InvalidDicomError:
            continue
        files.append(path)
    if not files:
        raise DeliveryError("run produced no DICOM files")
    headers = {}
    if dest.get("token_env"):
        token = os.environ.get(dest["token_env"])
        if not token:
            raise DeliveryError(f"environment variable {dest['token_env']} is not set for the sender")
        headers["Authorization"] = f"Bearer {token}"
    boundary = f"radfetch-{uuid.uuid4().hex}"
    headers["Content-Type"] = f'multipart/related; type="application/dicom"; boundary={boundary}'
    headers["Accept"] = "application/dicom+json"

    def body():
        for path in files:
            yield f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
            with open(path, "rb") as f:
                while chunk := f.read(1 << 16):
                    yield chunk
            yield b"\r\n"
        yield f"--{boundary}--\r\n".encode()

    try:
        r = httpx.post(
            dest["url"].rstrip("/") + "/studies", content=body(), headers=headers, timeout=settings.dicomweb_timeout_s
        )
    except httpx.HTTPError as exc:
        raise DeliveryError(f"STOW-RS request failed: {exc}") from exc
    # 200: everything stored. 202: some instances were refused. 409: all were refused.
    if r.status_code != 200:
        raise DeliveryError(f"STOW-RS returned HTTP {r.status_code}")


class Sender:
    def __init__(self, conn: store.Conn, settings: Settings, *, sleep: Callable[[float], None] = time.sleep):
        self.conn, self.s = conn, settings
        self.owner = f"sender-{socket.gethostname()}-{os.getpid()}"
        self._sleep = sleep

    def run_forever(self) -> None:
        log.info("sender started as %s", self.owner)
        run_steps(self.step, self.s.idle_sleep_s, self._sleep)

    def step(self) -> bool:
        d = store.claim_delivery(self.conn, self.owner, self.s.lease_s)
        if d is None:
            return False
        dest = d["destination"]
        out_dir = self.s.runs_dir / str(d["run_id"]) / "out"
        try:
            if not out_dir.is_dir():
                raise DeliveryError("run output directory is missing")
            if dest["type"] == "directory":
                send_directory(out_dir, dest, str(d["job_id"]))
            elif dest["type"] == "dicom":
                send_dicom(out_dir, dest, self.s)
            elif dest["type"] == "stow":
                send_stow(out_dir, dest, self.s)
            else:
                raise DeliveryError(f"unknown destination type: {dest['type']}")
        except DeliveryError as exc:
            store.delivery_failed(
                self.conn, d["id"], self.owner, str(exc), self.s.deliver_max_attempts, self.s.backoff_s(d["attempts"])
            )
            log.warning("delivery %s failed: %s", d["id"], exc)
            return True
        store.delivery_done(self.conn, d["id"], self.owner)
        log.info("delivered job %s to %s", d["job_id"], dest["type"])
        return True
