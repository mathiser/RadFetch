# API reference

Base URL: `http://localhost:8000`. Send `Authorization: Bearer <token>` once
tokens exist (see [Administration](administration.md)). Bodies and responses are
JSON. Interactive documentation generated from the code is at `/docs`.

"Operator" endpoints need an `operator` token; the others work with `viewer` too.

## Status

| Method and path | Token | Returns |
|---|---|---|
| `GET /healthz` | none | Job counts by state and worker heartbeats |
| `GET /metrics` | any | Prometheus text metrics |
| `GET /whoami` | any | The token's name and role |
| `GET /flows` | any | Names of the available flows |

## Jobs

| Method and path | Token | Does |
|---|---|---|
| `POST /jobs` | operator | Submit a one-off job. Body: `{"accession_number": "...", "flow": "..."}`. Returns `{"id": ...}` |
| `GET /jobs?state=&batch_id=&limit=` | any | List jobs, newest first (limit up to 1000) |
| `GET /jobs/{id}` | any | One job with its `events` and `runs` |
| `POST /jobs/{id}/cancel` | operator | Cancel. A running container is stopped within a few seconds |
| `POST /jobs/{id}/retry?full=false` | operator | Retry a failed or cancelled job. Re-sends only, if just the delivery failed, unless `full=true`. `409` for other states |

## Batches

| Method and path | Token | Does |
|---|---|---|
| `POST /batches` | operator | Create a batch. Body: `{"name": "...", "flow": "...", "accession_numbers": ["...", ...]}`. Duplicates and blanks are dropped |
| `GET /batches?limit=` | any | Recent batches with job counts by state |
| `GET /batches/{id}` | any | One batch with job counts |
| `GET /batches/{id}/jobs.csv` | any | One row per job: accession number, state, error, study UID, job id, updated time |
| `POST /batches/{id}/pause` | operator | Stop starting new work for the batch |
| `POST /batches/{id}/resume` | operator | Continue |
| `POST /batches/{id}/cancel` | operator | Cancel every unfinished job |
| `POST /batches/{id}/retry-failed` | operator | Retry every failed job |

## Errors

| Status | Meaning |
|---|---|
| `401` | Missing or wrong token |
| `403` | The token is read-only |
| `404` | No such job or batch |
| `409` | The job is not in a state that allows the action |
| `422` | Invalid body, or unknown or invalid flow |

## Example

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $RADFETCH_TOKEN" -H 'content-type: application/json' \
  -d '{"accession_number": "ACC0001", "flow": "example"}'
```
