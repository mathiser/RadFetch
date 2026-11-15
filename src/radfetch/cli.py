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


@app.command("cancel-batch")
def cancel_batch(batch_id: uuid.UUID):
    _, conn = _setup()
    typer.echo(f"cancelled {store.cancel_batch(conn, batch_id)} jobs")
