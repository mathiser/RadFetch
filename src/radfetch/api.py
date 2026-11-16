"""HTTP API and web UI.

Who may do what is decided in radfetch.auth: a role gives a set of permissions,
and each user can have permissions added or removed and be limited to certain flows.

A request is authenticated by a session cookie (after POST /auth/login), by a
bearer token belonging to a user, or by the optional shared RADFETCH_API_TOKEN.
While no user exists and no shared token is set, the API is open with operator
rights: keep it on localhost until the first administrator has been created.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import json
import secrets
import time
import logging
import uuid
from contextlib import asynccontextmanager
from importlib import resources
from collections.abc import Callable, Iterator

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
import segno
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from radfetch import auth, store
from radfetch.sso import MESSAGES as SSO_MESSAGES
from radfetch.sso import Oidc, SsoError
from radfetch.auth import Principal
from radfetch.config import BATCH_PRIORITY, Settings
from radfetch.flows import FlowError, list_flows, load_flow

log = logging.getLogger("radfetch.api")

SESSION_COOKIE = "radfetch_session"
USERNAME = auth.USERNAME_PATTERN


class JobIn(BaseModel):
    accession_number: str = Field(min_length=1, max_length=64)
    flow: str


class BatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    flow: str
    accession_numbers: list[str] = Field(min_length=1, max_length=100_000)


class LoginIn(BaseModel):
    username: str
    password: str
    code: str | None = None  # authenticator or recovery code, when the account has two-step sign-in


class CodeIn(BaseModel):
    code: str


class MfaRemoveIn(BaseModel):
    password: str


class UserIn(BaseModel):
    username: str = Field(pattern=USERNAME)
    role: str
    password: str | None = None  # omitted: a password is generated and returned once
    service_account: bool = False  # no password at all; use tokens
    sso: bool = False  # signs in through the identity provider; no password
    grants: list[str] = []
    denies: list[str] = []
    allowed_flows: list[str] | None = None


class UserPatch(BaseModel):
    role: str | None = None
    grants: list[str] | None = None
    denies: list[str] | None = None
    allowed_flows: list[str] | None = None
    all_flows: bool = False  # set true to lift a flow restriction
    disabled: bool | None = None
    password: str | None = None
    reset_mfa: bool = False  # remove the user's two-step sign-in, e.g. after a lost phone


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


class TokenIn(BaseModel):
    name: str | None = Field(default=None, pattern=USERNAME)


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

    app = FastAPI(title="RadFetch", lifespan=lifespan)

    def db() -> Iterator[store.Conn]:
        with pool.connection() as conn:
            yield conn

    oidc = Oidc(settings)
    oidc.check_config()

    # --- who is asking -----------------------------------------------------------------

    def auth_enabled(conn) -> bool:
        # With single sign-on configured the API is never open, even before the first person has signed in.
        return settings.api_token is not None or oidc.enabled or store.users_exist(conn)

    def signed_in(
        authorization: str | None = Header(None),
        x_remote_user: str | None = Header(None),
        session: str | None = Cookie(None, alias=SESSION_COOKIE),
        conn=Depends(db),
    ) -> Principal:
        """Who is asking. Use `current_user` unless the endpoint must work before two-step sign-in is set up."""
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token:
            if settings.api_token and secrets.compare_digest(token.encode(), settings.api_token.encode()):
                return Principal(x_remote_user or "shared-token", "operator", auth.ROLES["operator"])
            user = store.user_for_token(conn, token)
            if user:
                return auth.principal_for(user)
        elif session:
            user = store.user_for_session(conn, session)
            if user:
                principal = auth.principal_for(user)
                # A person who signed in with a password must have a second factor when that is the rule.
                # Single sign-on leaves the second factor to the identity provider.
                must_set_up = settings.require_mfa and not user["session_sso"] and not user["totp_enabled"]
                return dataclasses.replace(principal, sso=user["session_sso"], mfa_setup_required=must_set_up)
        elif not auth_enabled(conn):
            return Principal(x_remote_user, "operator", auth.ROLES["operator"])  # open mode
        raise HTTPException(401, "not signed in", headers={"WWW-Authenticate": "Bearer"})

    def current_user(user: Principal = Depends(signed_in)) -> Principal:
        if user.mfa_setup_required:
            raise HTTPException(403, "set up two-step sign-in first (Account page)")
        return user

    def need(permission: str) -> Callable[..., Principal]:
        def check(user: Principal = Depends(current_user)) -> Principal:
            if not user.can(permission):
                raise HTTPException(403, f"you lack the permission {permission}")
            return user

        return check

    def flows_of(user: Principal) -> list[str] | None:
        return None if user.allowed_flows is None else sorted(user.allowed_flows)

    def check_flow(name: str, user: Principal) -> None:
        if not user.sees_flow(name):
            raise HTTPException(403, f"you may not use the flow {name}")
        try:
            load_flow(settings.flows_dir, name)
        except FlowError as exc:
            raise HTTPException(422, str(exc)) from exc

    def found(value):
        if not value:
            raise HTTPException(404, "not found")
        return value

    def visible_job(conn, job_id: uuid.UUID, user: Principal) -> dict:
        job = store.get_job(conn, job_id)
        if job is None or not user.sees_flow(job["flow"]):
            raise HTTPException(404, "not found")  # same answer whether hidden or absent
        return job

    def visible_batch(conn, batch_id: uuid.UUID, user: Principal) -> dict:
        batch = store.get_batch(conn, batch_id)
        if batch is None or not user.sees_flow(batch["flow"]):
            raise HTTPException(404, "not found")
        return batch

    def may_manage(user: Principal, owner: str | None) -> None:
        if user.can("jobs.manage_all"):
            return
        if user.can("jobs.manage_own") and owner is not None and owner == user.name:
            return
        raise HTTPException(403, "you may only manage what you submitted yourself")

    def redact(job: dict, user: Principal) -> dict:
        """Without identifiers.view, a job shows its state and flow but nothing that identifies a patient."""
        if user.can("identifiers.view"):
            return job
        hidden = dict(job)
        hidden["accession_number"] = hidden["study_instance_uid"] = None
        if hidden.get("error"):
            hidden["error"] = "(hidden)"
        return hidden

    def describe(user: Principal, conn) -> dict:
        return {
            "name": user.name,
            "role": user.role,
            "permissions": sorted(user.permissions),
            "allowed_flows": flows_of(user),
            "auth_enabled": auth_enabled(conn),
            "sso": user.sso,
            "mfa_enabled": user.mfa_enabled,
            "mfa_setup_required": user.mfa_setup_required,
            "mfa_required_by_policy": settings.require_mfa,
        }

    # --- UI and sign-in ----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def ui():
        return resources.files("radfetch").joinpath("ui.html").read_text()

    @app.get("/healthz")
    def healthz(conn=Depends(db)):
        """Always open, for health checks. Job counts only."""
        return {"ok": True, "jobs": store.state_counts(conn), "status": store.get_status(conn)}

    def start_session(response: Response, conn, user: dict, sso: bool = False) -> None:
        token = store.create_session(conn, user["id"], settings.session_hours, sso)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=int(settings.session_hours * 3600),
            httponly=True,
            samesite="strict",
            secure=settings.public_url.startswith("https://"),
        )

    @app.get("/auth/config")
    def auth_config():
        """What the sign-in screen should offer. Open to everyone."""
        return {
            "sso": oidc.enabled,
            "sso_label": settings.oidc_button_label,
            "password_login": settings.password_login or not oidc.enabled,
            "messages": SSO_MESSAGES,
        }

    @app.post("/auth/login")
    def login(body: LoginIn, response: Response, conn=Depends(db)):
        if oidc.enabled and not settings.password_login:
            raise HTTPException(403, "password sign-in is turned off; use single sign-on")
        user, reason = store.check_login(
            conn, body.username, lambda stored: auth.verify_password(body.password, stored), body.code
        )
        if reason == "mfa_required":
            # The password was right; ask for the code. Nothing is signed in yet.
            return JSONResponse({"detail": "enter the code from your authenticator app", "mfa_required": True}, 401)
        if user is None:
            time.sleep(0.5)  # slows guessing; the account also locks after repeated failures
            raise HTTPException(
                401,
                "that code is not right" if reason == "mfa_bad"
                else "wrong username or password, or the account is locked or disabled",
            )
        start_session(response, conn, user)
        return {**describe(auth.principal_for(user), conn),
                "mfa_setup_required": settings.require_mfa and not user["totp_enabled"]}

    @app.get("/auth/oidc/login", include_in_schema=False)
    def oidc_login(conn=Depends(db)):
        try:
            return RedirectResponse(oidc.begin(conn), 302)
        except SsoError as exc:
            log.warning("single sign-on could not start: %s", exc)
            return RedirectResponse(f"/?sso_error={exc.code}", 303)

    @app.get("/auth/oidc/callback", include_in_schema=False)
    def oidc_callback(code: str = "", state: str = "", error: str = "", conn=Depends(db)):
        try:
            if error or not code:
                raise SsoError("denied", error)
            user = oidc.sign_in(conn, code, state)
        except SsoError as exc:
            log.warning("single sign-on refused (%s): %s", exc.code, exc)
            store.log_event(conn, None, "sso_refused", reason=exc.code)
            return RedirectResponse(f"/?sso_error={exc.code}", 303)
        response = RedirectResponse("/", 303)
        start_session(response, conn, user, sso=True)
        return response

    @app.post("/auth/logout")
    def logout(response: Response, session: str | None = Cookie(None, alias=SESSION_COOKIE), conn=Depends(db)):
        """Ends the RadFetch session. For a single sign-on session the answer also says where the
        browser can go to sign out at the identity provider."""
        sso_logout = None
        if session:
            user = store.user_for_session(conn, session)
            if user and user["session_sso"] and oidc.enabled:
                sso_logout = oidc.logout_url()
            store.delete_session(conn, session)
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True, "sso_logout_url": sso_logout}

    @app.get("/whoami")
    def whoami(user: Principal = Depends(signed_in), conn=Depends(db)):
        return describe(user, conn)

    @app.get("/roles")
    def roles(_: Principal = Depends(current_user)):
        """The roles, what each may do, and every permission that can be granted or denied individually."""
        return {
            "roles": [
                {"name": name, "description": auth.ROLE_DESCRIPTIONS[name], "permissions": sorted(perms)}
                for name, perms in auth.ROLES.items()
            ],
            "permissions": auth.PERMISSIONS,
        }

    # --- own account -------------------------------------------------------------------

    def real_user(user: Principal) -> str:
        if user.user_id is None:
            raise HTTPException(400, "this sign-in is not a user account")
        return user.name

    @app.post("/me/password")
    def change_password(body: PasswordIn, user: Principal = Depends(current_user), conn=Depends(db)):
        name = real_user(user)
        ok, _ = store.check_login(
            conn, name, lambda stored: auth.verify_password(body.current_password, stored), second_factor=False
        )
        if not ok:
            raise HTTPException(403, "the current password is wrong")
        try:
            store.update_user(conn, name, {"password_hash": auth.hash_password(body.new_password)}, by=name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "signed_out": True}

    # Two-step sign-in. These work before it is set up, so they depend on signed_in, not current_user.

    @app.post("/me/mfa/setup")
    def mfa_setup(user: Principal = Depends(signed_in), conn=Depends(db)):
        """Start setting up an authenticator app. Returns the secret, and a QR code to scan."""
        name = real_user(user)
        if user.sso:
            raise HTTPException(409, "this sign-in uses single sign-on; its second factor is managed there")
        secret = store.totp_begin(conn, name)
        uri = auth.totp_uri(secret, name)
        buffer = io.BytesIO()
        segno.make(uri, error="m").save(buffer, kind="svg", scale=4, border=2, xmldecl=False)
        return {"secret": secret, "uri": uri, "qr_svg": buffer.getvalue().decode()}

    @app.post("/me/mfa/confirm")
    def mfa_confirm(body: CodeIn, user: Principal = Depends(signed_in), conn=Depends(db)):
        """Confirm the set-up with a code from the app. Returns one-time recovery codes, shown only now."""
        codes = store.totp_confirm(conn, real_user(user), body.code)
        if codes is None:
            raise HTTPException(422, "that code is not right; check the app and the device's clock")
        return {"recovery_codes": codes}

    @app.post("/me/mfa/remove")
    def mfa_remove(body: MfaRemoveIn, user: Principal = Depends(signed_in), conn=Depends(db)):
        name = real_user(user)
        if settings.require_mfa:
            raise HTTPException(409, "two-step sign-in is mandatory here; set it up again instead of removing it")
        ok, _ = store.check_login(
            conn, name, lambda stored: auth.verify_password(body.password, stored), second_factor=False
        )
        if not ok:
            raise HTTPException(403, "the password is wrong")
        return {"removed": store.totp_disable(conn, name, by=name)}

    @app.get("/me/tokens")
    def my_tokens(user: Principal = Depends(current_user), conn=Depends(db)):
        return store.list_tokens(conn, real_user(user))

    @app.post("/me/tokens", status_code=201)
    def create_my_token(body: TokenIn, user: Principal = Depends(current_user), conn=Depends(db)):
        """An API token with your own rights. It is shown once."""
        name = real_user(user)
        try:
            return {"token": store.create_token(conn, name, body.name, by=name)}
        except UniqueViolation as exc:
            raise HTTPException(409, "a token with that name exists") from exc

    @app.delete("/me/tokens/{name}")
    def revoke_my_token(name: str, user: Principal = Depends(current_user), conn=Depends(db)):
        return {"revoked": store.revoke_token(conn, name, real_user(user), by=user.name)}

    # --- status and dashboards -----------------------------------------------------------

    @app.get("/flows")
    def flows(user: Principal = Depends(need("jobs.view"))):
        return [f for f in list_flows(settings.flows_dir) if user.sees_flow(f)]

    @app.get("/flows/{name}")
    def flow_detail(name: str, user: Principal = Depends(need("jobs.view"))):
        check_flow(name, user)
        flow = load_flow(settings.flows_dir, name)
        return flow.model_dump(exclude={"env"})  # env may hold settings not meant for every reader

    @app.get("/stats/overview")
    def stats(hours: int = 24, user: Principal = Depends(need("stats.view")), conn=Depends(db)):
        return store.stats_overview(conn, max(1, min(hours, 24 * 30)), flows_of(user))

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(_: Principal = Depends(need("stats.view")), conn=Depends(db)):
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
            if "disk_used_fraction" in gateway:
                gauges["radfetch_disk_used_fraction"] = gateway["disk_used_fraction"]
        for name, value in gauges.items():
            lines += [f"# TYPE {name} gauge", f"{name} {value}"]
        alive = [v for k, v in m["status"].items() if k.startswith("executor:") and v["age_s"] < 60]
        lines.append("# TYPE radfetch_executors gauge")
        for kind in ("gpu", "cpu", "any"):
            lines.append(f'radfetch_executors{{kind="{kind}"}} {sum(1 for v in alive if v.get("kind") == kind)}')
        waiting = m["waiting_steps"]
        lines.append("# TYPE radfetch_waiting_steps gauge")
        lines += [f'radfetch_waiting_steps{{needs="{k}"}} {waiting[k]}' for k in ("gpu", "cpu")]
        return "\n".join(lines) + "\n"

    @app.get("/audit")
    def audit(limit: int = 200, user: Principal = Depends(need("audit.view")), conn=Depends(db)):
        """Who did what. Job events show their detail only to those who may see identifiers."""
        events = store.audit_log(conn, min(limit, 2000), flows_of(user))
        if not user.can("identifiers.view"):
            for e in events:
                if e["job_id"] is not None:
                    e["detail"] = {k: v for k, v in e["detail"].items() if k == "by"}
        return events

    # --- jobs ----------------------------------------------------------------------------

    @app.post("/jobs", status_code=201)
    def submit_job(body: JobIn, conn=Depends(db), user: Principal = Depends(need("jobs.submit"))):
        """Submit a one-off job. It runs ahead of batch work and ignores the batch window."""
        check_flow(body.flow, user)
        job_id = store.submit_job(
            conn, body.accession_number.strip(), body.flow, settings.interactive_priority, None, user.name
        )
        return {"id": job_id}

    @app.get("/jobs")
    def jobs(
        state: str | None = None,
        batch_id: uuid.UUID | None = None,
        search: str | None = None,
        limit: int = 100,
        conn=Depends(db),
        user: Principal = Depends(need("jobs.view")),
    ):
        if search and not user.can("identifiers.view"):
            raise HTTPException(403, "you lack the permission identifiers.view")
        rows = store.list_jobs(conn, state, batch_id, min(limit, 1000), flows_of(user), search or None)
        return [redact(j, user) for j in rows]

    @app.get("/jobs/{job_id}")
    def job(job_id: uuid.UUID, conn=Depends(db), user: Principal = Depends(need("jobs.view"))):
        row = redact(visible_job(conn, job_id, user), user)
        events = store.job_events(conn, job_id)
        if not user.can("identifiers.view"):
            events = [{"at": e["at"], "kind": e["kind"], "detail": {}} for e in events]
        row["events"] = events
        row["runs"] = store.job_runs(conn, job_id)
        row["steps"] = [{k: s[k] for k in ("step", "state", "gpu", "needs")} for s in store.job_steps(conn, job_id)]
        return row

    @app.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: uuid.UUID, conn=Depends(db), user: Principal = Depends(current_user)):
        may_manage(user, visible_job(conn, job_id, user)["requested_by"])
        return {"cancelled": store.cancel_job(conn, job_id, user.name)}

    @app.post("/jobs/{job_id}/retry")
    def retry_job(job_id: uuid.UUID, full: bool = False, conn=Depends(db), user: Principal = Depends(current_user)):
        """Retry a failed or cancelled job. Only re-sends when just the delivery failed, unless full=true."""
        may_manage(user, visible_job(conn, job_id, user)["requested_by"])
        state = store.retry_job(conn, job_id, full, user.name)
        if state is None:
            raise HTTPException(409, "only failed or cancelled jobs can be retried")
        return {"state": state}

    # --- results -------------------------------------------------------------------------

    def parse_contains(contains: str | None):
        if not contains:
            return None
        try:
            return json.loads(contains)
        except ValueError as exc:
            raise HTTPException(422, "contains must be JSON, for example {\"grade\": 3}") from exc

    def result_row(row: dict, user: Principal) -> dict:
        if not user.can("identifiers.view"):
            row = {**row, "accession_number": None, "study_instance_uid": None}
        return row

    @app.get("/jobs/{job_id}/result")
    def job_result(job_id: uuid.UUID, conn=Depends(db), user: Principal = Depends(need("results.view"))):
        """The structured result the flow reported for this job."""
        visible_job(conn, job_id, user)
        return found(store.get_result(conn, job_id))

    @app.get("/results/flows")
    def results_flows(conn=Depends(db), user: Principal = Depends(need("results.view"))):
        return store.result_flows(conn, flows_of(user))

    @app.get("/results")
    def results(
        flow: str | None = None,
        contains: str | None = None,
        limit: int = 200,
        conn=Depends(db),
        user: Principal = Depends(need("results.view")),
    ):
        """Result documents, newest first. `contains` keeps documents containing that JSON."""
        rows = store.list_results(conn, flow, parse_contains(contains), min(limit, 5000), flows_of(user))
        return [result_row(r, user) for r in rows]

    @app.get("/results.csv")
    def results_csv(
        flow: str,
        contains: str | None = None,
        conn=Depends(db),
        user: Principal = Depends(need("results.view")),
    ):
        """One flow's results as a table: one column per top-level key; nested values as JSON text."""
        rows = [result_row(r, user) for r in store.list_results(conn, flow, parse_contains(contains), 100_000, flows_of(user))]
        keys = sorted({k for r in rows if isinstance(r["data"], dict) for k in r["data"]})
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["accession_number", "study_instance_uid", "job_id", "flow_version", "created_at", *keys])

        def cell(value):
            if value is None:
                return ""
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            # A leading = + - @ would be run as a formula by spreadsheet programs.
            return "'" + value if isinstance(value, str) and value[:1] in "=+-@" else value

        for r in rows:
            data = r["data"] if isinstance(r["data"], dict) else {}
            writer.writerow([cell(r["accession_number"]), cell(r["study_instance_uid"]), r["job_id"], cell(r["flow_version"]),
                             r["created_at"].isoformat(), *[cell(data.get(k)) for k in keys]])
        return Response(
            out.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="results-{flow}.csv"'},
        )

    # --- batches -------------------------------------------------------------------------

    @app.post("/batches", status_code=201)
    def create_batch(body: BatchIn, conn=Depends(db), user: Principal = Depends(need("batches.submit"))):
        check_flow(body.flow, user)
        batch_id = store.create_batch(conn, body.name, body.flow, body.accession_numbers, BATCH_PRIORITY, user.name)
        return found(store.get_batch(conn, batch_id))

    @app.get("/batches")
    def batches(limit: int = 50, conn=Depends(db), user: Principal = Depends(need("jobs.view"))):
        return store.list_batches(conn, min(limit, 500), flows_of(user))

    @app.get("/batches/{batch_id}")
    def batch(batch_id: uuid.UUID, conn=Depends(db), user: Principal = Depends(need("jobs.view"))):
        return visible_batch(conn, batch_id, user)

    @app.get("/batches/{batch_id}/jobs.csv")
    def batch_csv(batch_id: uuid.UUID, conn=Depends(db), user: Principal = Depends(need("identifiers.view"))):
        """One row per job: what ran, what failed and why. For checking a batch in a spreadsheet."""
        visible_batch(conn, batch_id, user)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["accession_number", "state", "error", "study_instance_uid", "job_id", "updated_at"])
        for j in store.batch_jobs(conn, batch_id):
            # A leading = + - @ would be run as a formula by spreadsheet programs.
            cells = [j["accession_number"], j["state"], j["error"] or "", j["study_instance_uid"] or "", j["id"],
                     j["updated_at"].isoformat()]
            writer.writerow(["'" + c if isinstance(c, str) and c[:1] in "=+-@" else c for c in cells])
        return Response(
            out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="batch-{batch_id}.csv"'},
        )

    def manage_batch(conn, batch_id: uuid.UUID, user: Principal) -> None:
        may_manage(user, visible_batch(conn, batch_id, user)["requested_by"])

    @app.post("/batches/{batch_id}/pause")
    def pause(batch_id: uuid.UUID, conn=Depends(db), user: Principal = Depends(current_user)):
        """Stop starting new work for the batch. A container that is already running finishes."""
        manage_batch(conn, batch_id, user)
        store.set_batch_paused(conn, batch_id, True, user.name)
        return store.get_batch(conn, batch_id)

    @app.post("/batches/{batch_id}/resume")
    def resume(batch_id: uuid.UUID, conn=Depends(db), user: Principal = Depends(current_user)):
        manage_batch(conn, batch_id, user)
        store.set_batch_paused(conn, batch_id, False, user.name)
        return store.get_batch(conn, batch_id)

    @app.post("/batches/{batch_id}/cancel")
    def cancel_batch(batch_id: uuid.UUID, conn=Depends(db), user: Principal = Depends(current_user)):
        manage_batch(conn, batch_id, user)
        return {"cancelled": store.cancel_batch(conn, batch_id, user.name)}

    @app.post("/batches/{batch_id}/retry-failed")
    def retry_failed(batch_id: uuid.UUID, conn=Depends(db), user: Principal = Depends(current_user)):
        manage_batch(conn, batch_id, user)
        return {"retried": store.retry_failed_in_batch(conn, batch_id, user.name)}

    # --- user administration -------------------------------------------------------------

    admin = need("users.manage")

    def validated(role: str | None, grants: list[str] | None, denies: list[str] | None) -> None:
        if role is not None and role not in auth.ROLES:
            raise HTTPException(422, f"unknown role: {role}")
        try:
            auth.validate_permissions([*(grants or []), *(denies or [])])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/users")
    def users(_: Principal = Depends(admin), conn=Depends(db)):
        rows = store.list_users(conn)
        for row in rows:
            row["permissions"] = sorted(auth.effective_permissions(row["role"], row["grants"], row["denies"]))
        return rows

    @app.post("/users", status_code=201)
    def create_user(body: UserIn, actor: Principal = Depends(admin), conn=Depends(db)):
        """Create a user. Without a password one is generated and returned once."""
        validated(body.role, body.grants, body.denies)
        generated = None
        password_hash = None
        if not (body.service_account or body.sso):
            password = body.password or (generated := auth.generate_password())
            try:
                password_hash = auth.hash_password(password)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        try:
            user = store.create_user(
                conn, body.username, body.role, password_hash, body.grants, body.denies, body.allowed_flows,
                actor.name, sso=body.sso,
            )
        except UniqueViolation as exc:
            raise HTTPException(409, "that username is taken") from exc
        return {**user, "generated_password": generated}

    @app.patch("/users/{username}")
    def change_user(username: str, body: UserPatch, actor: Principal = Depends(admin), conn=Depends(db)):
        validated(body.role, body.grants, body.denies)
        changes = body.model_dump(exclude_unset=True, exclude={"password", "all_flows", "reset_mfa"})
        if body.reset_mfa:
            store.totp_disable(conn, username, by=actor.name)
        if body.all_flows:
            changes["allowed_flows"] = None
        elif "allowed_flows" in changes and changes["allowed_flows"] is None:
            del changes["allowed_flows"]
        if body.password is not None:
            try:
                changes["password_hash"] = auth.hash_password(body.password)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        try:
            return found(store.update_user(conn, username, changes, actor.name))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/users/{username}")
    def delete_user(username: str, actor: Principal = Depends(admin), conn=Depends(db)):
        try:
            return {"deleted": store.delete_user(conn, username, actor.name)}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/users/{username}/tokens")
    def user_tokens(username: str, _: Principal = Depends(admin), conn=Depends(db)):
        found(store.get_user(conn, username))
        return store.list_tokens(conn, username)

    @app.post("/users/{username}/tokens", status_code=201)
    def create_user_token(username: str, body: TokenIn, actor: Principal = Depends(admin), conn=Depends(db)):
        found(store.get_user(conn, username))
        try:
            return {"token": store.create_token(conn, username, body.name, by=actor.name)}
        except UniqueViolation as exc:
            raise HTTPException(409, "a token with that name exists") from exc

    @app.delete("/users/{username}/tokens/{name}")
    def revoke_user_token(username: str, name: str, actor: Principal = Depends(admin), conn=Depends(db)):
        return {"revoked": store.revoke_token(conn, name, username, by=actor.name)}

    return app
