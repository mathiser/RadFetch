# Administration

Commands below are run on the host, in the folder with `docker-compose.yml`.

## Users and access

Accounts, roles, individual permissions, API tokens and the audit log are
described in [Users and permissions](users.md). In short:

```bash
docker compose exec api radfetch user create anna --role admin
```

- **The interface and API are open until the first account exists.** Create an administrator before making them reachable from other machines.
- Roles, from least to most: `monitor`, `viewer`, `submitter`, `operator`, `admin`. Each user can be given or denied single permissions and be limited to certain flows.
- `/healthz` needs no sign-in, so it can be used for health checks.

The API has no TLS of its own. To reach it from other machines, put a reverse
proxy (nginx, Caddy, Traefik) with HTTPS in front, set `RADFETCH_PUBLIC_URL` to
its `https://` address, and keep port 8000 bound to localhost, as it is in the
Compose file.

## Notifications

Set a webhook URL to be told when something needs attention:

```
RADFETCH_NOTIFY_WEBHOOK_URL=https://hooks.example/services/...
RADFETCH_PUBLIC_URL=https://radfetch.hospital.example
```

| Event | Sent when |
|---|---|
| `job_failed` | A one-off or pushed job fails |
| `batch_finished` | The last job of a batch finishes, with counts of done, failed and cancelled |
| `pacs_unreachable` | The circuit breaker opens. Once per outage |

Failures of jobs inside a batch are not sent one by one; they are in the
`batch_finished` summary and in the batch's CSV.

The message is JSON with an `event` field, event-specific fields, and a `text`
field containing a ready-made sentence. Slack-compatible incoming webhooks show
`text` directly; for other systems (Microsoft Teams workflows, e-mail gateways)
map the fields in the receiving workflow.

**Privacy.** A webhook usually leaves the hospital network. By default messages
therefore contain no accession numbers and no error text (errors can contain
accession numbers), only the job or batch identifier and a link. Set
`RADFETCH_NOTIFY_INCLUDE_IDENTIFIERS=true` only if the receiving system is
approved for patient-related data. Batch names are always included, so do not
put identifiers in them.

Notifications are sent by the `sender` service and retried with increasing
delays if the webhook is down.

## Monitoring

- **Dashboard**: active and finished jobs, failure rate, PACS state, instances fetched, disk use, and per-flow success rates and times, over the last 24 hours to 30 days.
- **`GET /healthz`**: the same as JSON, without a token.
- **`GET /metrics`**: Prometheus text format. Needs the `stats.view` permission: give the monitoring system a service account with the `monitor` role and a token.

| Metric | Alert when |
|---|---|
| `radfetch_gateway_breaker_open` | `1`: the PACS is not answering |
| `radfetch_gateway_heartbeat_age_seconds` | Keeps growing: the gateway is down or stuck in a very long retrieval |
| `radfetch_oldest_active_job_seconds` | Above your tolerance: work is not moving |
| `radfetch_jobs{state="failed"}` | Rises unexpectedly |
| `radfetch_cache_bytes`, `radfetch_disk_used_fraction` | For capacity planning; retrieval of batches stops at 80% disk use |
| `radfetch_gateway_batch_window_open` | Informational |
| `radfetch_incoming_studies` | Stays above 0: pushed studies are not being processed |

Logs:

```bash
docker compose logs -f --tail 100 gateway executor sender
```

## Backup and restore

Only Postgres needs backup. The cache can be re-fetched and run files are temporary.

```bash
docker compose exec -T postgres pg_dump -U radfetch radfetch | gzip > radfetch-$(date +%F).sql.gz
```

To restore into an empty database:

```bash
gunzip -c radfetch-2026-01-31.sql.gz | docker compose exec -T postgres psql -U radfetch radfetch
```

After a restore, the cache folder may contain series the database does not know,
and the reverse. Both are handled: unknown files are harmless, and a job whose
cached files are missing fetches them again.

## Retention of patient data

Three places hold patient data on disk, all under the data folder:

| Folder | Holds | Removed |
|---|---|---|
| `cache/` | Series fetched or received | When no job needs them and they were unused for `RADFETCH_CACHE_TTL_HOURS`; sooner under disk pressure |
| `runs/` | Per run: links to the input, the output, the container log | `RADFETCH_RUN_TTL_HOURS` after the job finished |
| `incoming/` | Pushed studies not yet processed | As soon as they are processed |

`RADFETCH_KEEP_FAILED_HOURS` keeps the input and run files of failed jobs longer,
so they can be inspected and retried without asking the PACS again.

The database keeps accession numbers, UIDs and the event history indefinitely.
If you need to remove old jobs, delete them from the `jobs` table; their runs,
deliveries and series links go with them.

Results delivered to a `directory` destination are yours to manage; RadFetch
never deletes them.

## Upgrading

```bash
git pull
```

```bash
docker compose up -d --build
```

Schema changes are applied automatically at start by the `init-db` service. A
flow container that is running during the upgrade keeps running; the new
executor picks it up and waits for it.

Take a database backup before upgrading.

## GPUs

- A flow with `gpu: true` gets the host's GPUs through the NVIDIA Container Toolkit.
- One executor runs one container at a time. With several GPUs, run one executor per GPU, each pinned with `RADFETCH_GPU_DEVICE_IDS`:

```yaml
  executor-gpu1:
    <<: *app
    command: executor
    environment:
      <<: *env
      RADFETCH_GPU_DEVICE_IDS: "1"
    volumes:
      - ./data:/data
      - ./flows:/flows:ro
      - /var/run/docker.sock:/var/run/docker.sock
```

  and set `RADFETCH_GPU_DEVICE_IDS: "0"` on the original `executor` the same way.
- Flows with `gpu: false` are run by the same executors, one at a time each.
- `RADFETCH_DEBUG_FAKE_GPU=true` is for development machines without a GPU. Never set it in production: model images would run on CPU or fail.

## Running redundant services

See [High availability](high-availability.md).

## Stopping work

| To | Do |
|---|---|
| Stop one batch | **Pause** on the Batches page. Nothing new starts; a running container finishes |
| Stop all PACS traffic now | `docker compose stop gateway`. Jobs wait and continue when it is started again |
| Stop all inference | `docker compose stop executor`. A running flow container keeps running until it finishes and is picked up at restart |
| Stop everything | `docker compose stop` |

## Security notes

- The executor has the host's Docker socket, which is root-equivalent on the host. A socket proxy that only allows container create, start, inspect, logs, kill and remove reduces this.
- Flow containers run without network by default. Keep it that way unless a flow needs otherwise.
- The push ingress accepts any sender unless `RADFETCH_SCP_ALLOWED_AE_TITLES` or TLS with client certificates is configured. AE titles are not secret, so also restrict the port at the network level.
- Change the Postgres password in `docker-compose.yml` from the default.
