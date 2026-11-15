# Writing flows

A flow is one YAML file in the `flows/` folder. The file name (without `.yaml`)
must equal the flow's `name`. Files are read each time they are used, so an edit
takes effect on the next job without restarting anything.

## A complete example

```yaml
name: lung-seg
version: "2.1"

select:
  - modality: CT
    series_description: "(?i)thorax|lung"
    min_instances: 50
pick: largest
multiple_studies: fail

image: registry.example/lung-seg@sha256:4f0c...
gpu: true
timeout_s: 1800
max_attempts: 2
shm_size: 2g
env:
  MODEL: v2

destinations:
  - type: dicom
    host: pacs.hospital.example
    port: 104
    ae_title: PACS
  - type: directory
    path: /data/results

trigger:
  calling_ae_title: "^CT_"
```

Validate every flow file with:

```bash
docker compose run --rm api check-flows
```

## Selecting series

`select` is a list of rules. A series is selected when it matches **any** rule;
inside a rule, **every** condition given must hold. Only selected series are
fetched from the PACS.

| Condition | Meaning |
|---|---|
| `modality` | Exact modality, e.g. `CT` |
| `series_description` | Regular expression searched in the series description. `(?i)` makes it case-insensitive |
| `min_instances` | At least this many instances (default 1) |
| `max_instances` | At most this many |

`pick: largest` keeps only the selected series with the most instances; the
default `all` keeps every selected series.

If no series matches, the job fails with "none of the study's N series match flow …".

`multiple_studies` says what to do when an accession number maps to several
studies: `fail` (default) fails the job; `all` runs once on the matching series
of all of them.

## The container contract

| | |
|---|---|
| Input | `/input/<SeriesInstanceUID>/<SOPInstanceUID>.dcm`, read-only |
| Output | Write results to `/output` |
| Success | Exit code 0. Any other exit code is a failed run |
| Network | None by default (`RADFETCH_CONTAINER_NETWORK`) |
| Time limit | `timeout_s`; the container is killed when it is exceeded |

Environment variables set for every run:

| Variable | Value |
|---|---|
| `RADFETCH_JOB_ID`, `RADFETCH_RUN_ID` | Identifiers of the job and this run |
| `RADFETCH_ACCESSION_NUMBER` | The accession number |
| `RADFETCH_STUDY_INSTANCE_UID` | The study (the first by UID when there are several) |
| `RADFETCH_FLOW` | The flow name |
| `RADFETCH_FAKE_GPU` | `1` only when the executor runs with the fake-GPU debug flag |

Plus anything under `env` in the flow.

Flow images often run as a non-root user, so `/output` is world-writable.

## Image and resources

| Field | Meaning | Default |
|---|---|---|
| `image` | The image to run. Pin it by digest (`name@sha256:…`) in production | required |
| `command` | Overrides the image's default command | image default |
| `gpu` | Give the container the host's GPUs | `false` |
| `timeout_s` | Kill the container after this long | 3600 |
| `max_attempts` | Runs before the job fails | 2 |
| `mem_limit`, `shm_size` | Docker memory and shared-memory limits, e.g. `16g`, `2g` | none |
| `version` | Your own version label, recorded on each job | `"1"` |

Pre-pull images on the host. The executor will pull a missing image, but then a
registry outage fails the job.

## Destinations

A flow can have any number of destinations; each is delivered and retried
separately, and a failed delivery never repeats inference.

| Type | Fields | What is sent |
|---|---|---|
| `directory` | `path` | Everything in `/output`, copied to `<path>/<job id>/` |
| `dicom` | `host`, `port`, `ae_title`, optional `tls: true` | Every DICOM file in `/output`, by C-STORE |
| `stow` | `url`, optional `token_env` | Every DICOM file in `/output`, by STOW-RS to `<url>/studies` |

- A `directory` path is a path **inside the sender container**. `/data/...` works out of the box (it is `./data` on the host); for anywhere else, mount that folder into the `sender` service.
- `dicom` and `stow` skip files that are not DICOM, and fail if there are none.
- `token_env` names an environment variable of the `sender` service that holds a bearer token, so the token is never written in the flow file.
- A flow with no destinations is `done` as soon as its run succeeds; the output stays in the run folder until it is cleaned up.

Every successful run also gets a `radfetch.json` in its output, recording the
job, flow version, exact image digest and input series.

## Triggers (push)

Without a `trigger`, a flow runs only when a job is submitted for it. With one,
studies pushed to the storage SCP start it too, when:

1. the sender's AE title matches `calling_ae_title` (a regular expression; omit it to accept any sender), and
2. the flow's `select` rules match at least one received series.

## Building a flow image

A minimal Python example:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir pydicom
COPY run.py /run.py
USER 1000
ENTRYPOINT ["python", "/run.py"]
```

```python
# run.py
import pathlib, pydicom

for series in pathlib.Path("/input").iterdir():
    slices = [pydicom.dcmread(f) for f in sorted(series.glob("*.dcm"))]
    # ... run the model, build result objects ...
    # result.save_as(f"/output/{series.name}-result.dcm")
```

Test it by hand with the same mounts RadFetch uses before adding the flow:

```bash
docker run --rm --network none -v /path/to/test-input:/input:ro -v /tmp/out:/output your-image
```
