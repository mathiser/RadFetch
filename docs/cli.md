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
| `user create <name> [--role R] [--flows a,b] [--service-account] [--password-stdin]` | Create an account. Prints a generated password once |
| `user set <name> [--role R] [--grant P] [--deny P] [--clear-adjustments] [--flows a,b] [--all-flows] [--disable] [--enable]` | Change role, individual permissions, flow restriction, or disable |
| `user passwd <name> [--password-stdin]` | Set a new password and sign the user out everywhere |
| `user list`, `user delete <name>`, `user roles` | List accounts, delete one, show roles and permissions |
| `token create <username> [--name label]` | Create an API token for an account and print it once |
| `token list`, `token revoke <label>` | List tokens, revoke one |
| `check-flows` | Validate every flow file; exits non-zero if one is invalid |
| `init-db` | Apply pending database schema changes. Compose runs this at every start |

## Services

Started by Compose; you rarely run these by hand.

| Command | Service |
|---|---|
| `api [--host] [--port]` | HTTP API and UI |
| `gateway` | PACS gateway |
| `executor` | Container executor; its kind comes from `RADFETCH_EXECUTOR_KIND` |
| `sender` | Result delivery and notifications |
| `scp` | Push ingress |
| `janitor` | Cleanup |

## Development helpers

| Command | Does |
|---|---|
| `fake-pacs [--port] [--studies] [--ae-title] [--move-dest AET=host:port]` | An in-memory PACS with synthetic studies `ACC0001`… |
| `fake-push [--host] [--port] [--calling-aet] [--accession-number] [--instances]` | Send one synthetic CT study to the push ingress |
