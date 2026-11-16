# API reference

Base URL: `http://localhost:8000`. Authenticate with `Authorization: Bearer <token>`
(a user's API token) or with the session cookie set by `POST /auth/login`. Bodies
and responses are JSON. Interactive documentation generated from the code is at `/docs`.

The "Needs" column names the permission required; see [Users and permissions](users.md).
Jobs and batches of flows an account is not allowed to see behave as if they did not exist.

## Sign-in and own account

| Method and path | Needs | Does |
|---|---|---|
| `POST /auth/login` | | Body `{"username", "password"}`. Sets the session cookie |
| `POST /auth/logout` | | Ends the session |
| `GET /whoami` | signed in | Name, role, effective permissions, allowed flows |
| `GET /roles` | signed in | The roles and every permission |
| `POST /me/password` | signed in | Body `{"current_password", "new_password"}`. Signs out everywhere |
| `GET /me/tokens`, `POST /me/tokens`, `DELETE /me/tokens/{name}` | signed in | List, create (shown once) and revoke own API tokens |

## Status

| Method and path | Needs | Returns |
|---|---|---|
| `GET /healthz` | nothing | Job counts by state and worker heartbeats |
| `GET /stats/overview?hours=24` | `stats.view` | The dashboard's numbers: finished and fetched per hour, per-flow figures, cache, gateway state |
| `GET /metrics` | `stats.view` | Prometheus text metrics |
| `GET /flows`, `GET /flows/{name}` | `jobs.view` | Flow names; one flow's definition (without its `env`) |
| `GET /audit?limit=` | `audit.view` | Latest events, newest first |

## Jobs

| Method and path | Needs | Does |
|---|---|---|
| `POST /jobs` | `jobs.submit` | Submit a one-off job. Body: `{"accession_number": "...", "flow": "..."}`. Returns `{"id": ...}` |
| `GET /jobs?state=&batch_id=&search=&limit=` | `jobs.view` (`search` also `identifiers.view`) | List jobs, newest first (limit up to 1000). `search` matches the start of the accession number |
| `GET /jobs/{id}` | `jobs.view` | One job with its `events` and `runs` |
| `POST /jobs/{id}/cancel` | own: `jobs.manage_own`; others': `jobs.manage_all` | Cancel. A running container is stopped within a few seconds |
| `POST /jobs/{id}/retry?full=false` | as cancel | Retry a failed or cancelled job. Re-sends only, if just the delivery failed, unless `full=true`. `409` for other states |

## Results

| Method and path | Needs | Does |
|---|---|---|
| `GET /jobs/{id}/result` | `results.view` | The structured result the flow reported for the job (`404` if none) |
| `GET /results?flow=&contains=&limit=` | `results.view` | Result documents, newest first. `contains` is JSON; only documents containing it are returned |
| `GET /results.csv?flow=&contains=` | `results.view` | One flow's results as a table: a column per top-level key, nested values as JSON text |
| `GET /results/flows` | `results.view` | Flows that have results, with counts |

Accession numbers and study UIDs in these responses are blank without `identifiers.view`.

```bash
curl -G http://localhost:8000/results -H "Authorization: Bearer $RADFETCH_TOKEN" \
  --data-urlencode 'flow=lung-seg' --data-urlencode 'contains={"grade": 3}'
```

## Batches

| Method and path | Needs | Does |
|---|---|---|
| `POST /batches` | `batches.submit` | Create a batch. Body: `{"name": "...", "flow": "...", "accession_numbers": ["...", ...]}`. Duplicates and blanks are dropped |
| `GET /batches?limit=` | `jobs.view` | Recent batches with job counts by state |
| `GET /batches/{id}` | `jobs.view` | One batch with job counts |
| `GET /batches/{id}/jobs.csv` | `identifiers.view` | One row per job: accession number, state, error, study UID, job id, updated time |
| `POST /batches/{id}/pause`, `/resume`, `/cancel`, `/retry-failed` | own: `jobs.manage_own`; others': `jobs.manage_all` | Pause, continue, cancel every unfinished job, retry every failed job |

## User administration

All need `users.manage`.

| Method and path | Does |
|---|---|
| `GET /users` | Accounts with role, adjustments, effective permissions and token count |
| `POST /users` | Body: `{"username", "role", "password"?, "service_account"?, "grants"?, "denies"?, "allowed_flows"?}`. Without a password one is generated and returned once |
| `PATCH /users/{name}` | Any of `role`, `grants`, `denies`, `allowed_flows`, `all_flows: true`, `disabled`, `password` |
| `DELETE /users/{name}` | Delete the account and its tokens |
| `GET /users/{name}/tokens`, `POST /users/{name}/tokens`, `DELETE /users/{name}/tokens/{token}` | List, create (shown once) and revoke a user's API tokens |

## Errors

| Status | Meaning |
|---|---|
| `401` | Missing or wrong token |
| `403` | The account lacks the permission, or may only manage its own work |
| `404` | No such job or batch |
| `409` | The job is not in a state that allows the action; a username or token name is taken; or the change would leave no administrator |
| `422` | Invalid body, or unknown or invalid flow |

## Example

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $RADFETCH_TOKEN" -H 'content-type: application/json' \
  -d '{"accession_number": "ACC0001", "flow": "example"}'
```
