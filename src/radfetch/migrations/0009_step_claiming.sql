-- Executors now claim single steps instead of whole jobs, so steps of one job can run on
-- different executors (CPU and GPU) and independent steps can run at the same time.
create table job_steps (
    job_id            uuid not null references jobs(id) on delete cascade,
    step              text not null,
    state             text not null default 'pending',
    gpu               boolean not null default false,
    needs             text[] not null default '{}',   -- steps whose output this one reads
    run_id            uuid,                            -- the run that succeeded
    not_before        timestamptz not null default now(),
    lease_owner       text,
    lease_expires_at  timestamptz,
    primary key (job_id, step),
    constraint job_steps_state_check check (state in ('pending','running','succeeded','failed'))
);
create index job_steps_claim_idx on job_steps (state, gpu);

-- Jobs that were mid-run during the upgrade start their steps again under the new scheme.
update runs set state = 'lost', finished_at = now(), error = 'interrupted by an upgrade' where state = 'running';
update jobs set state = 'ready', lease_owner = null, lease_expires_at = null where state = 'running';
