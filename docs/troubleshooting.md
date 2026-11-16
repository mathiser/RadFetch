# Troubleshooting

Start with the job's history: click the job in the UI, or

```bash
docker compose exec api radfetch status <job id>
```

It lists every step with its time and detail. Service logs have more:

```bash
docker compose logs --tail 100 gateway executor-gpu executor-cpu sender scp
```

## Job errors

| Error on the job | Meaning | What to do |
|---|---|---|
| `no study found for accession number …` | The PACS returned no study | Check the number. Check that RadFetch's AE title may query that PACS |
| `N studies found, expected one …` | The accession number maps to several studies | Set `multiple_studies: all` in the flow if it should run on all of them |
| `none of the study's N series match flow …` | The flow's `select` rules matched nothing | Compare the rules with the study's series descriptions and modalities |
| `PACS query failed: …` | The lookup failed repeatedly | See "The PACS is not reachable" below, then retry the job |
| `retrieval of series … failed: incomplete: received X, expected Y, failed Z` | Fewer instances arrived than the PACS announced | Often a storage class the PACS could not send (see `get` mode in [Connecting a PACS](pacs.md)), or the PACS dropped the transfer. Retry |
| `retrieval of series … failed: …` (other) | The transfer failed repeatedly | Check the gateway log; retry |
| `PACS returned a malformed UID` | A UID was not plain digits and dots | Data problem in the PACS; RadFetch refuses it because UIDs become folder names |
| `unknown flow: …` / `invalid flow file …` | The flow file is missing or invalid | `docker compose run --rm api check-flows` |
| `cannot start container: …` | Docker could not start the flow's container | See "Containers do not start" below |
| `container exited with code N` | The flow's program failed | Read the container log (below) |
| `timed out after Ns` | The run exceeded the flow's `timeout_s` | Raise `timeout_s` or look at why the run is slow |
| `gave up after N run attempts` / `gave up after N attempts of step …` | Every allowed run of that step failed | Fix the cause, then retry the job; it continues at that step |
| `the flow was changed while this job was running …` | A step the job had laid out no longer exists in the flow file, or reads from different steps | `radfetch retry <job id> --full` |
| `step NAME: …` | The error is from that step of a multi-step flow | As for the error after the step name |
| `delivery failed: …` | A destination kept refusing the results | Fix the destination, then retry: only the delivery is repeated |
| `delivery failed: run produced no DICOM files` | A `dicom` or `stow` destination found nothing to send | The flow's container must write DICOM files to `/output` |
| `delivery failed: run output directory is missing` | The run files were already cleaned up | Retry with a full rerun: `radfetch retry <job id> --full` |

### Reading a container's log

Each run's output of the container is kept with its run files:

```bash
sudo less data/runs/<run id>/container.log
```

The run id is in the job history (`run_started`). Run files are removed
`RADFETCH_RUN_TTL_HOURS` after the job finished; set `RADFETCH_KEEP_FAILED_HOURS`
to keep those of failed jobs longer.

## Symptoms

### Jobs stay `queued`

| Check | Likely cause |
|---|---|
| The Dashboard's PACS tile says "batch window closed" | Batch jobs wait for the window (`RADFETCH_BATCH_WINDOWS`, local time, `TZ`) |
| The PACS tile says "not answering" | The PACS is not answering; see below |
| The PACS tile says "gateway silent" or "unknown" | The gateway is not running: `docker compose ps gateway`, then its log |
| The batch shows "(paused)" | Resume it |
| Many jobs are `retrieving` | Normal: batch jobs are looked up only `RADFETCH_MAX_RESOLVED_AHEAD` ahead |

### Jobs stay `retrieving`

| Check | Likely cause |
|---|---|
| Several jobs are `ready` | Normal: batch fetching pauses while `RADFETCH_MAX_STAGED_JOBS` jobs wait for an executor. If they stay `ready`, see the next section |
| Disk is nearly full | Batch retrieval stops at 80% usage, everything at 95%. Free space or lower the retention settings |
| Gateway log shows retries | A series keeps failing; it is retried with growing delays and then fails the job |
| Slow but moving | The rate budget (`RADFETCH_RATE_INSTANCES_PER_MIN`) is doing its job |

### Jobs stay `ready`, or `running` with nothing happening

No executor is taking the next step.

| Check | Likely cause |
|---|---|
| The Dashboard's Executors tile is red | No executor of the kind the waiting step needs: a `gpu: true` step needs `executor-gpu`, every other step needs `executor-cpu`. Start one, or correct the flow's `gpu` setting |
| Executors are listed and busy | They are occupied; each runs one container at a time. Add CPU executors, or wait for the GPU |
| A job is `running` and its history shows a failed run | The step is waiting for its next attempt; the delay grows with each attempt |
| The batch shows "(paused)" | Resume it |
| Otherwise | `docker compose ps executor-gpu executor-cpu`, then their logs |

### Jobs stay `sending`

A destination is refusing or unreachable; the sender retries with growing delays
up to `RADFETCH_DELIVER_MAX_ATTEMPTS`. The sender log names the reason.

### The PACS is not reachable

The gateway log shows the cause. Common ones:

| Log says | Cause |
|---|---|
| `cannot connect to PACS` | Wrong host or port, or a firewall |
| `association with PACS rejected, aborted or timed out` | The PACS does not accept RadFetch's AE title, the PACS's AE title is wrong, or TLS is expected on one side only |
| `C-FIND failed with status 0x…` | The PACS refused the query |
| `C-MOVE failed with status 0xA801` | The PACS does not know RadFetch's AE title as a move destination |
| C-MOVE succeeds but nothing arrives | The PACS cannot reach this host on port 11113 |
| `QIDO-RS returned HTTP 401` / `403` | Missing or wrong DICOMweb token |
| certificate verify failed | The PACS certificate is not signed by `RADFETCH_TLS_CA_FILE`, or is not valid for the host name in `RADFETCH_PACS_HOST` |

After repeated failures the circuit breaker pauses all PACS traffic and tries
again by itself, first after a minute and then at doubling intervals up to half
an hour. Restarting the gateway makes it try at once.

### Containers do not start

| `cannot start container` says | Cause |
|---|---|
| `could not select device driver … capabilities: [[gpu]]` | The host has no NVIDIA Container Toolkit, or the flow says `gpu: true` on a host without a GPU |
| `pull access denied` / `not found` | The image name is wrong or the host is not logged in to the registry. Pre-pull the image on the host |
| `invalid mount config` / `bind source path does not exist` | `RADFETCH_HOST_DATA_DIR` is not the host's absolute path of the data folder |
| `permission denied … docker.sock` | The executor cannot use the Docker socket |

### Pushed studies do not start a job

| Check | Cause |
|---|---|
| The sender reports the association was rejected | Its AE title is not in `RADFETCH_SCP_ALLOWED_AE_TITLES`, or TLS settings differ |
| Nothing happens for a while | The study is processed only after `RADFETCH_SCP_STABLE_S` quiet seconds |
| `scp` log shows "0 job(s)" for the study | No flow has a `trigger` matching the sender whose `select` rules match the series |
| A second job appears with a few instances | Instances arrived after the quiet period; raise `RADFETCH_SCP_STABLE_S` |

### Cannot sign in

| Message or symptom | Cause |
|---|---|
| "wrong username or password, or the account is locked or disabled" | One of those three; the message does not say which on purpose. After ten wrong passwords an account is locked for 15 minutes. An administrator can reset the password, which also lifts the lock |
| Signed out unexpectedly | Sessions last `RADFETCH_SESSION_HOURS`; a password change or disabling the account signs out at once |
| No administrator can sign in | On the host: `docker compose exec api radfetch user passwd <name>` |
| After single sign-on: "you have not been given access to RadFetch" | The person is in no group named in `RADFETCH_OIDC_ROLE_MAP` and there is no default role. Add them to a mapped group, or create an account for them marked single sign-on |
| After single sign-on: "an account with your username already exists" | A password or service account has that name. Rename or delete it, or create the person under a different username claim |
| After single sign-on: "the identity provider's answer could not be verified" | The issuer URL, client ID or secret do not match the provider's registration, or the server clock is wrong. The API log has the detail |
| After single sign-on: "a second factor is required" | `RADFETCH_OIDC_REQUIRE_AMR` or `…_ACR` is set and the provider did not report it; see [Single sign-on](sso.md) |
| Single sign-on button missing | `RADFETCH_OIDC_ISSUER` and `RADFETCH_OIDC_CLIENT_ID` are not both set, or the API was not restarted |
| "that code is not right" | The authenticator's code is wrong or already used, or the phone's or server's clock is off. A recovery code also works |
| Lost the authenticator | Sign in with a recovery code, or ask an administrator to reset two-step sign-in |
| "set up two-step sign-in first" | `RADFETCH_REQUIRE_MFA` is on: open the Account page and set it up |
| "you lack the permission …" | The account's role or individual settings do not allow the action; see [Users and permissions](users.md) |
| A job someone else can see is "not found" | The account is limited to other flows |

### The disk fills up

Lower `RADFETCH_CACHE_TTL_HOURS` and `RADFETCH_RUN_TTL_HOURS`, and check
`RADFETCH_KEEP_FAILED_HOURS`. Above 80% usage the janitor evicts every cached
series no active job needs, regardless of age. Results in `directory`
destinations are never removed by RadFetch.

## Recovering from a crash or reboot

Start the stack again (`docker compose up -d`); the services restart by
themselves after a reboot. Nothing needs to be cleaned up: unfinished work is
picked up where it stopped. A flow container that was running when the host
went down is gone, so that job is run again.
