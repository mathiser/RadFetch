# Administration

Commands below are run on the host, in the folder with `docker-compose.yml`.

## Users and tokens

Access to the API and UI is by named bearer tokens.

```bash
docker compose exec api radfetch token create anna --role operator
```

```bash
docker compose exec api radfetch token list
```

```bash
docker compose exec api radfetch token revoke anna
```

- The token is printed once at creation. Only a hash is stored, so a lost token cannot be recovered: revoke it and create a new one.
- **`operator`** tokens can submit, cancel, retry and pause. **`viewer`** tokens are read-only, for a status screen or a monitoring system.
- The token's name is recorded as the requester of each job and on every cancel, retry and pause, visible in the job history.
- **The API is open until the first token exists.** Create a token before making the API reachable from other machines.
- `RADFETCH_API_TOKEN` in `.env` additionally defines one shared operator token. Prefer named tokens: they can be revoked one by one and show who did what.
- `/healthz` needs no token, so it can be used for health checks.

The API has no TLS of its own. To reach it from other machines, put a reverse
proxy (nginx, Caddy, Traefik) with HTTPS in front and keep port 8000 bound to
localhost, as it is in the Compose file.

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

- **UI header**: job counts, gateway heartbeat, circuit breaker and batch window at a glance.
- **`GET /healthz`**: the same as JSON, without a token.
- **`GET /metrics`**: Prometheus text format (needs a token once tokens exist).

| Metric | Alert when |
|---|---|
| `radfetch_gateway_breaker_open` | `1`: the PACS is not answering |
| `radfetch_gateway_heartbeat_age_seconds` | Keeps growing: the gateway is down or stuck in a very long retrieval |
| `radfetch_oldest_active_job_seconds` | Above your tolerance: work is not moving |
| `radfetch_jobs{state="failed"}` | Rises unexpectedly |
| `radfetch_cache_bytes` | For capacity planning |
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

## Stopping work

| To | Do |
|---|---|
| Stop one batch | **Pause** in the UI. Nothing new starts; a running container finishes |
| Stop all PACS traffic now | `docker compose stop gateway`. Jobs wait and continue when it is started again |
| Stop all inference | `docker compose stop executor`. A running flow container keeps running until it finishes and is picked up at restart |
| Stop everything | `docker compose stop` |

## Security notes

- The executor has the host's Docker socket, which is root-equivalent on the host. A socket proxy that only allows container create, start, inspect, logs, kill and remove reduces this.
- Flow containers run without network by default. Keep it that way unless a flow needs otherwise.
- The push ingress accepts any sender unless `RADFETCH_SCP_ALLOWED_AE_TITLES` or TLS with client certificates is configured. AE titles are not secret, so also restrict the port at the network level.
- Change the Postgres password in `docker-compose.yml` from the default.
