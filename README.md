# RadFetch

Runs containerized workflows on studies pulled from a clinical PACS by accession
number, one-off or in batches, without overloading the PACS.

```
pull: API / CLI ──> jobs (Postgres) ──> gateway ──┐
                                          │ C-FIND, C-GET/C-MOVE
                                         PACS     ├─> series cache ──> executor ──> sender ──> destinations
push: modality / PACS ── C-STORE ──> scp ─────────┘                      │ Docker
                                                                    flow container
```

| Service | Role |
|---|---|
| `postgres` | The only state store: jobs, retrievals, runs, deliveries, audit events |
| `api` | Submit jobs and batches; pause, resume, cancel; status |
| `gateway` | The only process that talks to the PACS. Resolves accession numbers and retrieves series into the cache |
| `scp` | Push ingress: a storage SCP; received studies start every flow whose `trigger` matches |
| `executor` | Runs one flow container at a time (one instance per GPU) |
| `sender` | Delivers run output (C-STORE or directory), retried independently of inference |
| `janitor` | Evicts unused cached series and old run directories |

## Quick start (with a fake PACS)

```bash
cp .env.example .env
```

Edit `.env`: set `RADFETCH_HOST_DATA_DIR` to the absolute path of `./data`, and
clear `RADFETCH_BATCH_WINDOWS` if you want batches to run during the day.

```bash
docker compose --profile dev up -d --build
```

Open http://localhost:8000 for the web UI: submit one-off jobs and batches, pause,
resume and cancel, and read each job's history. The same operations by `curl`:

```bash
curl -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"accession_number": "ACC0001", "flow": "example"}'
```

```bash
curl -X POST localhost:8000/batches -H 'content-type: application/json' \
  -d '{"name": "demo", "flow": "example", "accession_numbers": ["ACC0002", "ACC0003", "ACC0004"]}'
```

```bash
curl localhost:8000/healthz
```

Results appear under `data/results/<job id>/`. `GET /jobs/<id>` shows a job with
its full event history. The same operations exist on the CLI
(`docker compose exec api radfetch --help`).

## Flows

One YAML file per flow in `flows/`; see `flows/example.yaml`. A flow says which
series to fetch, which image to run and where the output goes. Files are read on
every use, so edits apply without restarts.

Destinations can be `directory`, `dicom` (C-STORE) or `stow` (STOW-RS). A flow
can set `multiple_studies: all` to run on the matching series of every study that
shares the accession number; the default is to fail such a job.
`radfetch check-flows` validates every flow file.

Container contract: input at `/input/<SeriesInstanceUID>/*.dcm` (read-only),
results written to `/output`, exit code 0 on success, no network.
`RADFETCH_JOB_ID`, `RADFETCH_ACCESSION_NUMBER`, `RADFETCH_STUDY_INSTANCE_UID`
and `RADFETCH_FLOW` are set in the environment.

## Traceability

- Every run records the image it actually used as an immutable digest, not just the tag in the flow file. `GET /jobs/<id>` lists a job's runs with it, and the UI shows it in the job history.
- A successful run gets a `radfetch.json` in its output: job, accession number, flow and version, image digest, and the input series with instance counts. It travels with `directory` deliveries; `dicom` and `stow` deliveries skip it like any non-DICOM file.
- `GET /batches/<id>/jobs.csv` (the CSV button in the UI) lists every job of a batch with its state and error, for checking a batch in a spreadsheet.

## Retrying

`POST /jobs/<id>/retry` (or the Retry button, or `radfetch retry <id>`) restarts a
failed or cancelled job. If only the delivery failed, the job goes back to
`sending` and inference is not repeated; add `?full=true` to start over anyway.
`POST /batches/<id>/retry-failed` does the same for every failed job of a batch.

Set `RADFETCH_KEEP_FAILED_HOURS` to keep the cached input series and run files
(input links, partial output, container log) of failed jobs for that long, so a
failed job can be inspected and retried without fetching from the PACS again.
It is off by default, and disk pressure still evicts the data.

## Push ingress

Send studies by C-STORE to port 11114 (any called AE title). Set
`RADFETCH_SCP_ALLOWED_AE_TITLES` to a comma-separated list to accept only those senders. Once a study has
received nothing for `RADFETCH_SCP_STABLE_S` seconds, each flow that has a
`trigger` is checked: if the sender's AE title matches the trigger and the flow's
series rules select at least one received series, a job is created that starts at
`ready`. Flows without a `trigger` are never started by a push, and studies no
flow wants are dropped. Pushed and pulled series share one cache, so a pushed
series also satisfies pull jobs waiting for it.

Instances that arrive after the quiet period start a second job with only the
late instances, so set the quiet period longer than the sender's longest pause.

## Monitoring and access

- `GET /healthz` is always open and includes the gateway's last heartbeat.
- `GET /metrics` is Prometheus text: jobs, retrievals and deliveries by state, cache size, age of the oldest unfinished job, and whether the circuit breaker is open.

### API access

The API is open until a token exists, so keep it on localhost until you have created one.

```bash
docker compose exec api radfetch token create amy --role operator
```

- The token is printed once; only its hash is stored. `radfetch token list` and `radfetch token revoke <name>` manage them.
- `operator` tokens may submit, cancel, retry and pause. `viewer` tokens are read-only, and the UI hides its controls for them.
- The token's name is recorded as the requester of every job and on cancel, retry and pause events.
- `RADFETCH_API_TOKEN` is still accepted as one shared operator token.
- `/healthz` is always open.

### TLS for DICOM

Set `RADFETCH_TLS_CA_FILE`, `RADFETCH_TLS_CERT_FILE` and `RADFETCH_TLS_KEY_FILE` (PEM), then:

- `RADFETCH_PACS_TLS=true` for TLS towards the PACS and on the C-MOVE storage SCP.
- `RADFETCH_SCP_TLS=true` for TLS on the push ingress. With a CA file set, senders must present a certificate signed by it.
- `tls: true` on a `dicom` destination in a flow for TLS when delivering results.

## How the PACS is protected

All of this lives in the gateway (`src/radfetch/gateway/`):

- **One association at a time.** The gateway is a single process doing one PACS operation per step. Run exactly one instance.
- **Selective retrieval.** Only the series a flow selects are fetched, and a series needed by several jobs is fetched once.
- **Rate budget.** `RADFETCH_RATE_INSTANCES_PER_MIN` caps the average pull rate for batch work; `RADFETCH_MIN_INTERVAL_S` spaces all operations.
- **Batch windows.** Batch work touches the PACS only inside `RADFETCH_BATCH_WINDOWS`.
- **Bounded preloading.** Batch series are fetched only while fewer than `RADFETCH_MAX_STAGED_JOBS` jobs are staged for the executor, and batch jobs are resolved only `RADFETCH_MAX_RESOLVED_AHEAD` ahead.
- **Disk watermarks.** Batch retrieval stops at 80% disk usage, all retrieval at 95%.
- **Circuit breaker.** After consecutive PACS failures all traffic stops for a cooldown that doubles while failures continue.

One-off jobs (`POST /jobs`) run ahead of batch work and ignore the window, the
rate budget and the preload limit, but never the single-association cap, the
circuit breaker or the hard disk limit.

## Job states

```
queued ─> retrieving ─> ready ─> running ─> sending ─> done
   any state before done ─> failed | cancelled
```

Workers claim rows with a lease (`FOR UPDATE SKIP LOCKED`). A crashed worker
needs no cleanup: its lease expires and the row is claimable again. After an
executor crash, the next executor adopts the still-running container by label
instead of starting inference again.

## Connecting a real PACS

`docs/deployment.md` is a step-by-step checklist for a first installation, including
what to ask the PACS administrator and a trial plan. To try against a real PACS
implementation locally first, `docker compose --profile orthanc up` adds Orthanc.

Set `RADFETCH_PACS_HOST`, `RADFETCH_PACS_PORT`, `RADFETCH_PACS_AE_TITLE` and
`RADFETCH_LOCAL_AE_TITLE` in `.env`.

- `RADFETCH_RETRIEVE_MODE=get` uses C-GET: nothing to register on the PACS beyond the calling AE title, but not every PACS supports it. The storage classes requested are listed in `GET_STORAGE_CLASSES` in `gateway/dimse.py`; extend that list for other modalities.
- `RADFETCH_RETRIEVE_MODE=dicomweb` uses QIDO-RS and WADO-RS against `RADFETCH_DICOMWEB_URL`, with an optional bearer token in `RADFETCH_DICOMWEB_TOKEN`. The PACS host and AE title settings are not used.
- `RADFETCH_RETRIEVE_MODE=move` uses C-MOVE to the gateway's own storage SCP on port 11113. The PACS must know the local AE title as a destination at this host and port.

## GPUs

A flow with `gpu: true` gets the host's GPUs through the NVIDIA Container Toolkit.
One executor runs one container at a time, so with several GPUs run one executor
per GPU and give each its own `RADFETCH_GPU_DEVICE_IDS` (for example `0`).

For development and CI on hosts without a GPU, `RADFETCH_DEBUG_FAKE_GPU=true`
makes the executor run `gpu: true` flows without requesting a GPU. The container
gets `RADFETCH_FAKE_GPU=1`, so a test image can take a CPU path. The executor logs
a warning at start while it is set; never set it in production, where a model
image would silently run on CPU or fail.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
```

Database tests need a Postgres they may wipe:

```bash
docker compose up -d postgres
```

```bash
RADFETCH_TEST_DATABASE_URL=postgresql://radfetch:radfetch@localhost:5432/radfetch pytest
```

Use a separate scratch database if the stack holds real jobs: the tests drop every table.
The Docker runtime tests run only when a Docker daemon is reachable.
`scripts/smoke.sh` brings the whole Compose stack up against the fake PACS, runs a
`gpu: true` flow with the fake GPU, and checks the results; CI runs it on every push.
With `RADFETCH_TEST_ORTHANC_TLS=1` it starts an Orthanc with mutual TLS and tests C-MOVE over it.
With `RADFETCH_TEST_ORTHANC_URL` set, the suite also runs against a real Orthanc
(all three retrieve modes and both DICOM delivery types).
`.github/workflows/test.yml` runs the suite, builds the image and validates the
example flows on every pull request and push to `main`.

## Schema changes

`radfetch init-db` applies the numbered SQL files in `src/radfetch/migrations/`
that have not run yet, and Compose runs it on every start. To change the schema,
add the next numbered file; never edit one that has been applied.

## Not built yet

- Single sign-on or passwords for the UI (it asks for a token).
- Per-flow or per-batch permissions: every operator can act on every job.
- TLS on the HTTP API itself: terminate it in a reverse proxy.
