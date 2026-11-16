# Installation on a GPU host

A checklist for the first installation against a clinical PACS. Work through it
in order; each step names how to confirm it worked. If you have not used RadFetch
before, do the [Tutorial](tutorial.md) first.

## 1. Host

- [ ] Docker Engine and the Compose plugin are installed.
- [ ] For GPU flows: the NVIDIA driver and NVIDIA Container Toolkit are installed. Confirm with:

  ```bash
  docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
  ```

- [ ] A data disk is mounted where `./data` will live. The cache, run directories and results must be on **one filesystem** (inputs are hardlinked from the cache).
- [ ] The host clock and timezone are right; batch windows use local time (`TZ` in `.env`).

## 2. PACS side (ask the PACS administrator)

- [ ] RadFetch's AE title (`RADFETCH_LOCAL_AE_TITLE`) is registered as allowed to query and retrieve.
- [ ] Which retrieval the PACS supports decides `RADFETCH_RETRIEVE_MODE`:
  - `dicomweb` if QIDO-RS and WADO-RS are offered.
  - `get` if C-GET is supported: nothing more to register.
  - `move` otherwise: the PACS must also know the AE title as a **destination** at this host, port 11113, and its firewall must allow that connection.
- [ ] Agree the limits with the PACS team, then set them: instances per minute, the batch window, and whether day-time one-off jobs are acceptable.
- [ ] If results go back to the PACS: the AE title is allowed to C-STORE (or STOW-RS) there.

## 3. Configuration

- [ ] Copy `.env.example` to `.env` and set `RADFETCH_HOST_DATA_DIR` to the absolute path of `./data`.
- [ ] Set the PACS connection, retrieve mode, `RADFETCH_RATE_INSTANCES_PER_MIN` and `RADFETCH_BATCH_WINDOWS`.
- [ ] Change the Postgres password in `docker-compose.yml` (both in `postgres` and in `RADFETCH_DATABASE_URL`).
- [ ] If the modalities or PACS require TLS, mount the certificates and set the `RADFETCH_TLS_*` settings (see [Connecting a PACS](pacs.md)).
- [ ] Optionally set `RADFETCH_NOTIFY_WEBHOOK_URL` to be told about failures and finished batches (see [Administration](administration.md)).
- [ ] Make sure `RADFETCH_DEBUG_FAKE_GPU` is **not** set. With several GPUs, run one executor per GPU with `RADFETCH_GPU_DEVICE_IDS`.
- [ ] If the push ingress is not used, remove the `scp` service; if it is, set `RADFETCH_SCP_ALLOWED_AE_TITLES`.

## 4. Flows

- [ ] One YAML file per flow in `flows/`, with the image **pinned by digest**.
- [ ] Pre-pull every image on the host, so a registry outage cannot fail a job.
- [ ] Validate:

  ```bash
  docker compose run --rm api check-flows
  ```

- [ ] Run each image once by hand on a test series with the same mounts RadFetch uses (`/input` read-only, `/output`, no network) and confirm it exits 0 and writes its results to `/output`.

## 5. First start

- [ ] Start the stack:

  ```bash
  docker compose up -d --build
  ```

- [ ] Create the first administrator before exposing the API beyond localhost (until an account exists, anyone who can reach it can use it):

  ```bash
  docker compose exec api radfetch user create <your-name> --role admin
  ```

- [ ] Sign in at http://localhost:8000 with the printed password, change it under **Account**, and check that the Dashboard's PACS tile says "reachable".
- [ ] Create accounts for the people who will use it, with the lowest role that fits each (see [Users and permissions](users.md)).

## 6. Trial against the PACS

Do these with the PACS team watching their side.

- [ ] Submit **one** one-off job for a known accession number. In the job history, confirm the selected series are the ones you expect, the instance counts match the PACS, and the run and delivery succeed.
- [ ] Submit a job for an accession number that does not exist and confirm it fails with "no study found".
- [ ] Submit a batch of about ten during the batch window. Confirm on the PACS side that there is never more than one association from RadFetch and that the transfer rate stays within what was agreed.
- [ ] Pause the batch and confirm PACS traffic stops after the series in flight.
- [ ] Stop the gateway container mid-batch and start it again; confirm the batch continues and nothing is fetched twice.
- [ ] Disconnect the PACS (or block the port) for a few minutes; confirm the header shows "PACS breaker open", that RadFetch stops trying, and that it recovers by itself.

## 7. Operations

Day-to-day tasks are described in [Administration](administration.md). Before going live:

- [ ] Back up Postgres nightly (`pg_dump`); the cache and run directories do not need backup.
- [ ] Scrape `GET /metrics` and alert on `radfetch_gateway_breaker_open == 1`, a growing `radfetch_gateway_heartbeat_age_seconds`, and `radfetch_oldest_active_job_seconds` above your tolerance.
- [ ] Decide retention: `RADFETCH_CACHE_TTL_HOURS` for cached series and `RADFETCH_RUN_TTL_HOURS` for run inputs, outputs and logs. Both hold patient data.
- [ ] Give the executor's Docker socket access a second look: it is root-equivalent on the host. A socket proxy limited to container create, start, inspect, logs, kill and remove reduces that.

## Not verified by the project's tests

- Real GPU access. `tests/test_docker_runtime.py::test_real_gpu_is_visible_in_container` checks it and runs only on a host with the NVIDIA runtime, so run the suite once on the GPU host.
- Any vendor PACS: retrieval is tested against Orthanc and an in-memory fake only.
