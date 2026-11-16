# Users and permissions

Everyone who uses the web interface or the API has an account. An account has a
**role**, which sets what it may do, and can be adjusted individually: single
permissions can be added or removed, and the account can be limited to certain
flows.

## Roles

Each role includes everything the role before it may do.

| Role | Meant for | Adds |
|---|---|---|
| `monitor` | IT monitoring, wall displays | See dashboards, and jobs and batches with their states, **without** patient identifiers |
| `viewer` | Clinicians and researchers who follow up results | See accession numbers, study UIDs, error texts and job histories; download batch CSVs; see, search and export the results flows report |
| `submitter` | People who run flows | Submit one-off jobs and batches; cancel, retry, pause and resume **their own** |
| `operator` | The team that runs the service | Cancel, retry, pause and resume **anyone's** jobs and batches |
| `admin` | Administrators | Manage users and tokens; read the audit log |

## Permissions

A role is a set of these permissions. Any of them can be granted to or removed
from a single user.

| Permission | Allows | From role |
|---|---|---|
| `jobs.view` | See jobs, batches and their states | monitor |
| `stats.view` | See dashboards and metrics | monitor |
| `identifiers.view` | See accession numbers, study UIDs, error texts and job histories | viewer |
| `results.view` | See, search and export the structured results flows report | viewer |
| `jobs.submit` | Submit one-off jobs | submitter |
| `batches.submit` | Submit batches | submitter |
| `jobs.manage_own` | Cancel, retry, pause and resume what they submitted | submitter |
| `jobs.manage_all` | Cancel, retry, pause and resume anyone's work | operator |
| `audit.view` | See the audit log | admin |
| `users.manage` | Create, change and remove users and their tokens | admin |

A user's effective permissions are the role's permissions, plus individual
grants, minus individual denies.

### Examples of individual adjustments

| Need | Set-up |
|---|---|
| A researcher who may run only the `lung-seg` flow | Role `submitter`, limited to flow `lung-seg` |
| A technician who may submit one-off jobs but not batches | Role `submitter`, deny `batches.submit` |
| A quality manager who reads the audit log but changes nothing | Role `viewer`, grant `audit.view` |
| A statistician who follows throughput without seeing identifiers | Role `monitor` |
| A researcher who analyses results without knowing whose they are | Role `viewer`, deny `identifiers.view` |
| A pipeline that submits jobs from another system | Service account with role `submitter` and an API token |

## Flow restrictions

An account limited to certain flows sees and can act on only the jobs and
batches of those flows. Everything else is hidden from it, including in the
dashboard figures and the audit log; asking for a hidden job gives the same
"not found" as a job that does not exist.

## What "without identifiers" means

Without `identifiers.view`, a user still sees that a job exists, its flow, state,
times and who submitted it. Accession numbers and study UIDs are blank, error
texts are replaced by "(hidden)", job histories show only the step names, the
accession-number search and the batch CSV are unavailable, and job events in the
audit log show only who acted.

Batch names are shown to everyone who can see the batch, so do not put patient
identifiers in them.

## The first administrator

Until the first account exists, the interface and API are open to anyone who can
reach them, with operator rights. Create an administrator before making the
service reachable from other machines:

```bash
docker compose exec api radfetch user create anna --role admin
```

The command prints a generated password once. Sign in at the web interface and
change it under **Account**.

## Managing users

In the web interface, **Users** (visible to administrators) lists the accounts.
Select one to change its role, set each permission to "as the role", "always
allow" or "never allow", limit it to flows, reset its password, create an API
token for it, disable or delete it.

The same from the command line:

```bash
docker compose exec api radfetch user create ben --role submitter --flows lung-seg
```

```bash
docker compose exec api radfetch user set ben --deny batches.submit --grant audit.view
```

```bash
docker compose exec api radfetch user set ben --role operator --clear-adjustments --all-flows
```

```bash
docker compose exec api radfetch user set ben --disable
```

```bash
docker compose exec api radfetch user passwd ben
```

```bash
docker compose exec api radfetch user list
```

`radfetch user roles` prints the roles and permissions.

Things to know:

- **Changes apply at once**, on the user's next request.
- **Disabling or changing a password signs the user out everywhere.** Their API tokens stop working while the account is disabled.
- **There is always an administrator.** RadFetch refuses a change that would leave no enabled administrator who can sign in.
- **Deleting a user** removes their tokens. Their jobs remain, with the name they were submitted under.
- **Lockout.** Ten wrong passwords or codes in a row lock an account for 15 minutes. An administrator can lift it at once by resetting the password.
- Passwords must be at least 10 characters and are stored only as a salted scrypt hash.

## API tokens

A token lets a script or another system act as a user, with that user's rights.

- Users create and revoke their own tokens under **Account**.
- Administrators can create a token for any user, from the user's page or with `radfetch token create <username>`.
- A token is shown once, when created. Only a hash is stored.
- For another system, create a **service account** (a user without a password) with the lowest role that works, and give it a token.

Send the token as `Authorization: Bearer <token>`.

`RADFETCH_API_TOKEN` in `.env`, if set, is one shared token with operator rights
that belongs to no account. It exists for simple set-ups; prefer accounts, which
show who did what and can be withdrawn one at a time.

## The audit log

Every submission, cancellation, retry, pause and resume, and every change to a
user or token, is recorded with the time and who did it. Administrators (or
anyone granted `audit.view`) read it under **Audit log** or at `GET /audit`.
Each job's own history is on the job's page.

Actions taken on the command line are recorded as done by `cli`: anyone who can
run commands on the host is in effect an administrator.

## Signing in

- **Password accounts** sign in with username and password, optionally (or mandatorily) with a code from an authenticator app.
- **Single sign-on** lets people use your organisation's identity provider; their role can follow their directory groups.

Both are described in [Single sign-on and two-step sign-in](sso.md).

Sessions are a cookie that is `HttpOnly` and `SameSite=Strict`, and marked
`Secure` when `RADFETCH_PUBLIC_URL` starts with `https://`. Serve the interface
over HTTPS through a reverse proxy and set that variable. A session lasts
`RADFETCH_SESSION_HOURS` (12 by default).
