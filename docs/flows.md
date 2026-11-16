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
| `RADFETCH_STEP` | The step's name (`main` for a single-container flow) |
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
| `gpu` | Give the container the host's GPUs. Also decides which executors may run it: GPU executors if true, CPU executors if false | `false` |
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

## Several containers in one flow

A flow can run several containers, each reading the DICOM series, the output of
earlier containers, or both. Replace `image` with `steps`:

```yaml
name: lung-pipeline
select:
  - modality: CT
    series_description: "(?i)thorax"

steps:
  - name: segment
    image: registry.example/lung-seg@sha256:4f0c...
    gpu: true
    # no inputs given: the series are mounted at /input

  - name: measure
    image: registry.example/measure@sha256:91ab...
    inputs:
      series: /dicom        # the DICOM series
      segment: /masks       # everything the step "segment" wrote to /output

  - name: report
    image: registry.example/report@sha256:c07d...
    inputs:
      measure: /measurements   # this step does not need the DICOM at all

deliver: [report, segment]     # whose /output is sent to the destinations
results_from: measure          # whose results.json is stored
destinations:
  - type: dicom
    host: pacs.hospital.example
    port: 104
    ae_title: PACS
```

How it works:

- **Wiring.** `inputs` maps a source to a path inside the container. The source is `series` for the DICOM series, or the name of another step for that step's whole `/output` folder. Everything is mounted read-only; each step writes to its own `/output`.
- **Order.** A step runs after every step it reads from. The order in the file does not matter, and a circular dependency is rejected when the flow is loaded.
- **Who runs what.** Executors take single steps, not whole jobs. A step marked `gpu: true` is taken by a GPU executor, every other step by a CPU executor. So the GPU is free for the next job while a job's CPU steps run, and steps that do not depend on each other run at the same time when executors are free. Two GPU steps never share one GPU.
- **Failures.** If a step fails, only that step is retried (up to its `max_attempts`); the outputs of the steps before it are kept. If a step fails for good, the job fails and any of its other steps that are still running are stopped. Retrying a failed job by hand also continues at the failed step. `radfetch retry <job> --full` runs every step again.
- **Delivery and results.** By default the last step's output is delivered and its `results.json` is stored. `deliver` and `results_from` change that. With several delivered steps, a `directory` destination gets one subfolder per step.
- **Per step** you can set `image`, `command`, `gpu`, `env`, `timeout_s`, `max_attempts`, `mem_limit` and `shm_size`. The flow's `timeout_s`, `max_attempts` and `env` are the defaults.
- **Traceability.** Each step is a run of its own in the job's history, with its image digest and log, and `radfetch.json` lists every step.

A flow written with a single `image` is the same thing as a flow with one step
named `main`.

Coming from RadDeploy: a RadDeploy flow's list of models with named input and
output mounts corresponds to `steps` with `inputs`. The difference is that a step
has exactly one output folder, named after the step, instead of several named
output mounts.

## Reporting results

Besides files, a flow can report **structured results**: measurements, scores,
labels, quality flags, anything expressible as JSON. The container writes them to
`/output/results.json`:

```json
{"volume_ml": 412.5, "grade": 3, "nodules": [{"mm": 6, "lobe": "RUL"}, {"mm": 11, "lobe": "LLL"}]}
```

RadFetch stores the document with the job. Nothing has to be declared in advance:
each flow, and each version of it, can report whatever shape it likes. The results
can then be:

- seen on the job's page and on the **Results** page of the web interface, as a table with one column per top-level key;
- searched for documents *containing* some JSON, such as `{"grade": 3}` or `{"nodules": [{"lobe": "RUL"}]}`;
- exported as CSV per flow, or read as JSON through the API (see [API reference](api.md)).

Things to know:

- The file must hold a JSON object or array, be at most `RADFETCH_RESULTS_MAX_BYTES` (1 MB by default), and contain no NUL characters. A file that breaks these rules is not stored; the job still succeeds, and its history says why.
- One document is kept per job. A rerun replaces it.
- Use `results_file:` in the flow to name a different file under `/output`.
- The file is also delivered like any other output file to `directory` destinations.
- Results stay as long as the job exists; they are not affected by cache or run-file cleanup.
- Seeing them needs the `results.view` permission (role `viewer` and up). They are patient-related data even when they hold no identifiers.
- For large or binary results (masks, images, reports), write files and use a destination. The results store is for data you want to query.

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
