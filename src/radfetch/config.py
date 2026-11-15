"""Runtime configuration, read from RADFETCH_* environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

INTERACTIVE_PRIORITY = 100
BATCH_PRIORITY = 0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RADFETCH_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://radfetch:radfetch@localhost:5432/radfetch"
    data_dir: Path = Path("/data")
    # Path of data_dir as seen by the Docker daemon. The executor starts sibling
    # containers through the host's Docker socket, so bind mounts need host paths.
    host_data_dir: Path | None = None
    flows_dir: Path = Path("/flows")

    # --- PACS connection -------------------------------------------------
    pacs_host: str = "localhost"
    pacs_port: int = 11112
    pacs_ae_title: str = "PACS"
    local_ae_title: str = "RADFETCH"
    # get: C-GET. move: C-MOVE to our own storage SCP. dicomweb: QIDO-RS and WADO-RS.
    retrieve_mode: Literal["get", "move", "dicomweb"] = "get"
    # dicomweb only: base URL such as https://pacs.example/dicom-web, and an optional bearer token.
    dicomweb_url: str = ""
    dicomweb_token: str | None = None
    dicomweb_timeout_s: float = 120
    # C-MOVE only: port of our storage SCP. The PACS must know local_ae_title
    # as a move destination pointing at this host and port.
    move_scp_port: int = 11113
    dimse_timeout_s: float = 120
    acse_timeout_s: float = 30
    network_timeout_s: float = 120

    # --- TLS for DICOM connections ---------------------------------------------
    # CA bundle used to verify peers; our own certificate and key (PEM).
    tls_ca_file: Path | None = None
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    # Use TLS towards the PACS (C-FIND, C-GET, C-MOVE) and on the C-MOVE storage SCP.
    pacs_tls: bool = False
    # Use TLS on the push ingress SCP. With tls_ca_file set, senders need a client certificate.
    scp_tls: bool = False

    # --- PACS protection ---------------------------------------------------
    # Average instances per minute the gateway may pull. 0 disables the limit.
    rate_instances_per_min: float = 600
    # Minimum pause between two PACS operations (queries and retrievals).
    min_interval_s: float = 0.5
    # When batch work may touch the PACS, e.g. "mon-fri 18:00-06:00; sat-sun 00:00-24:00".
    # Empty means always. One-off jobs ignore the window.
    batch_windows: str = ""
    # Batch jobs are resolved (C-FIND) only while fewer than this many are waiting for data.
    max_resolved_ahead: int = 10
    # Batch series are retrieved only while fewer than this many jobs are staged, waiting for the executor.
    max_staged_jobs: int = 4
    # Batch retrieval stops above this disk usage fraction; all retrieval stops above the hard limit.
    disk_high_watermark: float = 0.80
    disk_hard_limit: float = 0.95
    breaker_failure_threshold: int = 3
    breaker_base_cooldown_s: float = 60
    breaker_max_cooldown_s: float = 1800
    resolve_max_attempts: int = 5
    retrieve_max_attempts: int = 5
    retry_base_delay_s: float = 60
    retry_max_delay_s: float = 3600

    # --- Workers -----------------------------------------------------------
    interactive_priority: int = INTERACTIVE_PRIORITY
    lease_s: float = 120
    # A series retrieval is one blocking call, so its lease must outlast the largest series.
    retrieve_lease_s: float = 3600
    idle_sleep_s: float = 2
    executor_poll_s: float = 2
    container_network: str = "none"
    # GPUs handed to gpu flows by this executor: empty for all, or device ids such as "0" or
    # "0,1". With several GPUs, run one executor per GPU, each with its own id.
    gpu_device_ids: str = ""
    # Debug only: run gpu flows WITHOUT a GPU, for hosts and CI that have none. The container
    # gets RADFETCH_FAKE_GPU=1 so a test image can take a CPU path. Never set this in production.
    debug_fake_gpu: bool = False
    deliver_max_attempts: int = 8

    # --- Push ingress (storage SCP) -------------------------------------------
    scp_port: int = 11114
    # A pushed study is processed once nothing has arrived for this long.
    scp_stable_s: float = 30
    # Comma-separated calling AE titles allowed to push. Empty accepts any sender.
    scp_allowed_ae_titles: str = ""
    push_priority: int = 50
    # --- API -----------------------------------------------------------------
    # When set, every endpoint except /healthz requires "Authorization: Bearer <token>".
    api_token: str | None = None

    # --- Janitor -----------------------------------------------------------
    cache_ttl_hours: float = 72
    run_ttl_hours: float = 168
    # Keep the cached input series and run files of failed jobs at least this long, so they
    # can be inspected and retried without fetching from the PACS again. 0 turns it off.
    # Disk pressure still evicts them.
    keep_failed_hours: float = 0
    janitor_interval_s: float = 300

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "incoming"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    def host_path(self, path: Path) -> Path:
        """Translate a path under data_dir to the path the Docker daemon sees."""
        if self.host_data_dir is None:
            return path
        return self.host_data_dir / path.relative_to(self.data_dir)

    def backoff_s(self, attempts: int) -> float:
        return min(self.retry_max_delay_s, self.retry_base_delay_s * 2 ** max(0, attempts - 1))
