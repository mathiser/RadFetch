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

Open http://localhost:8000. The **Dashboard** opens; its **PACS** tile should say **reachable**, with "batch window open" beneath it.

## 2. Run a one-off job

The repository ships one flow, `example` (`flows/example.yaml`). It selects CT
series whose description contains "thorax", runs a small container that counts
the files it was given, and writes the result to `data/results/`.

On the **Submit** page, under **One-off job**, enter `ACC0001`, keep the flow
`example`, and press **Run now**.

On the **Jobs** page, watch the job move through `queued`, `retrieving`, `ready`,
`running`, `sending` and `done`. This takes a few seconds. Click the row to see
its history: which series were selected, when the run started, and the exact
image it used.

Look at the result:

```bash
sudo cat data/results/*/instance_count.txt
```

It prints `12`: the container saw only the thorax series, not the scout or the
dose report. Next to it is `radfetch.json`, which records what produced the result.

The example flow also reports a small structured result (`results.json`). Open the
**Results** page to see it as a table, one row per job; with real flows this is
where measurements and scores end up, ready to search and export.

## 3. Run a batch

On the **Submit** page, under **Batch**, enter a name, keep the flow, paste these
accession numbers and press **Queue batch**:

```
ACC0002
ACC0003
ACC0004
ACC0005
NOT-A-REAL-ONE
```

The **Batches** page opens and shows the batch with its progress. Four jobs finish; the fifth
fails with "no study found for accession number NOT-A-REAL-ONE". Things to try
while it runs or afterwards:

- **Pause** and **Resume** stop and restart the batch. A container that is already running finishes.
- **CSV** downloads one row per job with its state and error.
- **Retry failed** puts the failed job back in the queue (it will fail again, since the study does not exist).

## 4. See the PACS protection at work

Queue a second batch with `ACC0006` to `ACC0015`. While it runs, look at the
**Active jobs** tile on the Dashboard: the number of `ready` jobs stays small (the limit is 4 waiting for an executor). The gateway
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

## 6. Create accounts

Until an account exists, anyone who can reach the interface can use it. Create
an administrator:

```bash
docker compose exec api radfetch user create me --role admin
```

Copy the password it prints; it is shown only once. Reload the page and sign in.
The **Users** page now lets you add people. Try it:

1. Create a user `wall` with the role `monitor` and sign in as that user in a private window. The dashboard and job states are there, but accession numbers are hidden and nothing can be submitted.
2. As the administrator, create a user with the role `submitter`, select it, set "Submit batches" to "never allow", and save. That user can run one-off jobs but not batches.

[Users and permissions](users.md) explains the roles and what can be adjusted.

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
