-- Initial schema. Written idempotently so databases created before migrations existed adopt it cleanly.

create table if not exists batches (
    id            uuid primary key,
    name          text not null,
    flow          text not null,
    paused        boolean not null default false,
    requested_by  text,
    created_at    timestamptz not null default now()
);

-- One job = run one flow on one accession number.
-- States: queued -> retrieving -> ready -> running -> sending -> done
--         any non-terminal state -> failed | cancelled
create table if not exists jobs (
    id                  uuid primary key,
    batch_id            uuid references batches(id),
    accession_number    text not null,
    flow                text not null,
    flow_version        text,
    priority            int  not null default 0,
    state               text not null default 'queued',
    study_instance_uid  text,
    attempts            int  not null default 0,
    not_before          timestamptz not null default now(),
    lease_owner         text,
    lease_expires_at    timestamptz,
    error               text,
    requested_by        text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint jobs_state_check check (state in
        ('queued','retrieving','ready','running','sending','done','failed','cancelled'))
);
create index if not exists jobs_claim_idx on jobs (state, priority desc, created_at);
create index if not exists jobs_batch_idx on jobs (batch_id);

-- One retrieval = one series in the local cache, shared by any number of jobs.
-- States: pending -> staged ; pending -> failed ; staged -> evicted
create table if not exists retrievals (
    series_instance_uid text primary key,
    study_instance_uid  text not null,
    state               text not null default 'pending',
    priority            int  not null default 0,
    expected_instances  int,
    received_instances  int,
    bytes               bigint,
    attempts            int  not null default 0,
    not_before          timestamptz not null default now(),
    lease_owner         text,
    lease_expires_at    timestamptz,
    error               text,
    created_at          timestamptz not null default now(),
    staged_at           timestamptz,
    last_used_at        timestamptz,
    constraint retrievals_state_check check (state in
        ('pending','staged','failed','evicted'))
);
create index if not exists retrievals_claim_idx on retrievals (state, priority desc, created_at);

create table if not exists job_series (
    job_id              uuid not null references jobs(id) on delete cascade,
    series_instance_uid text not null references retrievals(series_instance_uid),
    primary key (job_id, series_instance_uid)
);
create index if not exists job_series_series_idx on job_series (series_instance_uid);

-- One run = one container execution attempt of a job.
create table if not exists runs (
    id           uuid primary key,
    job_id       uuid not null references jobs(id) on delete cascade,
    attempt      int  not null,
    state        text not null default 'running',
    image        text not null,
    exit_code    int,
    error        text,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    constraint runs_state_check check (state in
        ('running','succeeded','failed','cancelled','lost'))
);
create index if not exists runs_job_idx on runs (job_id, attempt);

-- One delivery = send the output of a successful run to one destination.
create table if not exists deliveries (
    id                uuid primary key,
    job_id            uuid not null references jobs(id) on delete cascade,
    run_id            uuid not null references runs(id) on delete cascade,
    destination       jsonb not null,
    state             text not null default 'pending',
    attempts          int  not null default 0,
    not_before        timestamptz not null default now(),
    lease_owner       text,
    lease_expires_at  timestamptz,
    error             text,
    created_at        timestamptz not null default now(),
    constraint deliveries_state_check check (state in ('pending','done','failed'))
);
create index if not exists deliveries_claim_idx on deliveries (state, not_before);

-- Append-only audit trail.
create table if not exists events (
    id      bigserial primary key,
    at      timestamptz not null default now(),
    job_id  uuid,
    kind    text not null,
    detail  jsonb not null default '{}'::jsonb
);
create index if not exists events_job_idx on events (job_id, id);

-- How a job entered the system: 'pull' (submitted by accession number) or 'push' (received by the SCP).
alter table jobs add column if not exists source text not null default 'pull';

-- Studies currently being received by the storage SCP, waiting to go quiet.
create table if not exists incoming_studies (
    study_instance_uid  text primary key,
    accession_number    text,
    calling_ae_title    text,
    first_received_at   timestamptz not null default now(),
    last_received_at    timestamptz not null default now()
);

-- Small key/value board for worker heartbeats, read by the API's /healthz and /metrics.
create table if not exists status (
    key         text primary key,
    value       jsonb not null,
    updated_at  timestamptz not null default now()
);
