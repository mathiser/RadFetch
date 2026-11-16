"""All state transitions. Every function is one atomic unit against Postgres.

Claims use leases instead of intermediate states: a row whose lease has expired
is simply claimable again, so a crashed worker needs no cleanup.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import Any
from collections.abc import Iterable

import psycopg
from psycopg.types.json import Jsonb

from radfetch import auth
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

    When only delivery failed, the job goes back to 'sending' and nothing is run again.
    Otherwise it goes back to 'queued' with fresh attempt budgets and continues at the
    step that failed; steps that already succeeded are kept. full=True runs every step again.
    """
    with conn.transaction():
        job = conn.execute(
            "select id from jobs where id = %s and state in ('failed','cancelled') for update", (job_id,)
        ).fetchone()
        if job is None:
            return None
        resend = None
        if not full:
            # Deliveries exist only once every step has succeeded, so a failed one means only sending failed.
            resend = conn.execute(
                "select 1 from deliveries where job_id = %s and state = 'failed' limit 1", (job_id,)
            ).fetchone()
        if resend:
            conn.execute(
                "update deliveries set state = 'pending', attempts = 0, not_before = now(), error = null,"
                " lease_owner = null, lease_expires_at = null where job_id = %s and state = 'failed'",
                (job_id,),
            )
            state = "sending"
        else:
            conn.execute("delete from deliveries where job_id = %s", (job_id,))
            # job_series stays, so the cached series stay protected until the job is resolved again.
            state = "queued"
            if full:
                conn.execute("update jobs set rerun_from = now() where id = %s", (job_id,))
                conn.execute("delete from job_steps where job_id = %s", (job_id,))  # laid out afresh
            else:
                # Steps that succeeded keep their output; the failed or interrupted ones go again.
                conn.execute(
                    "update job_steps set state = 'pending', not_before = now(), lease_owner = null,"
                    " lease_expires_at = null where job_id = %s and state in ('failed', 'running')",
                    (job_id,),
                )
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


def staged_backlog(conn: Conn) -> int:
    """Jobs whose data is here and that are waiting for an executor: nothing started yet, or
    their next step could run but no executor has taken it.

    This is what batch retrieval is held against. Counting only jobs that have not started
    would miss jobs queueing for the GPU after a first CPU step, and the cache would fill
    with studies the GPU cannot get to.
    """
    return conn.execute(
        """
        select count(*) as n from jobs j where j.state in ('ready', 'running') and (
            not exists (select 1 from job_steps s where s.job_id = j.id)
            or (not exists (select 1 from job_steps s where s.job_id = j.id and s.state = 'running')
                and exists (select 1 from job_steps s where s.job_id = j.id and s.state = 'pending'
                            and not exists (select 1 from job_steps d where d.job_id = s.job_id
                                            and d.step = any(s.needs) and d.state <> 'succeeded'))))
        """
    ).fetchone()["n"]


def waiting_steps(conn: Conn) -> dict[str, int]:
    """Steps that could run right now if an executor of the right kind were free."""
    rows = conn.execute(
        "select s.gpu, count(*) as n from job_steps s join jobs j on j.id = s.job_id"
        " left join batches b on b.id = j.batch_id"
        " where s.state = 'pending' and j.state in ('ready', 'running') and not coalesce(b.paused, false)"
        " and not exists (select 1 from job_steps d where d.job_id = s.job_id"
        "                 and d.step = any(s.needs) and d.state <> 'succeeded')"
        " group by s.gpu"
    ).fetchall()
    counts = {"gpu": 0, "cpu": 0}
    for row in rows:
        counts["gpu" if row["gpu"] else "cpu"] = row["n"]
    return counts


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
               (select percentile_cont(0.5) within group (order by t.seconds) from (
                    select sum(extract(epoch from r.finished_at - r.started_at)) as seconds
                    from runs r join jobs j2 on j2.id = r.job_id
                    where j2.flow = j.flow and j2.state = 'done' and r.state = 'succeeded'
                      and j2.updated_at > {window}
                    group by r.job_id) t) as median_run_s
        from jobs j where j.updated_at > {window} and {scope} group by j.flow order by j.flow
        """,
        p,
    ).fetchall()
    per_step = conn.execute(
        f"""
        select j.flow, r.step,
               count(*) filter (where r.state = 'succeeded') as succeeded,
               count(*) filter (where r.state = 'failed') as failed,
               percentile_cont(0.5) within group (order by extract(epoch from r.finished_at - r.started_at))
                   filter (where r.state = 'succeeded') as median_s
        from runs r join jobs j on j.id = r.job_id
        where r.finished_at > {window} and {scope} group by j.flow, r.step order by j.flow, r.step
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
        "steps": per_step,
        "oldest_waiting_s": waiting["oldest_s"],
        "cache": cache,
        "status": get_status(conn),
        "waiting_steps": waiting_steps(conn),
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


def unplanned_jobs(conn: Conn, limit: int = 20) -> list[Row]:
    """Ready jobs whose steps have not been laid out yet."""
    return conn.execute(
        "select j.* from jobs j left join batches b on b.id = j.batch_id"
        " where j.state = 'ready' and not coalesce(b.paused, false)"
        " and not exists (select 1 from job_steps s where s.job_id = j.id)"
        " order by j.priority desc, j.created_at limit %s",
        (limit,),
    ).fetchall()


def plan_job(conn: Conn, job_id: uuid.UUID, steps: list[tuple[str, bool, list[str]]]) -> None:
    """Lay out a job's steps as (name, needs a GPU, steps it reads from). Safe to repeat."""
    with conn.transaction():
        for name, gpu, needs in steps:
            conn.execute(
                "insert into job_steps (job_id, step, gpu, needs) values (%s, %s, %s, %s) on conflict do nothing",
                (job_id, name, gpu, needs),
            )


def claim_step(conn: Conn, owner: str, lease_s: float, kind: str = "any") -> Row | None:
    """Claim one runnable step for an executor of the given kind ('gpu', 'cpu' or 'any').

    Runnable means: pending, every step it reads from has succeeded, and its job is ready
    or running and not paused. A running step whose executor stopped heartbeating is taken
    over first. The row has `prev_state`: 'pending' for fresh work, 'running' for a takeover.
    One-off jobs go before pushed jobs before batch jobs, at step level.
    """
    with conn.transaction():
        row = conn.execute(
            """
            with next as (
                select s.job_id, s.step, s.state as prev_state
                from job_steps s join jobs j on j.id = s.job_id left join batches b on b.id = j.batch_id
                where (%(kind)s = 'any' or s.gpu = (%(kind)s = 'gpu'))
                  and ((s.state = 'running' and s.lease_expires_at < now() and j.state = 'running')
                    or (s.state = 'pending' and s.not_before <= now() and j.state in ('ready', 'running')
                        and not coalesce(b.paused, false)
                        and not exists (select 1 from job_steps d where d.job_id = s.job_id
                                        and d.step = any(s.needs) and d.state <> 'succeeded')))
                order by (s.state = 'running') desc, j.priority desc, j.created_at, s.step
                for update of s skip locked limit 1)
            update job_steps s set state = 'running', lease_owner = %(owner)s,
                                   lease_expires_at = now() + make_interval(secs => %(lease)s)
            from next where s.job_id = next.job_id and s.step = next.step
            returning s.*, next.prev_state
            """,
            {"owner": owner, "lease": lease_s, "kind": kind},
        ).fetchone()
        if row is not None and row["prev_state"] == "pending":
            # The job counts as running from the moment its first step is taken.
            conn.execute(
                "update jobs set state = 'running', updated_at = now() where id = %s and state = 'ready'",
                (row["job_id"],),
            )
        return row


def renew_step_lease(conn: Conn, job_id: uuid.UUID, step: str, owner: str, lease_s: float) -> str:
    """Heartbeat. 'ok' to carry on; 'taken' if another executor now has the step (leave the
    container to it); 'stopped' if the job was cancelled or failed (stop the container)."""
    cur = conn.execute(
        "update job_steps s set lease_expires_at = now() + make_interval(secs => %s) from jobs j"
        " where j.id = s.job_id and j.state = 'running' and s.job_id = %s and s.step = %s"
        " and s.state = 'running' and s.lease_owner = %s",
        (lease_s, job_id, step, owner),
    )
    if cur.rowcount == 1:
        return "ok"
    row = conn.execute(
        "select j.state as job_state, s.state, s.lease_owner from job_steps s join jobs j on j.id = s.job_id"
        " where s.job_id = %s and s.step = %s",
        (job_id, step),
    ).fetchone()
    if row and row["job_state"] == "running" and row["state"] == "running" and row["lease_owner"] != owner:
        return "taken"
    return "stopped"


def release_step(conn: Conn, job_id: uuid.UUID, step: str, owner: str, delay_s: float = 0) -> None:
    """Give a claimed step back without it counting as an attempt."""
    conn.execute(
        "update job_steps set state = 'pending', lease_owner = null, lease_expires_at = null,"
        " not_before = now() + make_interval(secs => %s) where job_id = %s and step = %s and lease_owner = %s",
        (delay_s, job_id, step, owner),
    )


def reset_step(conn: Conn, job_id: uuid.UUID, step: str) -> None:
    """A succeeded step's output is gone from disk: it has to run again."""
    conn.execute(
        "update job_steps set state = 'pending', run_id = null, not_before = now()"
        " where job_id = %s and step = %s and state = 'succeeded'",
        (job_id, step),
    )


def job_steps(conn: Conn, job_id: uuid.UUID) -> list[Row]:
    return conn.execute(
        "select step, state, gpu, needs, run_id, lease_owner from job_steps where job_id = %s order by step",
        (job_id,),
    ).fetchall()


def jobs_to_finish(conn: Conn, idle_s: float) -> list[Row]:
    """Running jobs whose steps have all succeeded but which were not finished, e.g. because
    the executor died between its last step and queuing the deliveries."""
    return conn.execute(
        "select j.* from jobs j where j.state = 'running'"
        " and exists (select 1 from job_steps s where s.job_id = j.id)"
        " and not exists (select 1 from job_steps s where s.job_id = j.id and s.state <> 'succeeded')"
        " and (select max(r.finished_at) from runs r where r.job_id = j.id) < now() - make_interval(secs => %s)"
        " limit 5",
        (idle_s,),
    ).fetchall()


def job_series_uids(conn: Conn, job_id: uuid.UUID) -> list[str]:
    rows = conn.execute(
        "select series_instance_uid from job_series where job_id = %s order by 1", (job_id,)
    ).fetchall()
    return [row["series_instance_uid"] for row in rows]


def latest_run(conn: Conn, job_id: uuid.UUID, step: str | None = None) -> Row | None:
    return conn.execute(
        "select * from runs where job_id = %(job)s and (%(step)s::text is null or step = %(step)s)"
        " order by attempt desc limit 1",
        {"job": job_id, "step": step},
    ).fetchone()


def create_run(conn: Conn, job_id: uuid.UUID, image: str, step: str = "main") -> Row:
    with conn.transaction():
        run = conn.execute(
            "insert into runs (id, job_id, attempt, image, step)"
            " values (%s, %s, (select coalesce(max(attempt), 0) + 1 from runs where job_id = %s), %s, %s) returning *",
            (uuid.uuid4(), job_id, job_id, image, step),
        ).fetchone()
        conn.execute(
            "update retrievals set last_used_at = now() where series_instance_uid in"
            " (select series_instance_uid from job_series where job_id = %s)",
            (job_id,),
        )
        log_event(conn, job_id, "run_started", run=str(run["id"]), attempt=run["attempt"], image=image,
                  **({} if step == "main" else {"step": step}))
    return run


def step_succeeded(conn: Conn, job_id: uuid.UUID, step: str, run_id: uuid.UUID, owner: str) -> bool:
    """Record a step's success. Returns True when that was the job's last outstanding step,
    in which case the caller finishes the job.

    The job row is locked first, so of two executors completing the last two steps at the
    same moment exactly one is told the job is complete.
    """
    with conn.transaction():
        conn.execute("select 1 from jobs where id = %s for update", (job_id,))
        cur = conn.execute(
            "update job_steps set state = 'succeeded', run_id = %s, lease_owner = null, lease_expires_at = null"
            " where job_id = %s and step = %s and state = 'running' and lease_owner = %s",
            (run_id, job_id, step, owner),
        )
        if cur.rowcount == 0:
            return False
        finish_run(conn, run_id, "succeeded", 0)
        log_event(conn, job_id, "run_succeeded", run=str(run_id), **({} if step == "main" else {"step": step}))
        left = conn.execute(
            "select count(*) as n from job_steps where job_id = %s and state <> 'succeeded'", (job_id,)
        ).fetchone()["n"]
        return left == 0


def succeeded_steps(conn: Conn, job_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """step name -> the run that succeeded."""
    rows = conn.execute(
        "select step, run_id from job_steps where job_id = %s and state = 'succeeded'", (job_id,)
    ).fetchall()
    return {row["step"]: row["run_id"] for row in rows}


def set_run_image_digest(conn: Conn, run_id: uuid.UUID, digest: str | None) -> None:
    conn.execute("update runs set image_digest = %s where id = %s", (digest, run_id))


def job_runs(conn: Conn, job_id: uuid.UUID) -> list[Row]:
    return conn.execute(
        "select id, attempt, step, state, image, image_digest, exit_code, error, started_at, finished_at"
        " from runs where job_id = %s order by attempt",
        (job_id,),
    ).fetchall()


def run_manifest(conn: Conn, run_id: uuid.UUID) -> dict[str, Any]:
    """Everything needed to say later what produced a result: inputs, flow version and exact image."""
    row = conn.execute(
        "select j.id as job_id, j.accession_number, j.study_instance_uid, j.flow, j.flow_version, j.source,"
        " j.requested_by, r.id as run_id, r.step, r.attempt, r.image, r.image_digest, r.started_at"
        " from runs r join jobs j on j.id = r.job_id where r.id = %s",
        (run_id,),
    ).fetchone()
    series = conn.execute(
        "select r.series_instance_uid, r.study_instance_uid, r.received_instances as instances"
        " from job_series js join retrievals r using (series_instance_uid) where js.job_id = %s order by 1",
        (row["job_id"],),
    ).fetchall()
    steps = conn.execute(
        "select s.step, r.id as run_id, r.image, r.image_digest, r.finished_at from job_steps s"
        " join runs r on r.id = s.run_id where s.job_id = %s and s.state = 'succeeded' order by s.step",
        (row["job_id"],),
    ).fetchall()
    return {**row, "input_series": series, "steps": steps}


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


def finish_job(
    conn: Conn,
    job_id: uuid.UUID,
    deliveries: list[tuple[uuid.UUID, dict]],
    result_run_id: uuid.UUID | None = None,
    result: Any = None,
    result_problem: str | None = None,
) -> bool:
    """Every step succeeded: running -> sending, or -> done when there is nothing to deliver.

    `deliveries` pairs the run whose output is sent with a destination. `result` is the
    flow's structured result document, stored in the same transaction; `result_problem`
    says why a result file was not stored. Safe to call twice: the second call does nothing.
    """
    with conn.transaction():
        job = conn.execute("select state from jobs where id = %s for update", (job_id,)).fetchone()
        unfinished = conn.execute(
            "select 1 from job_steps where job_id = %s and state <> 'succeeded' limit 1", (job_id,)
        ).fetchone()
        if job is None or job["state"] != "running" or unfinished:
            return False
        conn.execute(
            "update jobs set state = %s, error = null, lease_owner = null, lease_expires_at = null, updated_at = now()"
            " where id = %s",
            ("sending" if deliveries else "done", job_id),
        )
        conn.execute("delete from deliveries where job_id = %s", (job_id,))  # from before a rerun
        for run_id, dest in deliveries:
            conn.execute(
                "insert into deliveries (id, job_id, run_id, destination) values (%s, %s, %s, %s)",
                (uuid.uuid4(), job_id, run_id, Jsonb(dest)),
            )
        if result is not None:
            conn.execute(
                "insert into results (job_id, run_id, flow, flow_version, data)"
                " select j.id, %s, j.flow, j.flow_version, %s from jobs j where j.id = %s"
                " on conflict (job_id) do update set run_id = excluded.run_id, flow_version = excluded.flow_version,"
                " data = excluded.data, created_at = now()",
                (result_run_id, Jsonb(result), job_id),
            )
            log_event(conn, job_id, "result_stored", run=str(result_run_id))
        elif result_problem:
            log_event(conn, job_id, "result_rejected", run=str(result_run_id), reason=result_problem)
        if not deliveries:
            log_event(conn, job_id, "done")
    return True


def get_result(conn: Conn, job_id: uuid.UUID) -> Row | None:
    return conn.execute(
        "select run_id, flow, flow_version, data, created_at from results where job_id = %s", (job_id,)
    ).fetchone()


def list_results(
    conn: Conn,
    flow: str | None = None,
    contains: Any = None,
    limit: int = 200,
    flows: list[str] | None = None,
) -> list[Row]:
    """Result documents, newest first. `contains` keeps documents that contain that JSON (Postgres @>)."""
    return conn.execute(
        "select r.job_id, j.accession_number, j.study_instance_uid, r.flow, r.flow_version, r.created_at, r.data"
        " from results r join jobs j on j.id = r.job_id"
        " where (%(flow)s::text is null or r.flow = %(flow)s)"
        " and (%(flows)s::text[] is null or r.flow = any(%(flows)s))"
        " and (%(contains)s::jsonb is null or r.data @> %(contains)s::jsonb)"
        " order by r.created_at desc limit %(limit)s",
        {"flow": flow, "flows": flows, "contains": None if contains is None else Jsonb(contains), "limit": limit},
    ).fetchall()


def result_flows(conn: Conn, flows: list[str] | None = None) -> list[Row]:
    return conn.execute(
        "select flow, count(*) as n from results where (%(f)s::text[] is null or flow = any(%(f)s))"
        " group by flow order by flow",
        {"f": flows},
    ).fetchall()


def step_failed(
    conn: Conn,
    job_id: uuid.UUID,
    step: str,
    run_id: uuid.UUID,
    owner: str,
    error: str,
    exit_code: int | None,
    retry: bool,
    delay_s: float,
) -> None:
    """A step's run failed: the step waits for another attempt, or the whole job fails.

    When the job fails, other executors running its steps see that at their next heartbeat
    and stop their containers.
    """
    with conn.transaction():
        finish_run(conn, run_id, "failed", exit_code, error)
        cur = conn.execute(
            "update job_steps set state = %s, lease_owner = null, lease_expires_at = null,"
            " not_before = now() + make_interval(secs => %s)"
            " where job_id = %s and step = %s and state = 'running' and lease_owner = %s",
            ("pending" if retry else "failed", delay_s, job_id, step, owner),
        )
        if cur.rowcount == 0:
            return
        log_event(conn, job_id, "run_failed", run=str(run_id), error=error, exit_code=exit_code, retry=retry)
        if retry:
            conn.execute("update jobs set error = %s where id = %s and state = 'running'", (error, job_id))
        else:
            fail_job(conn, job_id, error)


def delete_run(conn: Conn, run_id: uuid.UUID) -> None:
    conn.execute("delete from runs where id = %s", (run_id,))


def count_runs(conn: Conn, job_id: uuid.UUID, step: str | None = None) -> int:
    """Run attempts (of one step, if given) since the job was created or last retried by hand."""
    return conn.execute(
        "select count(*) as n from runs r join jobs j on j.id = r.job_id"
        " where r.job_id = %(job)s and r.started_at >= coalesce(j.retried_at, '-infinity')"
        " and (%(step)s::text is null or r.step = %(step)s)",
        {"job": job_id, "step": step},
    ).fetchone()["n"]


def requeue_for_retrieval(conn: Conn, job_id: uuid.UUID, step: str, owner: str, missing: list[str]) -> bool:
    """Cache files were missing when a step was prepared: fetch them again. running -> retrieving.

    Returns False, changing nothing, when other steps of the job are running: taking the job
    out of 'running' would stop them. The caller then treats it as a failed attempt.
    """
    with conn.transaction():
        conn.execute("select 1 from jobs where id = %s for update", (job_id,))
        others = conn.execute(
            "select 1 from job_steps where job_id = %s and step <> %s and state = 'running' limit 1", (job_id, step)
        ).fetchone()
        if others:
            return False
        release_step(conn, job_id, step, owner)
        cur = conn.execute(
            "update jobs set state = 'retrieving', updated_at = now() where id = %s and state = 'running'", (job_id,)
        )
        if cur.rowcount == 0:
            return True
        conn.execute(
            "update retrievals set state = 'pending', attempts = 0, not_before = now(), error = null"
            " where series_instance_uid = any(%s) and state in ('staged','evicted','failed')",
            (missing,),
        )
        log_event(conn, job_id, "cache_miss", series=missing)
    return True


def run_state(conn: Conn, run_id: uuid.UUID) -> str | None:
    row = conn.execute("select state from runs where id = %s", (run_id,)).fetchone()
    return row["state"] if row else None


def prune_status(conn: Conn, older_than_hours: float = 24) -> None:
    """Forget heartbeats of workers that are long gone."""
    conn.execute("delete from status where updated_at < now() - make_interval(secs => %s * 3600.0)", (older_than_hours,))


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
    extra["waiting_steps"] = waiting_steps(conn)
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


_USER_COLUMNS = (
    "id, username, role, grants, denies, allowed_flows, disabled, created_at, last_login_at, sso, totp_enabled"
)


def create_user(
    conn: Conn,
    username: str,
    role: str,
    password_hash: str | None = None,
    grants: list[str] | None = None,
    denies: list[str] | None = None,
    allowed_flows: list[str] | None = None,
    by: str | None = None,
    sso: bool = False,
) -> Row:
    """`sso` marks an account that signs in through the identity provider; it is linked to the
    person with that username at their first single sign-on."""
    with conn.transaction():
        user = conn.execute(
            "insert into users (id, username, role, password_hash, grants, denies, allowed_flows, sso)"
            f" values (%s, %s, %s, %s, %s, %s, %s, %s) returning {_USER_COLUMNS}",
            (uuid.uuid4(), username, role, password_hash, grants or [], denies or [], allowed_flows, sso),
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
        " and (password_hash is not null or sso or exists"
        "      (select 1 from api_tokens t where t.user_id = u.id and t.revoked_at is null)) limit 1"
    ).fetchone()
    if ok is None:
        raise ValueError("this would leave no enabled administrator")


def check_login(
    conn: Conn,
    username: str,
    verify,
    code: str | None = None,
    second_factor: bool = True,
    max_failures: int = 10,
    lock_minutes: int = 15,
) -> tuple[Row | None, str]:
    """Check a password sign-in. Returns (user, "ok"), or (None, reason) with reason "bad",
    "mfa_required" (right password, the account has two-step sign-in, no code given) or "mfa_bad".

    `verify(password_hash)` checks the password. `code` is an authenticator code or an unused
    recovery code. Wrong passwords and wrong codes count towards locking the account.
    """
    with conn.transaction():
        row = conn.execute("select * from users where username = %s for update", (username,)).fetchone()
        if row is None:
            verify(None)  # spend the same time as for a real user
            return None, "bad"
        locked = conn.execute("select %s > now() as locked", (row["locked_until"],)).fetchone()["locked"]

        def failed(reason: str) -> tuple[None, str]:
            if not row["disabled"] and not locked:
                conn.execute(
                    "update users set failed_logins = failed_logins + 1,"
                    " locked_until = case when failed_logins + 1 >= %s then now() + make_interval(mins => %s) end"
                    " where id = %s",
                    (max_failures, lock_minutes, row["id"]),
                )
            return None, reason

        if row["disabled"] or locked or not verify(row["password_hash"]):
            return failed("bad")
        if second_factor and row["totp_enabled"]:
            if not code:
                return None, "mfa_required"
            step = auth.totp_verify(row["totp_secret"], code, row["totp_last_step"])
            if step is not None:
                conn.execute("update users set totp_last_step = %s where id = %s", (step, row["id"]))
            elif auth.recovery_hash(code) in row["recovery_codes"]:
                conn.execute(
                    "update users set recovery_codes = array_remove(recovery_codes, %s) where id = %s",
                    (auth.recovery_hash(code), row["id"]),
                )
                log_event(conn, None, "recovery_code_used", username=username)
            else:
                return failed("mfa_bad")
        conn.execute(
            "update users set failed_logins = 0, locked_until = null, last_login_at = now() where id = %s",
            (row["id"],),
        )
    return get_user(conn, username), "ok"


def totp_begin(conn: Conn, username: str) -> str:
    """Start (or restart) setting up two-step sign-in. It takes effect only once confirmed with a code."""
    secret = auth.totp_new_secret()
    conn.execute(
        "update users set totp_secret = %s, totp_enabled = false, totp_last_step = null where username = %s",
        (secret, username),
    )
    return secret


def totp_confirm(conn: Conn, username: str, code: str) -> list[str] | None:
    """Turn two-step sign-in on if the code is right. Returns the recovery codes, shown once."""
    with conn.transaction():
        row = conn.execute(
            "select id, totp_secret from users where username = %s and not totp_enabled for update", (username,)
        ).fetchone()
        step = auth.totp_verify(row["totp_secret"], code, None) if row else None
        if step is None:
            return None
        codes = auth.new_recovery_codes()
        conn.execute(
            "update users set totp_enabled = true, totp_last_step = %s, recovery_codes = %s where id = %s",
            (step, [auth.recovery_hash(c) for c in codes], row["id"]),
        )
        log_event(conn, None, "mfa_enabled", username=username)
    return codes


def totp_disable(conn: Conn, username: str, by: str | None = None) -> bool:
    with conn.transaction():
        cur = conn.execute(
            "update users set totp_enabled = false, totp_secret = null, totp_last_step = null, recovery_codes = '{}'"
            " where username = %s and (totp_enabled or totp_secret is not null)",
            (username,),
        )
        if cur.rowcount:
            log_event(conn, None, "mfa_removed", username=username, by=by)
        return cur.rowcount == 1


def save_oidc_login(conn: Conn, state: str, nonce: str, verifier: str) -> None:
    conn.execute("delete from oidc_logins where created_at < now() - interval '1 hour'")
    conn.execute("insert into oidc_logins (state, nonce, verifier) values (%s, %s, %s)", (state, nonce, verifier))


def take_oidc_login(conn: Conn, state: str, max_age_s: float = 600) -> Row | None:
    """Fetch and delete a sign-in in progress: a state value works once, and only for a few minutes."""
    return conn.execute(
        "delete from oidc_logins where state = %s and created_at > now() - make_interval(secs => %s)"
        " returning nonce, verifier",
        (state, max_age_s),
    ).fetchone()


def sso_sign_in(
    conn: Conn, subject: str, username: str, mapped_role: str | None, default_role: str | None
) -> Row:
    """Find or create the account for a person the identity provider has authenticated.

    - An account already linked to this subject is used, whatever its username is now.
    - Otherwise an account created for single sign-on with this username is linked.
    - Otherwise a new account is created, if a role follows from the person's groups or a
      default role is configured. A password account or service account with the same
      username is never taken over.
    `mapped_role` (from the provider's groups) replaces the account's role when given.
    Raises ValueError with a reason code when the person may not sign in.
    """
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(727002)")
        row = conn.execute("select * from users where oidc_subject = %s for update", (subject,)).fetchone()
        if row is None:
            row = conn.execute("select * from users where username = %s for update", (username,)).fetchone()
            if row is not None:
                if not row["sso"] or row["oidc_subject"] is not None:
                    raise ValueError("username_taken")
                conn.execute("update users set oidc_subject = %s where id = %s", (subject, row["id"]))
                log_event(conn, None, "sso_linked", username=username)
            else:
                role = mapped_role or default_role
                if not role:
                    raise ValueError("not_authorized")
                create_user(conn, username, role, sso=True, by="sso")
                conn.execute("update users set oidc_subject = %s where username = %s", (subject, username))
                row = conn.execute("select * from users where username = %s", (username,)).fetchone()
        if row["disabled"]:
            raise ValueError("disabled")
        if mapped_role and mapped_role != row["role"]:
            try:
                with conn.transaction():
                    conn.execute("update users set role = %s where id = %s", (mapped_role, row["id"]))
                    if row["role"] == "admin":
                        _require_an_admin(conn)
                log_event(
                    conn, None, "user_changed", username=row["username"], changes={"role": mapped_role}, by="sso"
                )
            except ValueError:
                # The provider's groups would demote the only administrator. Keep the role:
                # a system nobody can administer is worse than a role that lags the directory.
                log_event(conn, None, "sso_demotion_skipped", username=row["username"], wanted=mapped_role)
        conn.execute("update users set last_login_at = now() where id = %s", (row["id"],))
        return get_user(conn, row["username"])


def create_session(conn: Conn, user_id: uuid.UUID, hours: float, sso: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("delete from sessions where expires_at < now()")
    conn.execute(
        "insert into sessions (token_hash, user_id, sso, expires_at)"
        " values (%s, %s, %s, now() + make_interval(secs => %s * 3600.0))",
        (_token_hash(token), user_id, sso, hours),
    )
    return token


def user_for_session(conn: Conn, token: str) -> Row | None:
    return conn.execute(
        f"select {', '.join('u.' + c.strip() for c in _USER_COLUMNS.split(','))}, s.sso as session_sso,"
        " u.password_hash is not null as has_password from sessions s"
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
