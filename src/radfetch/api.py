"""HTTP API for submitting and controlling jobs.

Set RADFETCH_API_TOKEN to require a bearer token. Without it there is no
authentication: bind the API to localhost or put it behind an authenticating proxy.
X-Remote-User, when a proxy sets it, is recorded as the requester.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Iterator

import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from importlib import resources

from fastapi.responses import HTMLResponse, PlainTextResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from radfetch import store
from radfetch.config import BATCH_PRIORITY, Settings
from radfetch.flows import FlowError, list_flows, load_flow


class JobIn(BaseModel):
    accession_number: str = Field(min_length=1, max_length=64)
    flow: str


class BatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    flow: str
    accession_numbers: list[str] = Field(min_length=1, max_length=100_000)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    pool = ConnectionPool(
        settings.database_url, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        pool.open()
        yield
        pool.close()

    def authorize(authorization: str | None = Header(None)) -> None:
        if settings.api_token is None:
            return
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(given.encode(), settings.api_token.encode()):
            raise HTTPException(401, "missing or wrong bearer token", headers={"WWW-Authenticate": "Bearer"})

    app = FastAPI(title="radfetch", lifespan=lifespan)
    guarded = [Depends(authorize)]

    def db() -> Iterator[store.Conn]:
        with pool.connection() as conn:
            yield conn

    def check_flow(name: str) -> None:
        try:
            load_flow(settings.flows_dir, name)
        except FlowError as exc:
            raise HTTPException(422, str(exc)) from exc

    def found(value):
        if not value:
            raise HTTPException(404, "not found")
        return value

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def ui():
        """The web UI: one static page that talks to this API (and asks for the token if one is set)."""
        return resources.files("radfetch").joinpath("ui.html").read_text()

    @app.get("/healthz")
    def healthz(conn=Depends(db)):
        return {"ok": True, "jobs": store.state_counts(conn), "status": store.get_status(conn)}

    @app.get("/metrics", response_class=PlainTextResponse, dependencies=guarded)
    def metrics(conn=Depends(db)):
        """Prometheus text format."""
        m = store.metrics(conn)
        lines = []
        for table in ("jobs", "retrievals", "deliveries"):
            lines.append(f"# TYPE radfetch_{table} gauge")
            lines += [f'radfetch_{table}{{state="{state}"}} {n}' for state, n in sorted(m[table].items())]
        gauges = {
            "radfetch_cache_bytes": m["cache_bytes"],
            "radfetch_oldest_active_job_seconds": m["oldest_active_job_s"],
            "radfetch_incoming_studies": m["incoming_studies"],
        }
        gateway = m["status"].get("gateway")
        if gateway:
            gauges["radfetch_gateway_heartbeat_age_seconds"] = gateway["age_s"]
            gauges["radfetch_gateway_breaker_open"] = int(gateway["breaker_open"])
            gauges["radfetch_gateway_batch_window_open"] = int(gateway["batch_window_open"])
            gauges["radfetch_gateway_consecutive_failures"] = gateway["consecutive_failures"]
        for name, value in gauges.items():
            lines += [f"# TYPE {name} gauge", f"{name} {value}"]
        return "\n".join(lines) + "\n"

    @app.get("/flows", dependencies=guarded)
    def flows():
        return list_flows(settings.flows_dir)

    @app.post("/jobs", status_code=201, dependencies=guarded)
    def submit_job(body: JobIn, conn=Depends(db), x_remote_user: str | None = Header(None)):
        """Submit a one-off job. It runs ahead of batch work and ignores the batch window."""
        check_flow(body.flow)
        job_id = store.submit_job(
            conn, body.accession_number.strip(), body.flow, settings.interactive_priority, None, x_remote_user
        )
        return {"id": job_id}

    @app.get("/jobs", dependencies=guarded)
    def jobs(state: str | None = None, batch_id: uuid.UUID | None = None, limit: int = 100, conn=Depends(db)):
        return store.list_jobs(conn, state, batch_id, min(limit, 1000))

    @app.get("/jobs/{job_id}", dependencies=guarded)
    def job(job_id: uuid.UUID, conn=Depends(db)):
        row = found(store.get_job(conn, job_id))
        row["events"] = store.job_events(conn, job_id)
        return row

    @app.post("/jobs/{job_id}/cancel", dependencies=guarded)
    def cancel_job(job_id: uuid.UUID, conn=Depends(db)):
        found(store.get_job(conn, job_id))
        return {"cancelled": store.cancel_job(conn, job_id)}

    @app.post("/jobs/{job_id}/retry", dependencies=guarded)
    def retry_job(job_id: uuid.UUID, full: bool = False, conn=Depends(db)):
        """Retry a failed or cancelled job. Only re-sends when just the delivery failed, unless full=true."""
        found(store.get_job(conn, job_id))
        state = store.retry_job(conn, job_id, full)
        if state is None:
            raise HTTPException(409, "only failed or cancelled jobs can be retried")
        return {"state": state}

    @app.post("/batches/{batch_id}/retry-failed", dependencies=guarded)
    def retry_failed(batch_id: uuid.UUID, conn=Depends(db)):
        found(store.get_batch(conn, batch_id))
        return {"retried": store.retry_failed_in_batch(conn, batch_id)}

    @app.post("/batches", status_code=201, dependencies=guarded)
    def create_batch(body: BatchIn, conn=Depends(db), x_remote_user: str | None = Header(None)):
        check_flow(body.flow)
        batch_id = store.create_batch(
            conn, body.name, body.flow, body.accession_numbers, BATCH_PRIORITY, x_remote_user
        )
        return found(store.get_batch(conn, batch_id))

    @app.get("/batches", dependencies=guarded)
    def batches(limit: int = 50, conn=Depends(db)):
        return store.list_batches(conn, min(limit, 500))

    @app.get("/batches/{batch_id}", dependencies=guarded)
    def batch(batch_id: uuid.UUID, conn=Depends(db)):
        return found(store.get_batch(conn, batch_id))

    @app.post("/batches/{batch_id}/pause", dependencies=guarded)
    def pause(batch_id: uuid.UUID, conn=Depends(db)):
        """Stop starting new work for the batch. A container that is already running finishes."""
        found(store.set_batch_paused(conn, batch_id, True))
        return store.get_batch(conn, batch_id)

    @app.post("/batches/{batch_id}/resume", dependencies=guarded)
    def resume(batch_id: uuid.UUID, conn=Depends(db)):
        found(store.set_batch_paused(conn, batch_id, False))
        return store.get_batch(conn, batch_id)

    @app.post("/batches/{batch_id}/cancel", dependencies=guarded)
    def cancel_batch(batch_id: uuid.UUID, conn=Depends(db)):
        found(store.get_batch(conn, batch_id))
        return {"cancelled": store.cancel_batch(conn, batch_id)}

    return app
