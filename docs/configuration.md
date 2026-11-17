# Configuration reference

Every setting is an environment variable with the prefix `RADFETCH_`, normally
set in `.env`. All services read the same file. After changing a setting,
restart the services that use it (`docker compose up -d`).

Flows are configured separately, in `flows/*.yaml`: see [Writing flows](flows.md).

## Storage

| Variable | Default | Meaning |
|---|---|---|
| `RADFETCH_DATABASE_URL` | set in `docker-compose.yml` | Postgres connection |
| `RADFETCH_DATA_DIR` | `/data` | Cache, run files and incoming studies, inside the containers |
| `RADFETCH_HOST_DATA_DIR` | none, **required** | The same folder as the Docker host sees it. The executor starts flow containers through the host's Docker daemon, which needs host paths |
| `RADFETCH_FLOWS_DIR` | `/flows` | Folder with the flow files |

## PACS connection

| Variable | Default | Meaning |
|---|---|---|
| `RADFETCH_RETRIEVE_MODE` | `get` | `get`, `move` or `dicomweb`. See [Connecting a PACS](pacs.md) |
| `RADFETCH_PACS_HOST`, `RADFETCH_PACS_PORT` | `localhost`, `11112` | PACS address for `get` and `move` |
| `RADFETCH_PACS_AE_TITLE` | `PACS` | The PACS's AE title |
| `RADFETCH_LOCAL_AE_TITLE` | `RADFETCH` | RadFetch's AE title |
| `RADFETCH_MOVE_SCP_PORT` | `11113` | Storage SCP for `move` mode |
| `RADFETCH_DICOMWEB_URL` | empty | Base URL for `dicomweb` mode |
| `RADFETCH_DICOMWEB_TOKEN` | none | Bearer token for `dicomweb` mode |
| `RADFETCH_DICOMWEB_TIMEOUT_S` | `120` | HTTP timeout, also used for STOW-RS delivery |
| `RADFETCH_ACSE_TIMEOUT_S` | `30` | Time allowed to set up a DICOM association |
| `RADFETCH_DIMSE_TIMEOUT_S`, `RADFETCH_NETWORK_TIMEOUT_S` | `120` | Time allowed between DICOM messages |

## TLS

| Variable | Default | Meaning |
|---|---|---|
| `RADFETCH_TLS_CA_FILE` | none | CA bundle used to verify peers (system store if unset) |
| `RADFETCH_TLS_CERT_FILE`, `RADFETCH_TLS_KEY_FILE` | none | RadFetch's own certificate and key (PEM) |
| `RADFETCH_PACS_TLS` | `false` | TLS towards the PACS and on the C-MOVE storage SCP |
| `RADFETCH_SCP_TLS` | `false` | TLS on the push ingress |

## PACS protection

| Variable | Default | Meaning |
|---|---|---|
| `RADFETCH_RATE_INSTANCES_PER_MIN` | `600` | Average pull rate for batch work. `0` turns the limit off |
| `RADFETCH_MIN_INTERVAL_S` | `0.5` | Pause between any two PACS operations |
| `RADFETCH_BATCH_WINDOWS` | empty (always) | When batch work may touch the PACS, e.g. `mon-fri 18:00-06:00; sat-sun 00:00-24:00` |
| `RADFETCH_MAX_RESOLVED_AHEAD` | `10` | Batch jobs are looked up in the PACS only while fewer than this many wait for data |
| `RADFETCH_MAX_STAGED_JOBS` | `4` | Batch series are fetched only while fewer than this many jobs have their data here and wait for an executor, whether not yet started or between steps |
| `RADFETCH_DISK_HIGH_WATERMARK` | `0.80` | Batch retrieval stops above this disk usage; the janitor also starts evicting |
| `RADFETCH_DISK_HARD_LIMIT` | `0.95` | All retrieval stops above this disk usage |
| `RADFETCH_BREAKER_FAILURE_THRESHOLD` | `3` | Consecutive PACS failures before all PACS traffic pauses |
| `RADFETCH_BREAKER_BASE_COOLDOWN_S` | `60` | First pause; doubles while failures continue |
| `RADFETCH_BREAKER_MAX_COOLDOWN_S` | `1800` | Longest pause |
| `RADFETCH_RESOLVE_MAX_ATTEMPTS` | `5` | PACS lookups per job before it fails |
| `RADFETCH_RETRIEVE_MAX_ATTEMPTS` | `5` | Fetch attempts per series before its jobs fail |
| `RADFETCH_RETRY_BASE_DELAY_S`, `RADFETCH_RETRY_MAX_DELAY_S` | `60`, `3600` | Delay before a retry; doubles each time up to the maximum |

## Execution

| Variable | Default | Meaning |
|---|---|---|
| `RADFETCH_EXECUTOR_KIND` | `any` | Which steps this executor takes: `gpu`, `cpu` or `any`. The Compose file sets it per service |
| `RADFETCH_GPU_DEVICE_IDS` | empty (all) | GPUs this executor hands to `gpu` flows, e.g. `0` or `0,1` |
| `RADFETCH_CONTAINER_NETWORK` | `none` | Docker network mode for flow containers |
| `RADFETCH_DEBUG_FAKE_GPU` | `false` | Debug only: run `gpu` flows without a GPU. Never in production |
| `RADFETCH_RESULTS_MAX_BYTES` | `1000000` | Largest `results.json` stored with a job |
| `RADFETCH_DELIVER_MAX_ATTEMPTS` | `8` | Delivery attempts per destination before the job fails |

## Push ingress

| Variable | Default | Meaning |
|---|---|---|
| `RADFETCH_SCP_PORT` | `11114` | Port of the storage SCP |
| `RADFETCH_SCP_STABLE_S` | `30` | A pushed study is processed after this many quiet seconds |
| `RADFETCH_SCP_ALLOWED_AE_TITLES` | empty (any) | Comma-separated AE titles allowed to push |
| `RADFETCH_PUSH_PRIORITY` | `50` | Priority of pushed jobs (one-off is 100, batch is 0) |

## API, UI and notifications

| Variable | Default | Meaning |
|---|---|---|
| `RADFETCH_API_TOKEN` | none | One shared token with operator rights, outside the accounts |
| `RADFETCH_SESSION_HOURS` | `12` | How long a sign-in in the web interface lasts |
| `RADFETCH_SIMPLE_PASSWORD` | none | One shared password; signs in as the built-in `admin`. For small set-ups |
| `RADFETCH_REQUIRE_MFA` | `false` | Password sign-ins must set up two-step sign-in before they can do anything |
| `RADFETCH_PASSWORD_LOGIN` | `true` | Whether the RadFetch password form is offered next to single sign-on |
| `RADFETCH_GROUP_ROLE_MAP` | empty | JSON object mapping directory groups to roles, for LDAP, OpenID Connect and SAML |
| `RADFETCH_EXTERNAL_DEFAULT_ROLE` | empty | Role for directory people in no mapped group; empty refuses them |
| `RADFETCH_LDAP_URL` | empty (off) | `ldaps://host:636`, or `ldap://` with StartTLS. Turns LDAP sign-in on |
| `RADFETCH_LDAP_START_TLS`, `RADFETCH_LDAP_CA_FILE`, `RADFETCH_LDAP_ALLOW_INSECURE` | `false`, none, `false` | Encryption; see [Sign-in](sign-in.md#encryption) |
| `RADFETCH_LDAP_BIND_DN`, `RADFETCH_LDAP_BIND_PASSWORD` | empty | Service account that looks people up |
| `RADFETCH_LDAP_USER_BASE`, `RADFETCH_LDAP_USER_FILTER` | empty, `(sAMAccountName={username})` | Where and how people are found |
| `RADFETCH_LDAP_USER_DN_TEMPLATE` | empty | Without a service account: whom to bind as, e.g. `{username}@hospital.example` |
| `RADFETCH_LDAP_USERNAME_ATTRIBUTE`, `RADFETCH_LDAP_GROUPS_ATTRIBUTE` | `sAMAccountName`, `memberOf` | Attributes read from the person |
| `RADFETCH_LDAP_GROUP_BASE`, `RADFETCH_LDAP_GROUP_FILTER` | empty, `(member={user_dn})` | Optional group search (OpenLDAP, nested AD groups) |
| `RADFETCH_LDAP_TIMEOUT_S` | `10` | Seconds to wait for the directory |
| `RADFETCH_OIDC_ISSUER` | empty (off) | Issuer URL of the OpenID Connect provider; turns it on |
| `RADFETCH_OIDC_CLIENT_ID`, `RADFETCH_OIDC_CLIENT_SECRET` | | RadFetch's registration at the provider |
| `RADFETCH_OIDC_REQUIRE_AMR`, `RADFETCH_OIDC_REQUIRE_ACR`, `RADFETCH_OIDC_ACR_VALUES` | empty | Insist that the provider used a second factor |
| `RADFETCH_OIDC_USERNAME_CLAIM`, `RADFETCH_OIDC_GROUPS_CLAIM`, `RADFETCH_OIDC_SCOPES`, `RADFETCH_OIDC_BUTTON_LABEL` | see [Sign-in](sign-in.md#openid-connect) | Adjustments for the provider |
| `RADFETCH_SAML_IDP_METADATA` | empty (off) | URL or file of the SAML provider's metadata; turns SAML on |
| `RADFETCH_SAML_USERNAME_ATTRIBUTE`, `RADFETCH_SAML_GROUPS_ATTRIBUTE` | NameID, `groups` | Where the username and groups come from |
| `RADFETCH_SAML_REQUIRE_AUTHN_CONTEXT` | empty | Insist on an authentication context (a second factor) |
| `RADFETCH_SAML_ENTITY_ID`, `RADFETCH_SAML_BUTTON_LABEL` | see [Sign-in](sign-in.md#saml-20) | Adjustments |
| `RADFETCH_NOTIFY_WEBHOOK_URL` | empty (off) | Where to POST notifications |
| `RADFETCH_NOTIFY_INCLUDE_IDENTIFIERS` | `false` | Include accession numbers and error text in notifications |
| `RADFETCH_PUBLIC_URL` | empty | Address of the web interface. Added as a link in notifications; when it starts with `https://`, session cookies are marked secure |
| `RADFETCH_NOTIFY_MAX_ATTEMPTS` | `6` | Attempts per notification |

## Retention

| Variable | Default | Meaning |
|---|---|---|
| `RADFETCH_CACHE_TTL_HOURS` | `72` | A cached series no job needs is evicted after this long unused |
| `RADFETCH_RUN_TTL_HOURS` | `168` | Run files (input links, output, log) are removed this long after the job finished |
| `RADFETCH_KEEP_FAILED_HOURS` | `0` (off) | Keep the cached input and run files of failed jobs at least this long |
| `RADFETCH_JANITOR_INTERVAL_S` | `300` | How often the janitor runs |

## Rarely changed

| Variable | Default | Meaning |
|---|---|---|
| `RADFETCH_LEASE_S` | `120` | How long a worker's claim lasts without a heartbeat |
| `RADFETCH_RETRIEVE_LEASE_S` | `3600` | Claim on one series retrieval; must outlast the largest series |
| `RADFETCH_IDLE_SLEEP_S`, `RADFETCH_EXECUTOR_POLL_S` | `2` | Polling intervals |
| `TZ` | system | Timezone for batch windows (not prefixed) |
