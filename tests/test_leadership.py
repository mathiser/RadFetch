"""Standby replicas: only one gateway (and one ingress processor) is active, and a standby takes over."""

from radfetch import db, store
from radfetch.config import INTERACTIVE_PRIORITY
from radfetch.gateway.worker import Gateway
from radfetch.ingress import IngressProcessor


def test_only_one_gateway_is_active_and_standby_takes_over(conn, settings, pacs):
    other = db.connect(settings.database_url)
    first = Gateway(conn, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)
    standby = Gateway(other, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)
    store.submit_job(conn, "ACC0001", "seg", INTERACTIVE_PRIORITY)
    store.submit_job(conn, "ACC0002", "seg", INTERACTIVE_PRIORITY)

    standby.step()  # whichever asks first leads
    leader, follower = (standby, first) if standby._leading else (first, standby)
    assert leader.step()
    finds = pacs.finds
    assert not follower.step() and pacs.finds == finds  # the standby never touches the PACS

    leader.conn.close()  # the active gateway dies: its lock is released with its connection
    while follower.step():
        pass
    assert follower._leading
    assert store.count_jobs(follower.conn, "ready") == 2
    if follower.conn is not conn:
        follower.conn.close()


def test_standby_gateway_does_not_remove_the_leaders_temporary_files(conn, settings, pacs):
    other = db.connect(settings.database_url)
    leader = Gateway(conn, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None)
    leader.step()
    in_flight = settings.cache_dir / ".tmp" / "1.2.3.in-flight"
    in_flight.mkdir()
    Gateway(other, pacs, settings, disk_fraction=lambda: 0.1, sleep=lambda s: None).step()
    assert in_flight.is_dir()
    other.close()


def test_only_one_ingress_processor_is_active(conn, settings):
    settings.scp_stable_s = 0
    other = db.connect(settings.database_url)
    a, b = IngressProcessor(conn, settings), IngressProcessor(other, settings)
    a.step()
    b.step()
    assert a._leading and not b._leading
    other.close()
