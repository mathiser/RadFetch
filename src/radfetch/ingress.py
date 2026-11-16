"""Push ingress: a storage SCP that turns received studies into jobs.

Anything may be sent to the SCP. Once a study has been quiet for scp_stable_s,
every flow with a `trigger` is checked against it; each flow whose trigger and
series rules match gets a job that starts at 'ready', with the received series
placed in the same cache that pulled series use. Studies no flow wants are dropped.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydicom import dcmread
from pynetdicom import AE, AllStoragePresentationContexts, evt

from radfetch import store, tls
from radfetch.config import Settings
from radfetch.flows import Flow, FlowError, list_flows, load_flow
from radfetch.gateway.pacs import SeriesInfo, valid_uid
from radfetch.loop import run_steps

log = logging.getLogger("radfetch.ingress")

REFUSED = 0xA700
INVALID = 0xC000


class IngressServer:
    """The network side: writes each received instance to incoming/<study>/<series>/ and records the arrival."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._tmp = settings.incoming_dir / ".tmp"
        self._tmp.mkdir(parents=True, exist_ok=True)
        # Other replicas may be receiving into the same folder: remove only files abandoned long ago.
        for stale in self._tmp.iterdir():
            try:
                if time.time() - stale.stat().st_mtime > 3600:
                    stale.unlink()
            except OSError:
                pass
        self._pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=4, open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        self._server = None

    def start(self) -> None:
        self._pool.open()
        ae = AE(ae_title=self.s.local_ae_title)
        ae.supported_contexts = AllStoragePresentationContexts
        allowed = [t.strip() for t in self.s.scp_allowed_ae_titles.split(",") if t.strip()]
        if allowed:
            ae.require_calling_aet = allowed  # other senders are rejected at association time
        self._server = ae.start_server(
            ("0.0.0.0", self.s.scp_port),
            block=False,
            evt_handlers=[(evt.EVT_C_STORE, self._handle_store)],
            ssl_context=tls.server_context(self.s) if self.s.scp_tls else None,
        )
        log.info("storage SCP listening on port %d%s", self.s.scp_port, " (TLS)" if self.s.scp_tls else "")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        self._pool.close()

    def _handle_store(self, event) -> int:
        try:
            ds = event.dataset
            study = str(ds.get("StudyInstanceUID", ""))
            series = str(ds.get("SeriesInstanceUID", ""))
            sop = str(event.request.AffectedSOPInstanceUID)
            # The UIDs become path components: refuse anything that is not a plain UID.
            if not (valid_uid(study) and valid_uid(series) and valid_uid(sop)):
                return INVALID
            tmp = self._tmp / f"{sop}.{uuid.uuid4().hex}"
            tmp.write_bytes(event.encoded_dataset())
            target = self.s.incoming_dir / study / series / f"{sop}.dcm"
            for attempt in (1, 2):
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(tmp, target)
                    break
                except FileNotFoundError:
                    # The processor moved the study directory away between mkdir and replace.
                    if attempt == 2:
                        raise
            try:
                with self._pool.connection() as conn:
                    store.touch_incoming(
                        conn, study, str(ds.get("AccessionNumber", "") or ""), event.assoc.requestor.ae_title
                    )
            except Exception:
                target.unlink(missing_ok=True)  # without the row nothing would ever pick the file up
                raise
            return 0x0000
        except Exception:
            log.exception("could not store received instance")
            return REFUSED


class IngressProcessor:
    """The decision side: turns quiet studies into jobs. One step handles one study."""

    def __init__(self, conn: store.Conn, settings: Settings, *, sleep: Callable[[float], None] = time.sleep):
        self.conn, self.s = conn, settings
        self._sleep = sleep
        self._work = settings.incoming_dir / ".processing"
        self._work.mkdir(parents=True, exist_ok=True)
        self._leading = False

    def run_forever(self) -> None:
        run_steps(self.step, min(self.s.idle_sleep_s, 2), self._sleep)

    def _recover(self) -> None:
        """Put back studies that were mid-processing when we stopped; their rows are still there."""
        for work in self._work.iterdir():
            study = work.name.rsplit(".", 1)[0]
            for f in (p for p in work.rglob("*") if p.is_file()):
                target = self.s.incoming_dir / study / f.relative_to(work)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(f, target)
            shutil.rmtree(work, ignore_errors=True)

    def _triggered_flows(self) -> list[Flow]:
        flows = []
        for name in list_flows(self.s.flows_dir):
            try:
                flow = load_flow(self.s.flows_dir, name)
            except FlowError as exc:
                log.error("skipping flow: %s", exc)
                continue
            if flow.trigger is not None:
                flows.append(flow)
        return flows

    @staticmethod
    def _scan(work: Path, study_uid: str) -> list[SeriesInfo]:
        series = []
        for d in sorted(p for p in work.iterdir() if p.is_dir()):
            files = sorted(d.glob("*.dcm"))
            if not files:
                continue
            try:
                header = dcmread(files[0], stop_before_pixels=True)
            except Exception:
                log.exception("unreadable series %s, ignoring it", d.name)
                continue
            series.append(
                SeriesInfo(
                    series_instance_uid=d.name,
                    study_instance_uid=study_uid,
                    modality=str(header.get("Modality", "")) or None,
                    description=str(header.get("SeriesDescription", "")) or None,
                    instances=len(files),
                )
            )
        return series

    def _stage(self, work: Path, series_uid: str) -> tuple[int, int]:
        """Hardlink a received series into the cache, merging with what is already there."""
        dst = self.s.cache_dir / series_uid
        dst.mkdir(parents=True, exist_ok=True)
        for f in (work / series_uid).glob("*.dcm"):
            try:
                os.link(f, dst / f.name)
            except FileExistsError:
                pass
        files = list(dst.glob("*.dcm"))
        return len(files), sum(f.stat().st_size for f in files)

    def step(self) -> bool:
        # Any number of replicas may receive studies; one of them turns them into jobs.
        if not self._leading:
            if not store.try_lead(self.conn, "ingress"):
                return False
            self._leading = True
            self._recover()
        work = None
        try:
            with self.conn.transaction():
                row = store.claim_stable_incoming(self.conn, self.s.scp_stable_s)
                if row is None:
                    return False
                study_uid = row["study_instance_uid"]
                src = self.s.incoming_dir / study_uid
                if not src.is_dir():
                    store.delete_incoming(self.conn, study_uid)
                    return True
                # Take the directory out of the SCP's way: later arrivals start a fresh one.
                work = self._work / f"{study_uid}.{uuid.uuid4().hex}"
                os.rename(src, work)

                series = self._scan(work, study_uid)
                sender = row["calling_ae_title"] or ""
                jobs = 0
                for flow in self._triggered_flows():
                    if not flow.trigger.matches(sender):
                        continue
                    selected = flow.select_series(series)
                    if not selected:
                        continue
                    store.create_pushed_job(
                        self.conn,
                        row["accession_number"] or "",
                        flow.name,
                        flow.version,
                        self.s.push_priority,
                        study_uid,
                        [s.series_instance_uid for s in selected],
                        lambda uid, work=work: self._stage(work, uid),
                        requested_by=f"push:{sender}",
                    )
                    jobs += 1
                store.delete_incoming(self.conn, study_uid)
                if jobs == 0:
                    store.log_event(self.conn, None, "push_ignored", study=study_uid, sender=sender, series=len(series))
                log.info("study %s from %s: %d series, %d job(s)", study_uid, sender, len(series), jobs)
        except Exception:
            # Rolled back: the row is still there. Put the files back so the next attempt sees them.
            if work is not None and work.exists():
                self._recover()
            if work is not None:
                # Send a study that keeps failing to the back of the queue so it cannot block the others.
                store.touch_incoming(self.conn, work.name.rsplit(".", 1)[0], None, "")
            raise
        shutil.rmtree(work, ignore_errors=True)
        return True
