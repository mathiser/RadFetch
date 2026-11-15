"""Turns outbox rows into webhook messages."""

from __future__ import annotations

import httpx

from radfetch import store
from radfetch.config import Settings


class NotifyError(Exception):
    pass


def build_message(conn: store.Conn, settings: Settings, n: store.Row) -> dict | None:
    """The JSON body for a notification, or None when its subject no longer exists."""
    kind, link = n["kind"], f" {settings.public_url.rstrip('/')}/" if settings.public_url else ""
    if kind == "job_failed":
        job = store.get_job(conn, n["ref"])
        if job is None:
            return None
        body = {"event": kind, "job_id": str(job["id"]), "flow": job["flow"], "source": job["source"]}
        text = f"RadFetch: a job failed (flow {job['flow']}, job {job['id']})."
        if settings.notify_include_identifiers:
            body |= {"accession_number": job["accession_number"], "error": job["error"]}
            text = f"RadFetch: job for {job['accession_number']} (flow {job['flow']}) failed: {job['error']}"
    elif kind == "batch_finished":
        batch = store.get_batch(conn, n["ref"])
        if batch is None:
            return None
        counts = batch["jobs"]
        summary = ", ".join(f"{counts[s]} {s}" for s in ("done", "failed", "cancelled") if counts.get(s))
        body = {"event": kind, "batch_id": str(batch["id"]), "name": batch["name"], "flow": batch["flow"], "jobs": counts}
        text = f"RadFetch: batch \"{batch['name']}\" (flow {batch['flow']}) finished: {summary}."
    elif kind == "pacs_unreachable":
        body = {"event": kind, "consecutive_failures": n["detail"].get("failures")}
        text = "RadFetch: the PACS is not answering. Retrieval is paused and will resume by itself."
        if settings.notify_include_identifiers:
            body["error"] = n["detail"].get("error")
    else:
        return None
    return {**body, "text": text + link}


def send(settings: Settings, message: dict) -> None:
    try:
        r = httpx.post(settings.notify_webhook_url, json=message, timeout=15)
    except httpx.HTTPError as exc:
        raise NotifyError(f"webhook request failed: {exc}") from exc
    if not 200 <= r.status_code < 300:
        raise NotifyError(f"webhook returned HTTP {r.status_code}")
