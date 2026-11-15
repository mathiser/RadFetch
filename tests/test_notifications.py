"""Webhook notifications: what triggers them, what they contain, and delivery retries."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from radfetch import db, store
from radfetch.config import BATCH_PRIORITY, INTERACTIVE_PRIORITY
from radfetch.gateway.worker import Gateway
from radfetch.sender import Sender


@pytest.fixture
def hook(settings):
    """A local webhook receiver. `hook.status` sets the HTTP status it answers with."""

    class Hook:
        received: list[dict] = []
        status = 200

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            if Hook.status == 200:
                Hook.received.append(json.loads(body))
            self.send_response(Hook.status)
            self.end_headers()

        def log_message(self, *args):
            pass

    Hook.received = []
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings.notify_webhook_url = f"http://127.0.0.1:{server.server_port}/hook"
    yield Hook
    server.shutdown()


def drain(worker):
    while worker.step():
        pass


def gateway(conn, pacs, settings):
    return Gateway(conn, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)


def test_failed_one_off_job_notifies_without_identifiers_by_default(conn, settings, pacs, hook):
    job = store.submit_job(conn, "SECRET-ACC", "seg", INTERACTIVE_PRIORITY)
    ok = store.submit_job(conn, "ACC0001", "seg", INTERACTIVE_PRIORITY)
    drain(gateway(conn, pacs, settings))
    drain(Sender(conn, settings))
    (message,) = hook.received  # the successful job is silent
    assert message["event"] == "job_failed" and message["job_id"] == str(job)
    assert "SECRET-ACC" not in json.dumps(message)

    settings.notify_include_identifiers = True
    settings.public_url = "https://radfetch.example/"
    store.retry_job(conn, job)
    drain(gateway(conn, pacs, settings))
    drain(Sender(conn, settings))
    assert hook.received[1]["accession_number"] == "SECRET-ACC"
    assert hook.received[1]["text"].endswith("no study found for accession number SECRET-ACC https://radfetch.example/")
    assert store.get_job(conn, ok)["state"] == "ready"


def test_batch_notifies_once_when_its_last_job_finishes(conn, settings, pacs, hook):
    batch = store.create_batch(conn, "night run", "seg", ["ACC0001", "NOPE", "ACC0002"], BATCH_PRIORITY)
    drain(gateway(conn, pacs, settings))  # NOPE fails, two are ready
    drain(Sender(conn, settings))
    assert hook.received == []  # batch failures are reported in the summary, not one by one

    ready = [j["id"] for j in store.list_jobs(conn, "ready")]
    store.cancel_job(conn, ready[0])
    drain(Sender(conn, settings))
    assert hook.received == []
    store.cancel_job(conn, ready[1])
    drain(Sender(conn, settings))
    (message,) = hook.received
    assert message["event"] == "batch_finished" and message["batch_id"] == str(batch)
    assert message["jobs"] == {"failed": 1, "cancelled": 2}
    assert message["text"] == 'RadFetch: batch "night run" (flow seg) finished: 1 failed, 2 cancelled.'


def test_two_jobs_finishing_at_once_report_the_batch_exactly_once(conn, settings):
    store.create_batch(conn, "b", "seg", ["A", "B"], BATCH_PRIORITY)
    a, b = (j["id"] for j in store.list_jobs(conn))
    other = db.connect(settings.database_url)
    with conn.transaction():
        store.cancel_job(conn, a)
        t = threading.Thread(target=store.cancel_job, args=(other, b))  # blocks on the batch row lock
        t.start()
        t.join(0.5)
        assert t.is_alive()
    t.join(5)
    other.close()
    rows = conn.execute("select kind from notifications").fetchall()
    assert [r["kind"] for r in rows] == ["batch_finished"]


def test_pacs_outage_notifies_once_per_outage(conn, settings, pacs, hook):
    settings.breaker_failure_threshold = 2
    for i in range(4):
        store.submit_job(conn, f"ACC000{i + 1}", "seg", INTERACTIVE_PRIORITY)
    pacs.fail = True
    t = [0.0]
    gw = Gateway(conn, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None, clock=lambda: t[0])
    drain(gw)
    t[0] = 10_000  # cooldown over, still down: no second message for the same outage
    drain(gw)
    drain(Sender(conn, settings))
    assert [m["event"] for m in hook.received] == ["pacs_unreachable"]
    assert "error" not in hook.received[0]


def test_webhook_failures_are_retried_then_given_up(conn, settings, hook):
    settings.notify_max_attempts = 3
    store.enqueue_notification(conn, "pacs_unreachable", failures=3)
    hook.status = 500
    sender = Sender(conn, settings)
    drain(sender)
    row = conn.execute("select state, attempts, error from notifications").fetchone()
    assert (row["state"], row["attempts"]) == ("failed", 3) and "HTTP 500" in row["error"]

    store.enqueue_notification(conn, "pacs_unreachable", failures=3)
    hook.status = 200
    drain(sender)
    assert len(hook.received) == 1


def test_no_webhook_configured_discards_notifications(conn, settings, pacs):
    store.submit_job(conn, "NOPE", "seg", INTERACTIVE_PRIORITY)
    drain(gateway(conn, pacs, settings))
    drain(Sender(conn, settings))
    assert conn.execute("select state from notifications").fetchone()["state"] == "skipped"
