# Development

## Layout

```
src/radfetch/
  store.py            every state change, each one transaction
  migrations/         numbered SQL schema files
  gateway/            PACS side: worker, throttle, DIMSE and DICOMweb clients
  executor/           runtime interface, Docker implementation, worker
  ingress.py          push ingress (storage SCP)
  sender.py           deliveries and notifications
  janitor.py          cache eviction and cleanup
  auth.py             roles, permissions, password hashing
  api.py, ui.html     HTTP API and the single-page web interface
  cli.py              command line
  devtools/fakepacs.py  in-memory PACS (DIMSE and DICOMweb) for tests and demos
tests/                unit and integration tests
scripts/smoke.sh      full Compose stack test
deploy/              example Docker Swarm stack
mkdocs.yml            documentation site (published by .github/workflows/docs.yml)
```

Design rules worth keeping:

- Postgres is the only source of truth. A state change and everything that follows from it happen in one transaction in `store.py`.
- Work is claimed with leases (`FOR UPDATE SKIP LOCKED`); there are no "in progress" states to clean up after a crash.
- Only the gateway talks to the PACS, and it does one thing at a time.
- Executors claim single steps (`job_steps`), by kind. A job is `running` from its first claimed step until its last step succeeds; finishing it is idempotent and guarded by a lock on the job.
- The executor knows containers only through the `Runtime` interface.

## Running the tests

```bash
uv venv && uv pip install -e '.[dev]'
```

The database tests need a Postgres they may wipe:

```bash
docker compose up -d postgres
```

```bash
RADFETCH_TEST_DATABASE_URL=postgresql://radfetch:radfetch@localhost:5432/radfetch pytest
```

Use a separate scratch database if the stack holds real jobs: the tests drop every table.

| Set | To also run |
|---|---|
| a reachable Docker daemon | Docker runtime tests |
| `RADFETCH_TEST_ORTHANC_URL=http://localhost:8042` | Retrieval and delivery against a real Orthanc (`docker compose --profile orthanc up -d`; C-MOVE needs Orthanc to reach port 11193 on the test host) |
| `RADFETCH_TEST_ORTHANC_TLS=1` | C-MOVE over mutual TLS; the test starts its own Orthanc with host networking |
| a host with the NVIDIA runtime | The real-GPU test |

`scripts/smoke.sh` builds the image, starts the whole stack against the fake
PACS, runs a `gpu: true` flow with the fake GPU, and checks the results.

CI (`.github/workflows/test.yml`) runs all of the above except the real-GPU test
on every pull request and push to `main`.

## Documentation site

```bash
pip install 'mkdocs-material==9.5.*' && mkdocs serve
```

The site is built with `--strict` in CI, so a broken link fails the build.

## Changing the schema

Add the next numbered file under `src/radfetch/migrations/`. Never edit a file
that has been applied anywhere. Update `tests/test_migrations.py`.

## Adding things

- **A PACS protocol**: implement the three methods of `PacsClient` (`gateway/pacs.py`) and select it in `cli.py`.
- **A container runtime** (Kubernetes, Slurm): implement `Runtime` (`executor/runtime.py`).
- **A destination type**: add a model in `flows.py` and a send function in `sender.py`.
- **A notification event**: insert a row into `notifications` in the transaction that causes it, and add its message in `notify.py`.
