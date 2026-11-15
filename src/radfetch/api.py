"""HTTP API for submitting and controlling jobs.

Authentication is by bearer token:
- named tokens created with `radfetch token create NAME --role viewer|operator`;
  the token's name is recorded as the requester of everything it does;
- optionally one shared operator token in RADFETCH_API_TOKEN.
While neither exists the API is open: keep it on localhost or behind an
authenticating proxy (its X-Remote-User header is then recorded as the requester).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Iterator

import secrets
from dataclasses import dataclass
from importlib import resources

from fastapi import Depends, FastAPI, Header, HTTPException

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


@dataclass(frozen=True)
class User:
    name: str | None
    role: str  # 'viewer' may read; 'operator' may also submit, cancel, retry, pause


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

    app = FastAPI(title="radfetch", lifespan=lifespan)

    def db() -> Iterator[store.Conn]:
        with pool.connection() as conn:
            yield conn

    def current_user(
        authorization: str | None = Header(None), x_remote_user: str | None = Header(None), conn=Depends(db)
    ) -> User:
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token:
            if settings.api_token and secrets.compare_digest(token.encode(), settings.api_token.encode()):
                return User(x_remote_user or "shared-token", "operator")
            row = store.find_token(conn, token)
            if row:
                return User(row["name"], row["role"])
        elif settings.api_token is None and not store.tokens_exist(conn):
            return User(x_remote_user, "operator")  # open mode: no tokens configured at all
        raise HTTPException(401, "missing or wrong bearer token", headers={"WWW-Authenticate": "Bearer"})

    def operator(user: User = Depends(current_user)) -> User:
        if user.role != "operator":
            raise HTTPException(403, "this token is read-only")
        return user

    guarded = [Depends(current_user)]

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

    @app.get("/whoami")
    def whoami(user: User = Depends(current_user)):
        return {"name": user.name, "role": user.role}

    @app.get("/flows", dependencies=guarded)
    def flows():
        return list_flows(settings.flows_dir)

    @app.post("/jobs", status_code=201)
    def submit_job(body: JobIn, conn=Depends(db), user: User = Depends(operator)):
        """Submit a one-off job. It runs ahead of batch work and ignores the batch window."""
        check_flow(body.flow)
        job_id = store.submit_job(
            conn, body.accession_number.strip(), body.flow, settings.interactive_priority, None, user.name
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

    @app.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: uuid.UUID, conn=Depends(db), user: User = Depends(operator)):
        found(store.get_job(conn, job_id))
        return {"cancelled": store.cancel_job(conn, job_id, user.name)}

    @app.post("/jobs/{job_id}/retry")
    def retry_job(job_id: uuid.UUID, full: bool = False, conn=Depends(db), user: User = Depends(operator)):
        """Retry a failed or cancelled job. Only re-sends when just the delivery failed, unless full=true."""
        found(store.get_job(conn, job_id))
        state = store.retry_job(conn, job_id, full, user.name)
        if state is None:
            raise HTTPException(409, "only failed or cancelled jobs can be retried")
        return {"state": state}

    @app.post("/batches/{batch_id}/retry-failed")
    def retry_failed(batch_id: uuid.UUID, conn=Depends(db), user: User = Depends(operator)):
        found(store.get_batch(conn, batch_id))
        return {"retried": store.retry_failed_in_batch(conn, batch_id, user.name)}

    @app.post("/batches", status_code=201)
    def create_batch(body: BatchIn, conn=Depends(db), user: User = Depends(operator)):
        check_flow(body.flow)
        batch_id = store.create_batch(
            conn, body.name, body.flow, body.accession_numbers, BATCH_PRIORITY, user.name
        )
        return found(store.get_batch(conn, batch_id))

    @app.get("/batches", dependencies=guarded)
    def batches(limit: int = 50, conn=Depends(db)):
        return store.list_batches(conn, min(limit, 500))

    @app.get("/batches/{batch_id}", dependencies=guarded)
    def batch(batch_id: uuid.UUID, conn=Depends(db)):
        return found(store.get_batch(conn, batch_id))

    @app.post("/batches/{batch_id}/pause")
    def pause(batch_id: uuid.UUID, conn=Depends(db), user: User = Depends(operator)):
        """Stop starting new work for the batch. A container that is already running finishes."""
        found(store.set_batch_paused(conn, batch_id, True, user.name))
        return store.get_batch(conn, batch_id)

    @app.post("/batches/{batch_id}/resume")
    def resume(batch_id: uuid.UUID, conn=Depends(db), user: User = Depends(operator)):
        found(store.set_batch_paused(conn, batch_id, False, user.name))
        return store.get_batch(conn, batch_id)

    @app.post("/batches/{batch_id}/cancel")
    def cancel_batch(batch_id: uuid.UUID, conn=Depends(db), user: User = Depends(operator)):
        found(store.get_batch(conn, batch_id))
        return {"cancelled": store.cancel_batch(conn, batch_id, user.name)}

    return app
