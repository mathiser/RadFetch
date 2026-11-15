# Concepts

## The two ways in

```
pull: UI / API / CLI ──> jobs (Postgres) ──> gateway ──┐
                                               │ C-FIND, C-GET / C-MOVE / DICOMweb
                                              PACS     ├─> series cache ──> executor ──> sender ──> destinations
push: modality / PACS ── C-STORE ──> scp ──────────────┘                      │ Docker
                                                                         flow container
```

- **Pull.** You give RadFetch an accession number and a flow. It looks the study up in the PACS, fetches only the series the flow asks for, and runs the flow.
- **Push.** A modality or PACS sends a study to RadFetch's storage SCP. Every flow that has a `trigger` matching the sender, and whose series rules match what arrived, is started.

Both paths end in the same place: series in the local cache and a job ready for the executor.

## Services

| Service | What it does | How many |
|---|---|---|
| `postgres` | The only state store: jobs, retrievals, runs, deliveries, audit events | 1 |
| `api` | HTTP API and web UI | 1 |
| `gateway` | The only process that talks to the PACS | exactly 1 |
| `scp` | Push ingress (storage SCP) | 0 or 1 |
| `executor` | Runs one flow container at a time | 1 per GPU |
| `sender` | Delivers results; retried independently of inference | 1 |
| `janitor` | Evicts unused cached series, removes old run files | 1 |

## Words used throughout

- **Flow**: a YAML file saying which series to use, which container image to run, and where results go. See [Writing flows](flows.md).
- **Job**: one flow on one accession number (or one pushed study).
- **One-off job**: a single job submitted for immediate attention. It goes ahead of batch work.
- **Batch**: a named list of accession numbers for one flow. Batches can be paused, resumed and cancelled as a whole.
- **Retrieval**: one series in the local cache. Several jobs can share one retrieval, so a series is fetched once however many flows use it.
- **Run**: one execution of a flow's container for a job. A job can have several runs if one fails and is retried.
- **Delivery**: sending one run's output to one destination.

## Job states

```
queued ─> retrieving ─> ready ─> running ─> sending ─> done
   any state before done ─> failed | cancelled
```

| State | Meaning | Who moves it on |
|---|---|---|
| `queued` | Waiting for the gateway to look the study up in the PACS | gateway |
| `retrieving` | Study found; waiting for its series to be fetched | gateway |
| `ready` | All series are in the cache; waiting for the executor | executor |
| `running` | The flow's container is running | executor |
| `sending` | The run succeeded; results are being delivered | sender |
| `done` | All deliveries succeeded | |
| `failed` | Gave up; the job's `error` says why. Can be retried | you |
| `cancelled` | Cancelled by a user. Can be retried | you |

Pushed jobs start at `ready`.

## How the PACS is protected

All of this is enforced by the gateway.

| Protection | What it does | Applies to one-off jobs |
|---|---|---|
| Single association | The gateway does one PACS operation at a time | yes |
| Selective retrieval | Only series a flow selects are fetched, and each only once | yes |
| Minimum interval | A pause between any two PACS operations | yes |
| Circuit breaker | After repeated PACS failures all traffic stops for a cooldown that doubles while failures continue | yes |
| Hard disk limit | No retrieval above 95% disk usage | yes |
| Rate budget | Caps average instances per minute | no |
| Batch window | Batch work only inside configured hours | no |
| Bounded preloading | Batch studies are fetched only a few ahead of the executor | no |
| Disk watermark | Batch retrieval stops at 80% disk usage | no |

One-off jobs skip the last four so that an urgent request is not held up by a
running batch. They can never open a second association or push past an open
circuit breaker.

## Priorities

One-off jobs run first, then pushed jobs, then batch jobs. Priority applies both
when fetching and when the executor picks its next job. A running container is
never interrupted for a more urgent job.

## Crash safety

Workers claim work with a lease. If a worker dies, its lease expires and the work
becomes claimable again; nothing needs cleaning up. After an executor crash or
restart, the next executor finds the container that was running and waits for it
instead of starting inference again.
