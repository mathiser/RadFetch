"""PacsClient over DICOMweb: QIDO-RS for queries, WADO-RS for retrieval."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import BinaryIO, Callable, Iterable

import httpx
from pydicom import dcmread

from radfetch.config import Settings
from radfetch.gateway.pacs import PacsError, RetrieveResult, SeriesInfo, StudyInfo, valid_uid

STUDY_UID, SERIES_UID = "0020000D", "0020000E"
ACCESSION, STUDY_DESCRIPTION, STUDY_DATE = "00080050", "00081030", "00080020"
MODALITY, SERIES_DESCRIPTION, SERIES_INSTANCES = "00080060", "0008103E", "00201209"
PAGE = 200


def _value(item: dict, tag: str):
    values = item.get(tag, {}).get("Value") or [None]
    return values[0]


def split_multipart(chunks: Iterable[bytes], boundary: bytes, open_part: Callable[[], BinaryIO]) -> int:
    """Stream a multipart body into one file per part without holding a part in memory. Returns the part count."""
    delimiter = b"--" + boundary
    separator = b"\r\n" + delimiter
    buf = b"\r\n"  # lets the first delimiter be found like every other one
    it = iter(chunks)
    out: BinaryIO | None = None
    parts = 0

    def more() -> bool:
        nonlocal buf
        for chunk in it:
            if chunk:
                buf += chunk
                return True
        return False

    try:
        while True:
            # Everything up to the next delimiter is preamble (first time) or the current part's body.
            while (idx := buf.find(separator)) < 0:
                keep = len(separator) - 1  # a delimiter may straddle two chunks
                if out is not None and len(buf) > keep:
                    out.write(buf[:-keep])
                    buf = buf[-keep:]
                if not more():
                    raise PacsError("multipart response ended without a closing boundary")
            if out is not None:
                out.write(buf[:idx])
                out.close()
                out = None
            buf = buf[idx + len(separator) :]
            while len(buf) < 2:
                if not more():
                    raise PacsError("multipart response truncated after a boundary")
            if buf.startswith(b"--"):
                return parts
            # Skip the rest of the boundary line and the part headers.
            while (end := buf.find(b"\r\n\r\n")) < 0:
                if not more():
                    raise PacsError("multipart response truncated inside part headers")
            buf = buf[end + 4 :]
            out = open_part()
            parts += 1
    finally:
        if out is not None:
            out.close()


class DicomWebClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        if not settings.dicomweb_url:
            raise ValueError("RADFETCH_DICOMWEB_URL must be set for retrieve mode 'dicomweb'")
        headers = {"Authorization": f"Bearer {settings.dicomweb_token}"} if settings.dicomweb_token else {}
        self._http = httpx.Client(
            base_url=settings.dicomweb_url.rstrip("/") + "/",
            headers=headers,
            timeout=settings.dicomweb_timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _query(self, path: str, params: dict) -> list[dict]:
        """QIDO-RS with paging.

        A server may cap a response below the requested limit, so a short page does not
        mean the end: keep asking until a page comes back empty. That costs one extra
        small request per query.
        """
        results: list[dict] = []
        previous: list[dict] | None = None
        while True:
            try:
                r = self._http.get(
                    path,
                    params={**params, "limit": PAGE, "offset": len(results)},
                    headers={"Accept": "application/dicom+json"},
                )
            except httpx.HTTPError as exc:
                raise PacsError(f"QIDO-RS request failed: {exc}") from exc
            if r.status_code == 204:
                return results
            if r.status_code != 200:
                raise PacsError(f"QIDO-RS returned HTTP {r.status_code}")
            try:
                page = r.json()
            except ValueError as exc:
                raise PacsError("QIDO-RS returned invalid JSON") from exc
            if not page or page == previous:  # empty, or a server that ignores `offset`
                return results
            results += page
            previous = page

    def find_studies(self, accession_number: str) -> list[StudyInfo]:
        items = self._query(
            "studies", {"AccessionNumber": accession_number, "includefield": f"{STUDY_DESCRIPTION},{STUDY_DATE}"}
        )
        return [
            StudyInfo(
                study_instance_uid=str(_value(i, STUDY_UID)),
                accession_number=str(_value(i, ACCESSION) or accession_number),
                description=_value(i, STUDY_DESCRIPTION),
                date=_value(i, STUDY_DATE),
            )
            for i in items
            if _value(i, STUDY_UID)
        ]

    def find_series(self, study_instance_uid: str) -> list[SeriesInfo]:
        if not valid_uid(study_instance_uid):
            raise PacsError("malformed StudyInstanceUID")
        items = self._query(
            f"studies/{study_instance_uid}/series", {"includefield": f"{SERIES_DESCRIPTION},{SERIES_INSTANCES}"}
        )
        out = []
        for i in items:
            if not _value(i, SERIES_UID):
                continue
            n = _value(i, SERIES_INSTANCES)
            out.append(
                SeriesInfo(
                    series_instance_uid=str(_value(i, SERIES_UID)),
                    study_instance_uid=study_instance_uid,
                    modality=_value(i, MODALITY),
                    description=_value(i, SERIES_DESCRIPTION),
                    instances=int(n) if n is not None else None,
                )
            )
        return out

    def retrieve_series(self, study_instance_uid: str, series_instance_uid: str, dest: Path) -> RetrieveResult:
        if not (valid_uid(study_instance_uid) and valid_uid(series_instance_uid)):
            raise PacsError("malformed UID")
        dest.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []

        def open_part() -> BinaryIO:
            parts.append(dest / f".part-{uuid.uuid4().hex}")
            return open(parts[-1], "wb")

        try:
            with self._http.stream(
                "GET",
                f"studies/{study_instance_uid}/series/{series_instance_uid}",
                headers={"Accept": 'multipart/related; type="application/dicom"; transfer-syntax=*'},
            ) as r:
                if r.status_code not in (200, 206):
                    raise PacsError(f"WADO-RS returned HTTP {r.status_code}")
                content_type = r.headers.get("content-type", "")
                boundary = next(
                    (p.split("=", 1)[1].strip().strip('"') for p in content_type.split(";") if p.strip().lower().startswith("boundary=")),
                    None,
                )
                if not boundary:
                    raise PacsError("WADO-RS response is not multipart")
                # 206 Partial Content: the server could not deliver every instance.
                failed = 1 if r.status_code == 206 else 0
                split_multipart(r.iter_bytes(1 << 16), boundary.encode(), open_part)
        except httpx.HTTPError as exc:
            raise PacsError(f"WADO-RS request failed: {exc}") from exc

        received: set[str] = set()
        nbytes = 0
        for part in parts:
            try:
                header = dcmread(part, stop_before_pixels=True)
                sop = str(header.SOPInstanceUID)
                ok = valid_uid(sop) and str(header.SeriesInstanceUID) == series_instance_uid
            except Exception:
                ok = False
            if not ok:
                part.unlink()
                failed += 1
                continue
            size = part.stat().st_size
            part.replace(dest / f"{sop}.dcm")
            if sop not in received:
                received.add(sop)
                nbytes += size
        return RetrieveResult(received=len(received), failed=failed, bytes=nbytes)
