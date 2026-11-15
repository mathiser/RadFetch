# Connecting a PACS

## Choose a retrieve mode

Set `RADFETCH_RETRIEVE_MODE` in `.env`. Queries and retrieval use the same mode.

| Mode | Uses | Needs on the PACS side | Choose it when |
|---|---|---|---|
| `dicomweb` | QIDO-RS, WADO-RS | A DICOMweb endpoint, optionally a token | The PACS offers DICOMweb |
| `get` | C-FIND, C-GET | RadFetch's AE title allowed to query and retrieve | The PACS supports C-GET |
| `move` | C-FIND, C-MOVE | The AE title allowed to query, **and** registered as a destination at this host, port 11113 | The PACS only supports C-MOVE |

With `get`, everything happens on connections RadFetch opens. With `move`, the
PACS connects back to RadFetch's storage SCP, so the PACS must be able to reach
this host on port 11113 through any firewall.

## Settings for DIMSE (`get` and `move`)

```
RADFETCH_PACS_HOST=pacs.hospital.example
RADFETCH_PACS_PORT=104
RADFETCH_PACS_AE_TITLE=PACS
RADFETCH_LOCAL_AE_TITLE=RADFETCH
RADFETCH_RETRIEVE_MODE=get
```

In `get` mode RadFetch asks for a fixed list of storage classes (CT, MR, PET, NM,
CR, DX, mammography, US, XA, secondary capture, RT objects, registrations,
segmentations, structured reports). A series of another type is not delivered
and the retrieval fails as incomplete. To add a type, extend
`GET_STORAGE_CLASSES` in `src/radfetch/gateway/dimse.py`. `move` mode accepts
every storage class.

## Settings for DICOMweb

```
RADFETCH_RETRIEVE_MODE=dicomweb
RADFETCH_DICOMWEB_URL=https://pacs.hospital.example/dicom-web
RADFETCH_DICOMWEB_TOKEN=...
```

The token is sent as `Authorization: Bearer`. The PACS host and AE title settings
are not used in this mode.

## TLS

Mount the certificate files into the `gateway`, `scp` and `sender` services and set:

```
RADFETCH_TLS_CA_FILE=/certs/ca.pem
RADFETCH_TLS_CERT_FILE=/certs/radfetch.pem
RADFETCH_TLS_KEY_FILE=/certs/radfetch.key
```

| Setting | Effect |
|---|---|
| `RADFETCH_PACS_TLS=true` | TLS for C-FIND, C-GET and C-MOVE towards the PACS, and on the C-MOVE storage SCP |
| `RADFETCH_SCP_TLS=true` | TLS on the push ingress. With a CA file set, senders must present a certificate signed by it |
| `tls: true` on a `dicom` destination | TLS when delivering results to that destination |

RadFetch verifies the peer's certificate against the CA file and checks that it
is valid for the host name in `RADFETCH_PACS_HOST`. For DICOMweb, TLS comes from
using an `https://` URL.

## Limits to agree with the PACS team

| Setting | Meaning | Default |
|---|---|---|
| `RADFETCH_RATE_INSTANCES_PER_MIN` | Average instances per minute for batch work | 600 |
| `RADFETCH_BATCH_WINDOWS` | When batch work may touch the PACS | always |
| `RADFETCH_MIN_INTERVAL_S` | Pause between any two PACS operations | 0.5 |
| `RADFETCH_MAX_STAGED_JOBS` | How many batch studies are fetched ahead of the executor | 4 |

Batch windows look like `mon-fri 18:00-06:00; sat-sun 00:00-24:00`. Days are
optional (`22:00-04:00` means every day) and a window may cross midnight. Times
are local time, so set `TZ` in `.env`.

The rate budget is charged after each series with the number of instances
actually received, so a large series is followed by a proportionally longer pause.

## Try against Orthanc first

Orthanc is a real, open-source PACS. The Compose file can start one:

```bash
docker compose --profile orthanc up -d
```

Set in `.env`:

```
RADFETCH_PACS_HOST=orthanc
RADFETCH_PACS_PORT=4242
RADFETCH_PACS_AE_TITLE=ORTHANC
```

Restart the gateway (`docker compose up -d gateway`), upload some studies at
http://localhost:8042, and submit their accession numbers. This Orthanc has no
login and is for development only.

## Check the connection

Submit one job for an accession number you know exists and watch it reach
`retrieving`. The UI header shows **PACS reachable** until three PACS operations
in a row have failed, then **PACS breaker open**, so a wrong setting shows up
there only after a few attempts. The gateway's log has the detail:

```bash
docker compose logs --tail 50 gateway
```

See [Troubleshooting](troubleshooting.md) for what the common failures mean.
