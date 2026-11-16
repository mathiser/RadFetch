"""Command line: run the services and submit or inspect work."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

import typer

from radfetch import db, store
from radfetch.config import BATCH_PRIORITY, Settings
from radfetch.flows import FlowError, load_flow
from radfetch.loop import serve

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _setup() -> tuple[Settings, store.Conn]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    return settings, db.connect(settings.database_url)


def _check_flow(settings: Settings, name: str) -> None:
    try:
        load_flow(settings.flows_dir, name)
    except FlowError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


def _print(value) -> None:
    typer.echo(json.dumps(value, indent=2, default=str))


# --- services -----------------------------------------------------------------------


@app.command("init-db")
def init_db():
    """Apply pending schema migrations (safe to repeat)."""
    _, conn = _setup()
    applied = db.init_schema(conn)
    typer.echo(f"schema ready ({', '.join(applied) if applied else 'nothing to apply'})")


def _service() -> Settings:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("pynetdicom").setLevel(logging.WARNING)  # logs every sub-operation at INFO
    return Settings()


@app.command()
def gateway():
    """Run the PACS gateway. Run exactly one instance."""
    from radfetch.gateway.worker import Gateway

    settings = _service()
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    if settings.retrieve_mode == "dicomweb":
        from radfetch.gateway.dicomweb import DicomWebClient

        pacs = DicomWebClient(settings)
    else:
        from radfetch.gateway.dimse import DimseClient

        pacs = DimseClient(settings)
    serve(settings.database_url, lambda conn: Gateway(conn, pacs, settings))


@app.command()
def executor():
    """Run the container executor. One instance per GPU."""
    from radfetch.executor.docker_runtime import DockerRuntime
    from radfetch.executor.worker import Executor

    settings = _service()
    runtime = DockerRuntime(settings)
    serve(settings.database_url, lambda conn: Executor(conn, runtime, settings))


@app.command()
def sender():
    """Run the output sender."""
    from radfetch.sender import Sender

    settings = _service()
    serve(settings.database_url, lambda conn: Sender(conn, settings))


@app.command()
def scp():
    """Run the storage SCP that turns pushed studies into jobs for flows with a trigger."""
    from radfetch.ingress import IngressProcessor, IngressServer

    settings = _service()
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    IngressServer(settings).start()
    serve(settings.database_url, lambda conn: IngressProcessor(conn, settings))


@app.command()
def janitor():
    """Run cache eviction and run-directory cleanup."""
    from radfetch.janitor import Janitor

    settings = _service()
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    serve(settings.database_url, lambda conn: Janitor(conn, settings))


@app.command()
def api(host: str = "0.0.0.0", port: int = 8000):
    """Run the HTTP API."""
    import uvicorn

    from radfetch.api import create_app

    uvicorn.run(create_app(), host=host, port=port)


@app.command("fake-pacs")
def fake_pacs(
    port: int = 11112,
    studies: int = 5,
    ae_title: str = "PACS",
    move_dest: Optional[str] = typer.Option(None, help="AET=host:port of a C-MOVE destination"),
):
    """Run an in-memory Q/R SCP with synthetic studies ACC0001.. for development."""
    from radfetch.devtools.fakepacs import FakePacsServer, default_studies

    logging.basicConfig(level=logging.INFO)
    destinations = {}
    if move_dest:
        aet, addr = move_dest.split("=")
        host, dest_port = addr.rsplit(":", 1)
        destinations[aet] = (host, int(dest_port))
    FakePacsServer(default_studies(studies), ae_title, destinations).start(port)
    typer.echo(f"fake PACS {ae_title} listening on {port} with accession numbers ACC0001..ACC{studies:04d}")
    while True:
        time.sleep(3600)


@app.command("fake-push")
def fake_push(
    host: str = "localhost",
    port: int = 11114,
    calling_aet: str = "CT_TEST",
    accession_number: str = "PUSH0001",
    instances: int = 12,
):
    """Send one synthetic CT study by C-STORE, to try the push ingress."""
    from pynetdicom import AE

    from radfetch.devtools.fakepacs import CT, FakeSeries, FakeStudy, _instance

    study = FakeStudy(accession_number, [FakeSeries("CT", "Thorax 2.0 B31f", instances)])
    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(CT)
    assoc = ae.associate(host, port)
    if not assoc.is_established:
        typer.echo("error: association rejected or host unreachable", err=True)
        raise typer.Exit(1)
    stored = sum(
        1 for i in range(instances) if assoc.send_c_store(_instance(study, study.series[0], i + 1)).Status == 0
    )
    assoc.release()
    typer.echo(f"sent {stored}/{instances} instances of {accession_number} as {calling_aet}")
    raise typer.Exit(0 if stored == instances else 1)


# --- client commands ------------------------------------------------------------------


@app.command()
def submit(accession_number: str, flow: str):
    """Submit a one-off job (runs ahead of batch work)."""
    settings, conn = _setup()
    _check_flow(settings, flow)
    typer.echo(store.submit_job(conn, accession_number, flow, settings.interactive_priority, None, "cli"))


@app.command()
def batch(name: str, flow: str, file: Path = typer.Argument(..., help="one accession number per line")):
    """Submit a batch from a text file."""
    settings, conn = _setup()
    _check_flow(settings, flow)
    lines = [line.split(",")[0] for line in file.read_text().splitlines() if not line.startswith("#")]
    batch_id = store.create_batch(conn, name, flow, lines, BATCH_PRIORITY, "cli")
    _print(store.get_batch(conn, batch_id))


@app.command()
def status(job_id: Optional[uuid.UUID] = typer.Argument(None)):
    """Show one job with its history, or the job counts per state."""
    _, conn = _setup()
    if job_id is None:
        _print(store.state_counts(conn))
        return
    job = store.get_job(conn, job_id)
    if job is None:
        raise typer.Exit(1)
    job["events"] = store.job_events(conn, job_id)
    _print(job)


@app.command("batch-status")
def batch_status(batch_id: uuid.UUID):
    _, conn = _setup()
    _print(store.get_batch(conn, batch_id))


@app.command()
def pause(batch_id: uuid.UUID):
    _, conn = _setup()
    store.set_batch_paused(conn, batch_id, True)


@app.command()
def resume(batch_id: uuid.UUID):
    _, conn = _setup()
    store.set_batch_paused(conn, batch_id, False)


@app.command()
def cancel(job_id: uuid.UUID):
    _, conn = _setup()
    typer.echo("cancelled" if store.cancel_job(conn, job_id) else "not cancelled (unknown or already finished)")


@app.command()
def retry(job_id: uuid.UUID, full: bool = typer.Option(False, help="start over even if only delivery failed")):
    """Retry a failed or cancelled job."""
    _, conn = _setup()
    state = store.retry_job(conn, job_id, full)
    typer.echo(f"restarted from {state}" if state else "not retried (unknown, or not failed/cancelled)")


@app.command("retry-failed")
def retry_failed(batch_id: uuid.UUID):
    """Retry every failed job of a batch."""
    _, conn = _setup()
    typer.echo(f"retried {store.retry_failed_in_batch(conn, batch_id)} jobs")


@app.command("check-flows")
def check_flows():
    """Validate every flow file; exits non-zero if any is invalid."""
    from radfetch.flows import list_flows

    settings = Settings()
    bad = 0
    for name in list_flows(settings.flows_dir):
        try:
            flow = load_flow(settings.flows_dir, name)
            steps = ", ".join(f"{st.name}: {st.image}" if len(flow.steps) > 1 else st.image for st in flow.ordered_steps())
            typer.echo(f"ok    {name} (version {flow.version}, {'steps ' if len(flow.steps) > 1 else 'image '}{steps})")
        except FlowError as exc:
            bad += 1
            typer.echo(f"error {exc}", err=True)
    raise typer.Exit(1 if bad else 0)


user_app = typer.Typer(no_args_is_help=True, help="Manage user accounts.")
token_app = typer.Typer(no_args_is_help=True, help="Manage API tokens.")
app.add_typer(user_app, name="user")
app.add_typer(token_app, name="token")


def _admin_conn():
    return db.connect(Settings().database_url)


def _fail(message: str):
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


@user_app.command("create")
def user_create(
    username: str,
    role: str = typer.Option("viewer", help="monitor, viewer, submitter, operator or admin"),
    password_stdin: bool = typer.Option(False, help="read the password from standard input instead of generating one"),
    service_account: bool = typer.Option(False, help="no password; the account is used with API tokens only"),
    flows: Optional[str] = typer.Option(None, help="comma-separated flows the user is limited to (default: all)"),
):
    """Create a user. A generated password is printed once. The first user should be an admin."""
    import sys

    from radfetch import auth

    if role not in auth.ROLES:
        _fail(f"role must be one of: {', '.join(auth.ROLES)}")
    password = generated = None
    if not service_account:
        password = sys.stdin.readline().rstrip("\n") if password_stdin else (generated := auth.generate_password())
    try:
        store.create_user(
            _admin_conn(), username, role,
            auth.hash_password(password) if password else None,
            allowed_flows=[f.strip() for f in flows.split(",")] if flows else None, by="cli",
        )
    except Exception as exc:
        _fail(str(exc).splitlines()[0])
    typer.echo(f"created {username} ({role})")
    if generated:
        typer.echo(f"password: {generated}")


@user_app.command("list")
def user_list():
    _print(store.list_users(_admin_conn()))


@user_app.command("set")
def user_set(
    username: str,
    role: Optional[str] = typer.Option(None),
    grant: list[str] = typer.Option([], help="permission to add on top of the role (repeatable)"),
    deny: list[str] = typer.Option([], help="permission to remove from the role (repeatable)"),
    clear_adjustments: bool = typer.Option(False, help="remove all individual grants and denies"),
    flows: Optional[str] = typer.Option(None, help="comma-separated flows to limit the user to"),
    all_flows: bool = typer.Option(False, help="lift the flow restriction"),
    disable: bool = typer.Option(False),
    enable: bool = typer.Option(False),
):
    """Change a user's role, individual permissions, flow restriction, or disable the account."""
    from radfetch import auth

    conn = _admin_conn()
    user = store.get_user(conn, username)
    if user is None:
        _fail("no such user")
    changes: dict = {}
    if role:
        if role not in auth.ROLES:
            _fail(f"role must be one of: {', '.join(auth.ROLES)}")
        changes["role"] = role
    try:
        auth.validate_permissions([*grant, *deny])
    except ValueError as exc:
        _fail(str(exc))
    if grant or deny or clear_adjustments:
        base_grants, base_denies = ([], []) if clear_adjustments else (user["grants"], user["denies"])
        changes["grants"] = sorted((set(base_grants) | set(grant)) - set(deny))
        changes["denies"] = sorted((set(base_denies) | set(deny)) - set(grant))
    if all_flows:
        changes["allowed_flows"] = None
    elif flows is not None:
        changes["allowed_flows"] = [f.strip() for f in flows.split(",") if f.strip()]
    if disable or enable:
        changes["disabled"] = disable
    try:
        _print(store.update_user(conn, username, changes, by="cli"))
    except ValueError as exc:
        _fail(str(exc))


@user_app.command("passwd")
def user_passwd(username: str, password_stdin: bool = typer.Option(False)):
    """Set a new password (generated unless --password-stdin). Signs the user out everywhere."""
    import sys

    from radfetch import auth

    password = sys.stdin.readline().rstrip("\n") if password_stdin else auth.generate_password()
    try:
        ok = store.update_user(_admin_conn(), username, {"password_hash": auth.hash_password(password)}, by="cli")
    except ValueError as exc:
        _fail(str(exc))
    if ok is None:
        _fail("no such user")
    typer.echo("password changed" if password_stdin else f"password: {password}")


@user_app.command("delete")
def user_delete(username: str):
    try:
        ok = store.delete_user(_admin_conn(), username, by="cli")
    except ValueError as exc:
        _fail(str(exc))
    typer.echo("deleted" if ok else "no such user")


@user_app.command("roles")
def user_roles():
    """Show the roles and the permissions that can be granted or denied individually."""
    from radfetch import auth

    for name, perms in auth.ROLES.items():
        typer.echo(f"{name:<10} {auth.ROLE_DESCRIPTIONS[name]}")
        typer.echo(f"{'':<10} {', '.join(sorted(perms))}")
    typer.echo("\npermissions:")
    for name, text in auth.PERMISSIONS.items():
        typer.echo(f"  {name:<18} {text}")


@token_app.command("create")
def token_create(username: str, name: Optional[str] = typer.Option(None, help="a label for the token")):
    """Create an API token for a user and print it once. It has that user's rights."""
    try:
        typer.echo(store.create_token(_admin_conn(), username, name, by="cli"))
    except Exception as exc:
        _fail(str(exc).splitlines()[0])


@token_app.command("list")
def token_list():
    _print(store.list_tokens(_admin_conn()))


@token_app.command("revoke")
def token_revoke(name: str):
    ok = store.revoke_token(_admin_conn(), name, by="cli")
    typer.echo("revoked" if ok else "no active token with that name")


@app.command("cancel-batch")
def cancel_batch(batch_id: uuid.UUID):
    _, conn = _setup()
    typer.echo(f"cancelled {store.cancel_batch(conn, batch_id)} jobs")
