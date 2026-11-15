# Command line reference

The `radfetch` command is inside every service container:

```bash
docker compose exec api radfetch <command>
```

The command line talks to the database directly, so it needs no token. Anyone
who can run it on the host is an administrator.

## Work

| Command | Does |
|---|---|
| `submit <accession number> <flow>` | Submit a one-off job; prints its id |
| `batch <name> <flow> <file>` | Create a batch from a text file with one accession number per line. Lines starting with `#` are ignored |
| `status` | Job counts by state |
| `status <job id>` | One job with its history |
| `batch-status <batch id>` | One batch with job counts |
| `cancel <job id>` | Cancel a job |
| `retry <job id> [--full]` | Retry a failed or cancelled job |
| `pause <batch id>` / `resume <batch id>` | Pause or resume a batch |
| `cancel-batch <batch id>` | Cancel every unfinished job of a batch |
| `retry-failed <batch id>` | Retry every failed job of a batch |

To create a batch from a file on the host, pass it on standard input:

```bash
docker compose exec -T api radfetch batch "March cohort" example /dev/stdin < accessions.txt
```

## Administration

| Command | Does |
|---|---|
| `token create <name> [--role viewer\|operator]` | Create a named API token and print it once |
| `token list` | List tokens with role and revocation time |
| `token revoke <name>` | Revoke a token |
| `check-flows` | Validate every flow file; exits non-zero if one is invalid |
| `init-db` | Apply pending database schema changes. Compose runs this at every start |

## Services

Started by Compose; you rarely run these by hand.

| Command | Service |
|---|---|
| `api [--host] [--port]` | HTTP API and UI |
| `gateway` | PACS gateway |
| `executor` | Container executor |
| `sender` | Result delivery and notifications |
| `scp` | Push ingress |
| `janitor` | Cleanup |

## Development helpers

| Command | Does |
|---|---|
| `fake-pacs [--port] [--studies] [--ae-title] [--move-dest AET=host:port]` | An in-memory PACS with synthetic studies `ACC0001`… |
| `fake-push [--host] [--port] [--calling-aet] [--accession-number] [--instances]` | Send one synthetic CT study to the push ingress |
