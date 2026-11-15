# Tutorial: your first jobs

This walks through RadFetch on one machine with a built-in fake PACS, so nothing
clinical is involved. You need Docker with the Compose plugin and about ten minutes.

The fake PACS holds 20 synthetic studies with accession numbers `ACC0001` to
`ACC0020`. Each has a 12-slice CT thorax series, a 2-slice scout and a 1-image
dose report.

## 1. Start the stack

```bash
git clone https://github.com/mathiser/RadFetch.git
```

```bash
cd RadFetch
```

```bash
cp .env.example .env
```

Edit `.env` and change two lines:

- `RADFETCH_HOST_DATA_DIR`: the absolute path of the `data` folder inside this checkout, for example `/home/you/RadFetch/data`.
- `RADFETCH_BATCH_WINDOWS=`: leave the value empty, so batches run at any time of day.

Then start everything, including the fake PACS:

```bash
docker compose --profile dev up -d --build
```

Open http://localhost:8000. The header should show **gateway up**, **PACS reachable** and **batch window open**.

## 2. Run a one-off job

The repository ships one flow, `example` (`flows/example.yaml`). It selects CT
series whose description contains "thorax", runs a small container that counts
the files it was given, and writes the result to `data/results/`.

In the UI, under **One-off job**, enter `ACC0001`, keep the flow `example`, and
press **Run now**.

Watch the job in the **Jobs** table move through `queued`, `retrieving`, `ready`,
`running`, `sending` and `done`. This takes a few seconds. Click the row to see
its history: which series were selected, when the run started, and the exact
image it used.

Look at the result:

```bash
sudo cat data/results/*/instance_count.txt
```

It prints `12`: the container saw only the thorax series, not the scout or the
dose report. Next to it is `radfetch.json`, which records what produced the result.

## 3. Run a batch

Under **Batch**, enter a name, keep the flow, paste these accession numbers and
press **Queue batch**:

```
ACC0002
ACC0003
ACC0004
ACC0005
NOT-A-REAL-ONE
```

The batch appears under **Batches** with its progress. Four jobs finish; the fifth
fails with "no study found for accession number NOT-A-REAL-ONE". Things to try
while it runs or afterwards:

- **Pause** and **Resume** stop and restart the batch. A container that is already running finishes.
- **CSV** downloads one row per job with its state and error.
- **Retry failed** puts the failed job back in the queue (it will fail again, since the study does not exist).

## 4. See the PACS protection at work

Queue a second batch with `ACC0006` to `ACC0015`. While it runs, look at the job
counts in the header: the number of `ready` jobs never goes above 4. The gateway
only fetches a few studies ahead of the executor instead of pulling all ten at
once. That limit is `RADFETCH_MAX_STAGED_JOBS`.

Now submit a one-off job for `ACC0020` while the batch is still running. It is
fetched and run ahead of the remaining batch jobs.

## 5. Receive a pushed study

The `example` flow has a `trigger` that matches senders whose AE title starts with
`CT_`. Send a synthetic study to the push ingress:

```bash
docker compose exec scp radfetch fake-push --calling-aet CT_ROOM1 --accession-number PUSHED01
```

After 30 quiet seconds (`RADFETCH_SCP_STABLE_S`) a job with source `push` appears
and runs. Send another with `--calling-aet MR_ROOM1`: nothing starts, because no
flow's trigger matches that sender.

## 6. Turn on authentication

Until a token exists the API is open. Create one:

```bash
docker compose exec api radfetch token create me --role operator
```

Copy the token it prints; it is shown only once. Reload the UI: it now asks for
the token, and your name appears in the header. Jobs you submit are recorded
under that name.

## 7. Clean up

```bash
docker compose --profile dev down -v
```

```bash
sudo rm -rf data
```

## Where next

- [Writing flows](flows.md) to run your own image.
- [Connecting a PACS](pacs.md) and [Installation](installation.md) for a real deployment.
