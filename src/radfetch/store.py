"""All state transitions. Every function is one atomic unit against Postgres.

Claims use leases instead of intermediate states: a row whose lease has expired
is simply claimable again, so a crashed worker needs no cleanup.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

from radfetch.gateway.pacs import SeriesInfo

Conn = psycopg.Connection
Row = dict[str, Any]

TERMINAL = ("done", "failed", "cancelled")
# Job states that still need their series in the cache.
NEEDS_CACHE = ("retrieving", "ready", "running")


def log_event(conn: Conn, job_id: uuid.UUID | None, kind: str, **detail: Any) -> None:
    conn.execute(
        "insert into events (job_id, kind, detail) values (%s, %s, %s)",
        (job_id, kind, Jsonb(detail)),
    )


# --- submission ---------------------------------------------------------------


def submit_job(
    conn: Conn,
    accession_number: str,
    flow: str,
    priority: int,
    batch_id: uuid.UUID | None = None,
    requested_by: str | None = None,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    with conn.transaction():
        conn.execute(
            "insert into jobs (id, batch_id, accession_number, flow, priority, requested_by)"
            " values (%s, %s, %s, %s, %s, %s)",
            (job_id, batch_id, accession_number, flow, priority, requested_by),
        )
        log_event(conn, job_id, "submitted", accession=accession_number, flow=flow, by=requested_by)
    return job_id


def create_batch(
    conn: Conn,
    name: str,
    flow: str,
    accession_numbers: Iterable[str],
    priority: int,
    requested_by: str | None = None,
) -> uuid.UUID:
    batch_id = uuid.uuid4()
    # Duplicates inside one batch would only repeat the same work.
    accessions = list(dict.fromkeys(a.strip() for a in accession_numbers if a.strip()))
    with conn.transaction():
        conn.execute(
            "insert into batches (id, name, flow, requested_by) values (%s, %s, %s, %s)",
            (batch_id, name, flow, requested_by),
        )
        for accession in accessions:
            submit_job(conn, accession, flow, priority, batch_id, requested_by)
    return batch_id


def set_batch_paused(conn: Conn, batch_id: uuid.UUID, paused: bool, by: str | None = None) -> bool:
    with conn.transaction():
        cur = conn.execute("update batches set paused = %s where id = %s", (paused, batch_id))
        if cur.rowcount:
            log_event(conn, None, "batch_paused" if paused else "batch_resumed", batch=str(batch_id), by=by)
        return cur.rowcount == 1


def cancel_job(conn: Conn, job_id: uuid.UUID, by: str | None = None) -> bool:
    """Cancel unless already finished. A running container is stopped by the executor's next heartbeat."""
    with conn.transaction():
        cur = conn.execute(
            "update jobs set state = 'cancelled', updated_at = now(), lease_owner = null, lease_expires_at = null"
            " where id = %s and state not in ('done','failed','cancelled')",
            (job_id,),
        )
        if cur.rowcount:
            log_event(conn, job_id, "cancelled", by=by)
        return cur.rowcount == 1


def retry_job(conn: Conn, job_id: uuid.UUID, full: bool = False, by: str | None = None) -> str | None:
    """Give a failed or cancelled job another go. Returns the state it restarts from, or None if not retryable.

    When only delivery failed and the run's output is still wanted, the job goes back
    to 'sending' and inference is not repeated. Otherwise (or with full=True) it
    starts over from 'queued' with fresh attempt budgets.
    """
    with conn.transaction():
        job = conn.execute(
            "select id from jobs where id = %s and state in ('failed','cancelled') for update", (job_id,)
        ).fetchone()
        if job is None:
            return None
        resend = None
        if not full:
            resend = conn.execute(
                "select r.id from runs r where r.job_id = %s and r.state = 'succeeded'"
                " and r.attempt = (select max(attempt) from runs where job_id = %s)"
                " and exists (select 1 from deliveries d where d.run_id = r.id and d.state = 'failed')",
                (job_id, job_id),
            ).fetchone()
        if resend:
            conn.execute(
                "update deliveries set state = 'pending', attempts = 0, not_before = now(), error = null,"
                " lease_owner = null, lease_expires_at = null where run_id = %s and state = 'failed'",
                (resend["id"],),
            )
            state = "sending"
        else:
            conn.execute("delete from deliveries where job_id = %s", (job_id,))
            # job_series stays, so the cached series stay protected until the job is resolved again.
            state = "queued"
        conn.execute(
            "update jobs set state = %s, error = null, attempts = 0, not_before = now(), retried_at = now(),"
            " lease_owner = null, lease_expires_at = null, updated_at = now() where id = %s",
            (state, job_id),
        )
        log_event(conn, job_id, "retried", restart_from=state, by=by)
        return state


def retry_failed_in_batch(conn: Conn, batch_id: uuid.UUID, by: str | None = None) -> int:
    with conn.transaction():
        rows = conn.execute("select id from jobs where batch_id = %s and state = 'failed'", (batch_id,)).fetchall()
        return sum(1 for row in rows if retry_job(conn, row["id"], by=by))


def cancel_batch(conn: Conn, batch_id: uuid.UUID, by: str | None = None) -> int:
    with conn.transaction():
        rows = conn.execute(
            "update jobs set state = 'cancelled', updated_at = now(), lease_owner = null, lease_expires_at = null"
            " where batch_id = %s and state not in ('done','failed','cancelled') returning id",
            (batch_id,),
        ).fetchall()
        for row in rows:
            log_event(conn, row["id"], "cancelled", via="batch", by=by)
        return len(rows)


# --- queries --------------------------------------------------------------------


def get_job(conn: Conn, job_id: uuid.UUID) -> Row | None:
    return conn.execute("select * from jobs where id = %s", (job_id,)).fetchone()


def list_jobs(
    conn: Conn,
    state: str | None = None,
    batch_id: uuid.UUID | None = None,
    limit: int = 100,
    flows: list[str] | None = None,
    search: str | None = None,
) -> list[Row]:
    """Newest first. `flows` limits the result to those flows; `search` matches the accession number's start."""
    return conn.execute(
        "select * from jobs where (%(state)s::text is null or state = %(state)s)"
        " and (%(batch)s::uuid is null or batch_id = %(batch)s)"
        " and (%(flows)s::text[] is null or flow = any(%(flows)s))"
        " and (%(search)s::text is null or accession_number ilike %(search)s || '%%')"
        " order by created_at desc, accession_number limit %(limit)s",
        {"state": state, "batch": batch_id, "limit": limit, "flows": flows, "search": search},
    ).fetchall()


def get_batch(conn: Conn, batch_id: uuid.UUID) -> Row | None:
    batch = conn.execute("select * from batches where id = %s", (batch_id,)).fetchone()
    if batch is None:
        return None
    counts = conn.execute(
        "select state, count(*) as n from jobs where batch_id = %s group by state", (batch_id,)
    ).fetchall()
    batch["jobs"] = {row["state"]: row["n"] for row in counts}
    return batch


def list_batches(conn: Conn, limit: int = 50, flows: list[str] | None = None) -> list[Row]:
    batches = conn.execute(
        "select * from batches where (%(flows)s::text[] is null or flow = any(%(flows)s))"
        " order by created_at desc limit %(limit)s",
        {"flows": flows, "limit": limit},
    ).fetchall()
    counts = conn.execute(
        "select batch_id, state, count(*) as n from jobs where batch_id = any(%s) group by batch_id, state",
        ([b["id"] for b in batches],),
    ).fetchall()
    for batch in batches:
        batch["jobs"] = {c["state"]: c["n"] for c in counts if c["batch_id"] == batch["id"]}
    return batches


def job_events(conn: Conn, job_id: uuid.UUID) -> list[Row]:
    return conn.execute("select at, kind, detail from events where job_id = %s order by id", (job_id,)).fetchall()


def count_jobs(conn: Conn, state: str) -> int:
    return conn.execute("select count(*) as n from jobs where state = %s", (state,)).fetchone()["n"]


def state_counts(conn: Conn, flows: list[str] | None = None) -> dict[str, int]:
    rows = conn.execute(
        "select state, count(*) as n from jobs where (%(f)s::text[] is null or flow = any(%(f)s)) group by state",
        {"f": flows},
    ).fetchall()
    return {row["state"]: row["n"] for row in rows}


def stats_overview(conn: Conn, hours: int, flows: list[str] | None = None) -> dict[str, Any]:
    """Numbers behind the dashboard for the last `hours` hours."""
    p = {"h": hours, "f": flows}
    scope = "(%(f)s::text[] is null or j.flow = any(%(f)s))"
    window = "now() - make_interval(hours => %(h)s)"
    finished = conn.execute(
        f"select date_trunc('hour', e.at) as hour, e.kind, count(*) as n from events e join jobs j on j.id = e.job_id"
        f" where e.kind in ('done','failed') and e.at > {window} and {scope} group by 1, 2 order by 1",
        p,
    ).fetchall()
    fetched = conn.execute(
        f"select date_trunc('hour', staged_at) as hour, sum(received_instances)::bigint as instances,"
        f" count(*) as series from retrievals where staged_at > {window} group by 1 order by 1",
        p,
    ).fetchall()
    per_flow = conn.execute(
        f"""
        select j.flow,
               count(*) filter (where j.state = 'done') as done,
               count(*) filter (where j.state = 'failed') as failed,
               count(*) filter (where j.state not in ('done','failed','cancelled')) as active,
               percentile_cont(0.5) within group (order by extract(epoch from j.updated_at - j.created_at))
                   filter (where j.state = 'done') as median_job_s,
               (select percentile_cont(0.5) within group (order by extract(epoch from r.finished_at - r.started_at))
                  from runs r join jobs j2 on j2.id = r.job_id
                 where j2.flow = j.flow and r.state = 'succeeded' and r.finished_at > {window}) as median_run_s
        from jobs j where j.updated_at > {window} and {scope} group by j.flow order by j.flow
        """,
        p,
    ).fetchall()
    waiting = conn.execute(
        f"select coalesce(extract(epoch from now() - min(j.created_at)), 0)::float as oldest_s from jobs j"
        f" where j.state in ('queued','retrieving','ready') and {scope}",
        p,
    ).fetchone()
    cache = conn.execute(
        "select coalesce(sum(bytes), 0)::bigint as bytes, count(*) as series from retrievals where state = 'staged'"
    ).fetchone()
    return {
        "hours": hours,
        "now": conn.execute("select now() as now").fetchone()["now"],
        "jobs": state_counts(conn, flows),
        "finished_per_hour": finished,
        "fetched_per_hour": fetched,
        "flows": per_flow,
        "oldest_waiting_s": waiting["oldest_s"],
        "cache": cache,
        "status": get_status(conn),
    }


# --- gateway: resolve -------------------------------------------------------------


def claim_job_to_resolve(conn: Conn, owner: str, lease_s: float, interactive_priority: int, batch_ok: bool) -> Row | None:
    return conn.execute(
        """
        update jobs set lease_owner = %(owner)s,
                        lease_expires_at = now() + make_interval(secs => %(lease)s),
                        attempts = attempts + 1, updated_at = now()
        where id = (
            select j.id from jobs j left join batches b on b.id = j.batch_id
            where j.state = 'queued' and j.not_before <= now()
              and (j.lease_expires_at is null or j.lease_expires_at < now())
              and not coalesce(b.paused, false)
              and (j.priority >= %(interactive)s or %(batch_ok)s)
            order by j.priority desc, j.created_at
            for update of j skip locked limit 1)
        returning *
        """,
        {"owner": owner, "lease": lease_s, "interactive": interactive_priority, "batch_ok": batch_ok},
    ).fetchone()


_PROMOTE_READY = """
    update jobs set state = 'ready', attempts = 0, not_before = now(), updated_at = now()
    where state = 'retrieving' and {filter}
      and not exists (
          select 1 from job_series js join retrievals r using (series_instance_uid)
          where js.job_id = jobs.id and r.state <> 'staged')
    returning id
"""


def resolve_job(
    conn: Conn, job_id: uuid.UUID, owner: str, study_instance_uid: str, flow_version: str, series: list[SeriesInfo]
) -> bool:
    """queued -> retrieving (or straight to ready when every series is already cached).

    Returns False when the job is no longer ours (cancelled, or the lease was lost).
    """
    with conn.transaction():
        cur = conn.execute(
            "update jobs set state = 'retrieving', study_instance_uid = %s, flow_version = %s,"
            " attempts = 0, lease_owner = null, lease_expires_at = null, error = null, updated_at = now()"
            " where id = %s and state = 'queued' and lease_owner = %s",
            (study_instance_uid, flow_version, job_id, owner),
        )
        if cur.rowcount == 0:
            return False
        conn.execute("delete from job_series where job_id = %s", (job_id,))  # left from before a retry
        # Sorted to take row locks in a consistent order. The upsert always locks the
        # retrieval row, which is what makes cache eviction safe (see evict_retrieval).
        for s in sorted(series, key=lambda s: s.series_instance_uid):
            conn.execute(
                """
                insert into retrievals (series_instance_uid, study_instance_uid, expected_instances)
                values (%s, %s, %s)
                on conflict (series_instance_uid) do update set
                    state = case when retrievals.state in ('failed','evicted') then 'pending' else retrievals.state end,
                    attempts = case when retrievals.state in ('failed','evicted') then 0 else retrievals.attempts end,
                    not_before = case when retrievals.state in ('failed','evicted') then now() else retrievals.not_before end,
                    error = case when retrievals.state in ('failed','evicted') then null else retrievals.error end,
                    expected_instances = coalesce(excluded.expected_instances, retrievals.expected_instances),
                    last_used_at = now()
                """,
                (s.series_instance_uid, s.study_instance_uid, s.instances),
            )
            conn.execute(
                "insert into job_series (job_id, series_instance_uid) values (%s, %s) on conflict do nothing",
                (job_id, s.series_instance_uid),
            )
        log_event(conn, job_id, "resolved", study=study_instance_uid, series=[s.series_instance_uid for s in series])
        promoted = conn.execute(_PROMOTE_READY.format(filter="id = %s"), (job_id,)).fetchall()
        if promoted:
            log_event(conn, job_id, "ready", cached=True)
    return True


def fail_job(conn: Conn, job_id: uuid.UUID, error: str, owner: str | None = None) -> bool:
    with conn.transaction():
        cur = conn.execute(
            "update jobs set state = 'failed', error = %(error)s, lease_owner = null, lease_expires_at = null,"
            " updated_at = now()"
            " where id = %(id)s and state not in ('done','failed','cancelled')"
            " and (%(owner)s::text is null or lease_owner = %(owner)s)",
            {"error": error, "id": job_id, "owner": owner},
        )
        if cur.rowcount:
            log_event(conn, job_id, "failed", error=error)
        return cur.rowcount == 1


def retry_resolve(conn: Conn, job_id: uuid.UUID, owner: str, error: str, delay_s: float) -> None:
    conn.execute(
        "update jobs set lease_owner = null, lease_expires_at = null, error = %s,"
        " not_before = now() + make_interval(secs => %s), updated_at = now()"
        " where id = %s and state = 'queued' and lease_owner = %s",
        (error, delay_s, job_id, owner),
    )


# --- gateway: retrieve ----------------------------------------------------------------


def claim_retrieval(conn: Conn, owner: str, lease_s: float, interactive_priority: int, batch_ok: bool) -> Row | None:
    """Claim the next pending series that an active, unpaused job is waiting for.

    Priority is derived from the waiting jobs, so cancelling a one-off job also
    removes its urgency. Series of the oldest waiting job go first so that jobs
    complete one at a time instead of all being half-fetched.
    """
    return conn.execute(
        """
        with next as (
            select r.series_instance_uid, w.priority
            from retrievals r
            join lateral (
                select max(j.priority) as priority, min(j.created_at) as oldest
                from job_series js
                join jobs j on j.id = js.job_id
                left join batches b on b.id = j.batch_id
                where js.series_instance_uid = r.series_instance_uid
                  and j.state = 'retrieving' and not coalesce(b.paused, false)
            ) w on w.priority is not null
            where r.state = 'pending' and r.not_before <= now()
              and (r.lease_expires_at is null or r.lease_expires_at < now())
              and (w.priority >= %(interactive)s or %(batch_ok)s)
            order by w.priority desc, w.oldest, r.series_instance_uid
            for update of r skip locked limit 1)
        update retrievals r set lease_owner = %(owner)s,
                                lease_expires_at = now() + make_interval(secs => %(lease)s),
                                attempts = r.attempts + 1
        from next where r.series_instance_uid = next.series_instance_uid
        returning r.*, next.priority as priority
        """,
        {"owner": owner, "lease": lease_s, "interactive": interactive_priority, "batch_ok": batch_ok},
    ).fetchone()


def mark_staged(conn: Conn, series_instance_uid: str, owner: str, received: int, nbytes: int) -> list[uuid.UUID]:
    """pending -> staged, then promote every job whose series are now all staged. Returns promoted job ids."""
    with conn.transaction():
        cur = conn.execute(
            "update retrievals set state = 'staged', received_instances = %s, bytes = %s, error = null,"
            " staged_at = now(), last_used_at = now(), lease_owner = null, lease_expires_at = null"
            " where series_instance_uid = %s and state = 'pending' and lease_owner = %s",
            (received, nbytes, series_instance_uid, owner),
        )
        if cur.rowcount == 0:
            return []
        rows = conn.execute(
            _PROMOTE_READY.format(
                filter="id in (select job_id from job_series where series_instance_uid = %s)"
            ),
            (series_instance_uid,),
        ).fetchall()
        for row in rows:
            log_event(conn, row["id"], "ready")
        return [row["id"] for row in rows]


def retrieval_failed(
    conn: Conn, series_instance_uid: str, owner: str, error: str, max_attempts: int, delay_s: float
) -> bool:
    """Back off, or after max_attempts give up and fail the jobs waiting for this series. Returns True if final."""
    with conn.transaction():
        row = conn.execute(
            "select attempts from retrievals where series_instance_uid = %s and state = 'pending'"
            " and lease_owner = %s for update",
            (series_instance_uid, owner),
        ).fetchone()
        if row is None:
            return False
        final = row["attempts"] >= max_attempts
        conn.execute(
            "update retrievals set state = %s, error = %s, lease_owner = null, lease_expires_at = null,"
            " not_before = now() + make_interval(secs => %s) where series_instance_uid = %s",
            ("failed" if final else "pending", error, delay_s, series_instance_uid),
        )
        if final:
            jobs = conn.execute(
                "update jobs set state = 'failed', error = %s, updated_at = now()"
                " where state = 'retrieving'"
                " and id in (select job_id from job_series where series_instance_uid = %s) returning id",
                (f"retrieval of series {series_instance_uid} failed: {error}", series_instance_uid),
            ).fetchall()
            for job in jobs:
                log_event(conn, job["id"], "failed", error=error, series=series_instance_uid)
        return final


# --- executor ---------------------------------------------------------------------------


def claim_job_to_run(conn: Conn, owner: str, lease_s: float) -> Row | None:
    """Claim a ready job, or take over a running job whose executor stopped heartbeating.

    The returned row has `prev_state`: 'ready' for fresh work, 'running' for a takeover.
    """
    return conn.execute(
        """
        with next as (
            select j.id, j.state as prev_state
            from jobs j left join batches b on b.id = j.batch_id
            where (j.state = 'running' and j.lease_expires_at < now())
               or (j.state = 'ready' and j.not_before <= now() and not coalesce(b.paused, false))
            order by (j.state = 'running') desc, j.priority desc, j.created_at
            for update of j skip locked limit 1)
        update jobs j set state = 'running', lease_owner = %(owner)s,
                          lease_expires_at = now() + make_interval(secs => %(lease)s), updated_at = now()
        from next where j.id = next.id
        returning j.*, next.prev_state
        """,
        {"owner": owner, "lease": lease_s},
    ).fetchone()


def renew_job_lease(conn: Conn, job_id: uuid.UUID, owner: str, lease_s: float) -> bool:
    """Heartbeat. False means the job was cancelled or taken over: stop working on it."""
    cur = conn.execute(
        "update jobs set lease_expires_at = now() + make_interval(secs => %s)"
        " where id = %s and state = 'running' and lease_owner = %s",
        (lease_s, job_id, owner),
    )
    return cur.rowcount == 1


def job_series_uids(conn: Conn, job_id: uuid.UUID) -> list[str]:
    rows = conn.execute(
        "select series_instance_uid from job_series where job_id = %s order by 1", (job_id,)
    ).fetchall()
    return [row["series_instance_uid"] for row in rows]


def latest_run(conn: Conn, job_id: uuid.UUID) -> Row | None:
    return conn.execute(
        "select * from runs where job_id = %s order by attempt desc limit 1", (job_id,)
    ).fetchone()


def create_run(conn: Conn, job_id: uuid.UUID, image: str) -> Row:
    with conn.transaction():
        run = conn.execute(
            "insert into runs (id, job_id, attempt, image)"
            " values (%s, %s, (select coalesce(max(attempt), 0) + 1 from runs where job_id = %s), %s) returning *",
            (uuid.uuid4(), job_id, job_id, image),
        ).fetchone()
        conn.execute(
            "update retrievals set last_used_at = now() where series_instance_uid in"
            " (select series_instance_uid from job_series where job_id = %s)",
            (job_id,),
        )
        log_event(conn, job_id, "run_started", run=str(run["id"]), attempt=run["attempt"], image=image)
    return run


def set_run_image_digest(conn: Conn, run_id: uuid.UUID, digest: str | None) -> None:
    conn.execute("update runs set image_digest = %s where id = %s", (digest, run_id))


def job_runs(conn: Conn, job_id: uuid.UUID) -> list[Row]:
    return conn.execute(
        "select id, attempt, state, image, image_digest, exit_code, error, started_at, finished_at"
        " from runs where job_id = %s order by attempt",
        (job_id,),
    ).fetchall()


def run_manifest(conn: Conn, run_id: uuid.UUID) -> dict[str, Any]:
    """Everything needed to say later what produced a result: inputs, flow version and exact image."""
    row = conn.execute(
        "select j.id as job_id, j.accession_number, j.study_instance_uid, j.flow, j.flow_version, j.source,"
        " j.requested_by, r.id as run_id, r.attempt, r.image, r.image_digest, r.started_at"
        " from runs r join jobs j on j.id = r.job_id where r.id = %s",
        (run_id,),
    ).fetchone()
    series = conn.execute(
        "select r.series_instance_uid, r.study_instance_uid, r.received_instances as instances"
        " from job_series js join retrievals r using (series_instance_uid) where js.job_id = %s order by 1",
        (row["job_id"],),
    ).fetchall()
    return {**row, "input_series": series}


def promote_ready_jobs(conn: Conn) -> int:
    """Safety net: move any job to 'ready' whose series are all staged but that is still 'retrieving'."""
    with conn.transaction():
        rows = conn.execute(_PROMOTE_READY.format(filter="true")).fetchall()
        for row in rows:
            log_event(conn, row["id"], "ready", via="reconciler")
        return len(rows)


def batch_jobs(conn: Conn, batch_id: uuid.UUID) -> list[Row]:
    return conn.execute(
        "select accession_number, state, error, study_instance_uid, id, updated_at from jobs"
        " where batch_id = %s order by created_at, accession_number",
        (batch_id,),
    ).fetchall()


def finish_run(conn: Conn, run_id: uuid.UUID, state: str, exit_code: int | None = None, error: str | None = None) -> None:
    conn.execute(
        "update runs set state = %s, exit_code = %s, error = %s, finished_at = now()"
        " where id = %s and state = 'running'",
        (state, exit_code, error, run_id),
    )


def run_succeeded(conn: Conn, job_id: uuid.UUID, run_id: uuid.UUID, owner: str, destinations: list[dict]) -> bool:
    """running -> sending (one delivery per destination), or -> done when there is nowhere to send."""
    with conn.transaction():
        cur = conn.execute(
            "update jobs set state = %s, error = null, lease_owner = null, lease_expires_at = null, updated_at = now()"
            " where id = %s and state = 'running' and lease_owner = %s",
            ("sending" if destinations else "done", job_id, owner),
        )
        if cur.rowcount == 0:
            return False
        finish_run(conn, run_id, "succeeded", 0)
        for dest in destinations:
            conn.execute(
                "insert into deliveries (id, job_id, run_id, destination) values (%s, %s, %s, %s)",
                (uuid.uuid4(), job_id, run_id, Jsonb(dest)),
            )
        log_event(conn, job_id, "run_succeeded", run=str(run_id))
        if not destinations:
            log_event(conn, job_id, "done")
    return True


def run_failed(
    conn: Conn,
    job_id: uuid.UUID,
    run_id: uuid.UUID,
    owner: str,
    error: str,
    exit_code: int | None,
    retry: bool,
    delay_s: float,
    run_state: str = "failed",
) -> None:
    """running -> ready (retry after a delay) or -> failed."""
    with conn.transaction():
        finish_run(conn, run_id, run_state, exit_code, error)
        cur = conn.execute(
            "update jobs set state = %s, error = %s, lease_owner = null, lease_expires_at = null,"
            " not_before = now() + make_interval(secs => %s), updated_at = now()"
            " where id = %s and state = 'running' and lease_owner = %s",
            ("ready" if retry else "failed", error, delay_s, job_id, owner),
        )
        if cur.rowcount:
            log_event(conn, job_id, "run_failed", run=str(run_id), error=error, exit_code=exit_code, retry=retry)
            if not retry:
                log_event(conn, job_id, "failed", error=error)


def delete_run(conn: Conn, run_id: uuid.UUID) -> None:
    conn.execute("delete from runs where id = %s", (run_id,))


def count_runs(conn: Conn, job_id: uuid.UUID) -> int:
    """Run attempts since the job was created or last retried by hand."""
    return conn.execute(
        "select count(*) as n from runs r join jobs j on j.id = r.job_id"
        " where r.job_id = %s and r.started_at >= coalesce(j.retried_at, '-infinity')",
        (job_id,),
    ).fetchone()["n"]


def requeue_for_retrieval(conn: Conn, job_id: uuid.UUID, owner: str, missing: list[str]) -> None:
    """Cache files were missing when the run was prepared: fetch them again. running -> retrieving."""
    with conn.transaction():
        cur = conn.execute(
            "update jobs set state = 'retrieving', lease_owner = null, lease_expires_at = null, updated_at = now()"
            " where id = %s and state = 'running' and lease_owner = %s",
            (job_id, owner),
        )
        if cur.rowcount == 0:
            return
        conn.execute(
            "update retrievals set state = 'pending', attempts = 0, not_before = now(), error = null"
            " where series_instance_uid = any(%s) and state in ('staged','evicted','failed')",
            (missing,),
        )
        log_event(conn, job_id, "cache_miss", series=missing)


# --- sender -------------------------------------------------------------------------------


def claim_delivery(conn: Conn, owner: str, lease_s: float) -> Row | None:
    return conn.execute(
        """
        update deliveries set lease_owner = %(owner)s,
                              lease_expires_at = now() + make_interval(secs => %(lease)s),
                              attempts = attempts + 1
        where id = (
            select d.id from deliveries d join jobs j on j.id = d.job_id
            where d.state = 'pending' and d.not_before <= now() and j.state = 'sending'
              and (d.lease_expires_at is null or d.lease_expires_at < now())
            order by j.priority desc, d.created_at
            for update of d skip locked limit 1)
        returning *
        """,
        {"owner": owner, "lease": lease_s},
    ).fetchone()


def delivery_done(conn: Conn, delivery_id: uuid.UUID, owner: str) -> None:
    """Mark delivered; the job is done once no delivery is left pending."""
    with conn.transaction():
        row = conn.execute(
            "update deliveries set state = 'done', error = null, lease_owner = null, lease_expires_at = null"
            " where id = %s and state = 'pending' and lease_owner = %s returning job_id",
            (delivery_id, owner),
        ).fetchone()
        if row is None:
            return
        job_id = row["job_id"]
        # Lock the job so two senders finishing the last two deliveries cannot both miss "all done".
        conn.execute("select 1 from jobs where id = %s for update", (job_id,))
        open_left = conn.execute(
            "select count(*) as n from deliveries where job_id = %s and state <> 'done'", (job_id,)
        ).fetchone()["n"]
        if open_left == 0:
            cur = conn.execute(
                "update jobs set state = 'done', updated_at = now() where id = %s and state = 'sending'", (job_id,)
            )
            if cur.rowcount:
                log_event(conn, job_id, "done")


def delivery_failed(conn: Conn, delivery_id: uuid.UUID, owner: str, error: str, max_attempts: int, delay_s: float) -> None:
    with conn.transaction():
        row = conn.execute(
            "select job_id, attempts from deliveries where id = %s and state = 'pending' and lease_owner = %s for update",
            (delivery_id, owner),
        ).fetchone()
        if row is None:
            return
        final = row["attempts"] >= max_attempts
        conn.execute(
            "update deliveries set state = %s, error = %s, lease_owner = null, lease_expires_at = null,"
            " not_before = now() + make_interval(secs => %s) where id = %s",
            ("failed" if final else "pending", error, delay_s, delivery_id),
        )
        if final:
            fail_job(conn, row["job_id"], f"delivery failed: {error}")


# --- janitor --------------------------------------------------------------------------------


def eviction_candidates(conn: Conn, older_than_hours: float) -> list[str]:
    """Staged series, least recently used first, that were not used within the given time."""
    rows = conn.execute(
        "select series_instance_uid from retrievals where state = 'staged'"
        " and coalesce(last_used_at, staged_at) < now() - make_interval(secs => %s * 3600.0)"
        " order by coalesce(last_used_at, staged_at)",
        (older_than_hours,),
    ).fetchall()
    return [row["series_instance_uid"] for row in rows]


def evict_retrieval(conn: Conn, series_instance_uid: str, remove_files, keep_failed_hours: float = 0) -> bool:
    """staged -> evicted unless an active job still needs the series.

    The row lock is taken first and the reference check runs as a later statement,
    so a concurrent resolve_job (which locks the same row before linking a job to
    it) is either fully visible here or sees 'evicted' and re-queues the retrieval.
    `remove_files` runs while the lock is held.
    """
    with conn.transaction():
        row = conn.execute(
            "select 1 from retrievals where series_instance_uid = %s and state = 'staged' for update",
            (series_instance_uid,),
        ).fetchone()
        if row is None:
            return False
        # In use: needed by an active job, by a retried job waiting to be resolved again, or
        # by a recently failed job whose input is being kept for inspection and retry.
        in_use = conn.execute(
            "select 1 from job_series js join jobs j on j.id = js.job_id"
            " where js.series_instance_uid = %s and (j.state = any(%s) or (j.state = 'failed'"
            " and j.updated_at > now() - make_interval(secs => %s * 3600.0))) limit 1",
            (series_instance_uid, [*NEEDS_CACHE, "queued"], keep_failed_hours),
        ).fetchone()
        if in_use:
            return False
        conn.execute(
            "update retrievals set state = 'evicted' where series_instance_uid = %s", (series_instance_uid,)
        )
        remove_files()
    return True


def purgeable_run(conn: Conn, run_id: uuid.UUID, older_than_hours: float, keep_failed_hours: float = 0) -> bool:
    """True when the run's files can go: unknown run, or its job finished long enough ago.

    Runs of failed jobs are kept for at least keep_failed_hours.
    """
    row = conn.execute(
        "select j.state, j.updated_at < now() - make_interval(secs => 3600.0 *"
        " (case when j.state = 'failed' then greatest(%s, %s) else %s end)) as old"
        " from runs r join jobs j on j.id = r.job_id where r.id = %s",
        (older_than_hours, keep_failed_hours, older_than_hours, run_id),
    ).fetchone()
    if row is None:
        return True
    return row["state"] in TERMINAL and row["old"]


# --- push ingress ---------------------------------------------------------------------------


def touch_incoming(conn: Conn, study_instance_uid: str, accession_number: str | None, calling_ae_title: str) -> None:
    conn.execute(
        "insert into incoming_studies (study_instance_uid, accession_number, calling_ae_title) values (%s, %s, %s)"
        " on conflict (study_instance_uid) do update set last_received_at = now(),"
        " accession_number = coalesce(nullif(excluded.accession_number, ''), incoming_studies.accession_number)",
        (study_instance_uid, accession_number, calling_ae_title),
    )


def claim_stable_incoming(conn: Conn, stable_s: float) -> Row | None:
    """Lock a study that has received nothing for stable_s. Call inside a transaction; the lock ends with it."""
    return conn.execute(
        "select * from incoming_studies where last_received_at < now() - make_interval(secs => %s)"
        " order by last_received_at for update skip locked limit 1",
        (stable_s,),
    ).fetchone()


def delete_incoming(conn: Conn, study_instance_uid: str) -> None:
    conn.execute("delete from incoming_studies where study_instance_uid = %s", (study_instance_uid,))


def create_pushed_job(
    conn: Conn,
    accession_number: str,
    flow: str,
    flow_version: str,
    priority: int,
    study_instance_uid: str,
    series_uids: list[str],
    stage_files,
    requested_by: str | None = None,
) -> uuid.UUID:
    """Create a job that starts at 'ready': its series were pushed to us instead of pulled.

    `stage_files(series_uid) -> (instances, bytes)` puts the files into the cache. It is
    called while the retrieval row is locked, so it cannot race with eviction.
    """
    job_id = uuid.uuid4()
    with conn.transaction():
        conn.execute(
            "insert into jobs (id, accession_number, flow, flow_version, priority, state, study_instance_uid,"
            " requested_by, source) values (%s, %s, %s, %s, %s, 'ready', %s, %s, 'push')",
            (job_id, accession_number, flow, flow_version, priority, study_instance_uid, requested_by),
        )
        for series_uid in sorted(series_uids):
            conn.execute(
                "insert into retrievals (series_instance_uid, study_instance_uid) values (%s, %s)"
                " on conflict (series_instance_uid) do update set last_used_at = now()",
                (series_uid, study_instance_uid),
            )
            instances, nbytes = stage_files(series_uid)
            conn.execute(
                "update retrievals set state = 'staged', received_instances = %s, bytes = %s, error = null,"
                " staged_at = now(), last_used_at = now(), lease_owner = null, lease_expires_at = null"
                " where series_instance_uid = %s",
                (instances, nbytes, series_uid),
            )
            conn.execute("insert into job_series (job_id, series_instance_uid) values (%s, %s)", (job_id, series_uid))
            # Pull jobs that were waiting for the same series can go ahead too.
            waiting = conn.execute(
                _PROMOTE_READY.format(
                    filter="id in (select job_id from job_series where series_instance_uid = %s)"
                ),
                (series_uid,),
            ).fetchall()
            for row in waiting:
                log_event(conn, row["id"], "ready", via="push")
        log_event(
            conn, job_id, "received", accession=accession_number, flow=flow, study=study_instance_uid,
            series=series_uids, by=requested_by,
        )
        log_event(conn, job_id, "ready")
    return job_id


# --- status board ---------------------------------------------------------------------------


def set_status(conn: Conn, key: str, **value: Any) -> None:
    conn.execute(
        "insert into status (key, value) values (%s, %s)"
        " on conflict (key) do update set value = excluded.value, updated_at = now()",
        (key, Jsonb(value)),
    )


def get_status(conn: Conn) -> dict[str, Row]:
    rows = conn.execute(
        "select key, value, extract(epoch from now() - updated_at)::float as age_s from status"
    ).fetchall()
    return {row["key"]: {**row["value"], "age_s": row["age_s"]} for row in rows}


def metrics(conn: Conn) -> dict[str, Any]:
    def by_state(table: str) -> dict[str, int]:
        rows = conn.execute(f"select state, count(*) as n from {table} group by state").fetchall()  # noqa: S608
        return {row["state"]: row["n"] for row in rows}

    extra = conn.execute(
        "select"
        " (select coalesce(sum(bytes), 0) from retrievals where state = 'staged')::bigint as cache_bytes,"
        " (select coalesce(extract(epoch from now() - min(created_at)), 0) from jobs"
        "   where state not in ('done','failed','cancelled'))::float as oldest_active_job_s,"
        " (select count(*) from incoming_studies) as incoming_studies"
    ).fetchone()
    return {
        "jobs": by_state("jobs"),
        "retrievals": by_state("retrievals"),
        "deliveries": by_state("deliveries"),
        "status": get_status(conn),
        **extra,
    }


# --- users, sessions and API tokens -------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_USER_COLUMNS = "id, username, role, grants, denies, allowed_flows, disabled, created_at, last_login_at"


def create_user(
    conn: Conn,
    username: str,
    role: str,
    password_hash: str | None = None,
    grants: list[str] | None = None,
    denies: list[str] | None = None,
    allowed_flows: list[str] | None = None,
    by: str | None = None,
) -> Row:
    with conn.transaction():
        user = conn.execute(
            "insert into users (id, username, role, password_hash, grants, denies, allowed_flows)"
            f" values (%s, %s, %s, %s, %s, %s, %s) returning {_USER_COLUMNS}",
            (uuid.uuid4(), username, role, password_hash, grants or [], denies or [], allowed_flows),
        ).fetchone()
        log_event(conn, None, "user_created", username=username, role=role, by=by)
    return user


def get_user(conn: Conn, username: str) -> Row | None:
    return conn.execute(f"select {_USER_COLUMNS} from users where username = %s", (username,)).fetchone()


def list_users(conn: Conn) -> list[Row]:
    return conn.execute(
        f"select {_USER_COLUMNS}, password_hash is not null as has_password,"
        " (select count(*) from api_tokens t where t.user_id = users.id and t.revoked_at is null) as tokens"
        " from users order by username"
    ).fetchall()


def users_exist(conn: Conn) -> bool:
    return conn.execute("select 1 from users limit 1").fetchone() is not None


def update_user(conn: Conn, username: str, changes: dict[str, Any], by: str | None = None) -> Row | None:
    """Change role, grants, denies, allowed_flows, disabled or password_hash.

    Refuses (ValueError) to leave the system without an enabled admin.
    """
    allowed = {"role", "grants", "denies", "allowed_flows", "disabled", "password_hash"}
    changes = {k: v for k, v in changes.items() if k in allowed}
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(727002)")  # serialise the last-admin check
        current = conn.execute("select * from users where username = %s for update", (username,)).fetchone()
        if current is None:
            return None
        if changes:
            sets = ", ".join(f"{k} = %({k})s" for k in changes)
            conn.execute(f"update users set {sets} where username = %(username)s", {**changes, "username": username})
        _require_an_admin(conn)
        if changes.get("disabled") or "password_hash" in changes:
            conn.execute("delete from sessions where user_id = %s", (current["id"],))  # sign them out everywhere
        logged = {k: v for k, v in changes.items() if k != "password_hash"}
        if "password_hash" in changes:
            logged["password"] = "changed"
        log_event(conn, None, "user_changed", username=username, changes=logged, by=by)
    return get_user(conn, username)


def delete_user(conn: Conn, username: str, by: str | None = None) -> bool:
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(727002)")
        cur = conn.execute("delete from users where username = %s", (username,))
        if cur.rowcount == 0:
            return False
        _require_an_admin(conn)
        log_event(conn, None, "user_deleted", username=username, by=by)
    return True


def _require_an_admin(conn: Conn) -> None:
    """Once accounts exist there must always be an enabled admin who can sign in, or nobody could manage users."""
    if not users_exist(conn):
        return
    ok = conn.execute(
        "select 1 from users u where not disabled and role = 'admin' and not ('users.manage' = any(denies))"
        " and (password_hash is not null or exists"
        "      (select 1 from api_tokens t where t.user_id = u.id and t.revoked_at is null)) limit 1"
    ).fetchone()
    if ok is None:
        raise ValueError("this would leave no enabled administrator")


def check_login(conn: Conn, username: str, verify, max_failures: int = 10, lock_minutes: int = 15) -> Row | None:
    """Return the user when verify(password_hash) is true. Locks the account after repeated failures."""
    with conn.transaction():
        row = conn.execute("select * from users where username = %s for update", (username,)).fetchone()
        if row is None:
            verify(None)  # spend the same time as for a real user
            return None
        locked = conn.execute("select %s > now() as locked", (row["locked_until"],)).fetchone()["locked"]
        if row["disabled"] or locked or not verify(row["password_hash"]):
            if not row["disabled"] and not locked:
                conn.execute(
                    "update users set failed_logins = failed_logins + 1,"
                    " locked_until = case when failed_logins + 1 >= %s then now() + make_interval(mins => %s) end"
                    " where id = %s",
                    (max_failures, lock_minutes, row["id"]),
                )
            return None
        conn.execute(
            "update users set failed_logins = 0, locked_until = null, last_login_at = now() where id = %s",
            (row["id"],),
        )
    return get_user(conn, username)


def create_session(conn: Conn, user_id: uuid.UUID, hours: float) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("delete from sessions where expires_at < now()")
    conn.execute(
        "insert into sessions (token_hash, user_id, expires_at) values (%s, %s, now() + make_interval(secs => %s * 3600.0))",
        (_token_hash(token), user_id, hours),
    )
    return token


def user_for_session(conn: Conn, token: str) -> Row | None:
    return conn.execute(
        f"select {', '.join('u.' + c.strip() for c in _USER_COLUMNS.split(','))} from sessions s"
        " join users u on u.id = s.user_id where s.token_hash = %s and s.expires_at > now() and not u.disabled",
        (_token_hash(token),),
    ).fetchone()


def delete_session(conn: Conn, token: str) -> None:
    conn.execute("delete from sessions where token_hash = %s", (_token_hash(token),))


def create_token(conn: Conn, username: str, name: str | None = None, by: str | None = None) -> str:
    """Create an API token for a user and return it. This is the only time the token itself is available."""
    with conn.transaction():
        user = get_user(conn, username)
        if user is None:
            raise ValueError(f"no such user: {username}")
        token = "rf_" + secrets.token_urlsafe(32)
        name = name or f"{username}-{secrets.token_hex(3)}"
        conn.execute(
            "insert into api_tokens (id, name, user_id, token_hash) values (%s, %s, %s, %s)",
            (uuid.uuid4(), name, user["id"], _token_hash(token)),
        )
        log_event(conn, None, "token_created", name=name, username=username, by=by)
    return token


def user_for_token(conn: Conn, token: str) -> Row | None:
    return conn.execute(
        f"select {', '.join('u.' + c.strip() for c in _USER_COLUMNS.split(','))} from api_tokens t"
        " join users u on u.id = t.user_id where t.token_hash = %s and t.revoked_at is null and not u.disabled",
        (_token_hash(token),),
    ).fetchone()


def list_tokens(conn: Conn, username: str | None = None) -> list[Row]:
    return conn.execute(
        "select t.name, u.username, t.created_at, t.revoked_at from api_tokens t join users u on u.id = t.user_id"
        " where %(u)s::text is null or u.username = %(u)s order by t.created_at",
        {"u": username},
    ).fetchall()


def revoke_token(conn: Conn, name: str, username: str | None = None, by: str | None = None) -> bool:
    """Revoke a token by name; with username, only if it belongs to that user."""
    with conn.transaction():
        cur = conn.execute(
            "update api_tokens t set revoked_at = now() from users u where u.id = t.user_id and t.name = %(n)s"
            " and t.revoked_at is null and (%(u)s::text is null or u.username = %(u)s)",
            {"n": name, "u": username},
        )
        if cur.rowcount:
            log_event(conn, None, "token_revoked", name=name, by=by)
        return cur.rowcount == 1


def audit_log(conn: Conn, limit: int = 200, flows: list[str] | None = None) -> list[Row]:
    """Latest events, newest first. With `flows`, job events of other flows are left out."""
    return conn.execute(
        "select e.id, e.at, e.job_id, e.kind, e.detail from events e left join jobs j on j.id = e.job_id"
        " where %(f)s::text[] is null or e.job_id is null or j.flow = any(%(f)s)"
        " order by e.id desc limit %(limit)s",
        {"f": flows, "limit": limit},
    ).fetchall()


# --- notifications ---------------------------------------------------------------------------


def enqueue_notification(conn: Conn, kind: str, ref: uuid.UUID | None = None, **detail: Any) -> None:
    conn.execute("insert into notifications (kind, ref, detail) values (%s, %s, %s)", (kind, ref, Jsonb(detail)))


def claim_notification(conn: Conn, owner: str, lease_s: float) -> Row | None:
    return conn.execute(
        """
        update notifications set lease_owner = %(owner)s,
                                 lease_expires_at = now() + make_interval(secs => %(lease)s),
                                 attempts = attempts + 1
        where id = (
            select id from notifications
            where state = 'pending' and not_before <= now()
              and (lease_expires_at is null or lease_expires_at < now())
            order by id for update skip locked limit 1)
        returning *
        """,
        {"owner": owner, "lease": lease_s},
    ).fetchone()


def finish_notification(conn: Conn, notification_id: int, state: str, error: str | None = None) -> None:
    conn.execute(
        "update notifications set state = %s, error = %s, lease_owner = null, lease_expires_at = null where id = %s",
        (state, error, notification_id),
    )


def retry_notification(conn: Conn, notification_id: int, error: str, delay_s: float) -> None:
    conn.execute(
        "update notifications set error = %s, lease_owner = null, lease_expires_at = null,"
        " not_before = now() + make_interval(secs => %s) where id = %s",
        (error, delay_s, notification_id),
    )


# --- leadership ------------------------------------------------------------------------------

LEADER_KEYS = {"gateway": 727101, "ingress": 727102}


def try_lead(conn: Conn, role: str) -> bool:
    """Become the single active instance of a role, or learn that another one is.

    A Postgres session-level advisory lock: it is held for as long as this connection
    lives and is released by the database the moment the connection ends, so a standby
    takes over without any timeout to tune. Calling it again while leading returns True.
    """
    return conn.execute("select pg_try_advisory_lock(%s) as ok", (LEADER_KEYS[role],)).fetchone()["ok"]
