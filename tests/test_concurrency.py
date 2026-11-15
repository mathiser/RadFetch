"""Claims must hand each row to exactly one worker."""

import threading

from radfetch import db, store
from radfetch.config import INTERACTIVE_PRIORITY


def test_parallel_claims_do_not_overlap(conn, settings):
    for i in range(40):
        store.submit_job(conn, f"ACC{i:04d}", "seg", INTERACTIVE_PRIORITY)
    claimed: list = []

    def worker(name):
        c = db.connect(settings.database_url)
        while (job := store.claim_job_to_resolve(c, name, 60, INTERACTIVE_PRIORITY, True)) is not None:
            claimed.append(job["id"])
        c.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 40 == len(set(claimed))
