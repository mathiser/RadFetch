-- Structured results a flow reports (its results.json), one document per job, queryable as JSON.
create table results (
    job_id      uuid primary key references jobs(id) on delete cascade,
    run_id      uuid not null references runs(id) on delete cascade,
    flow        text not null,
    flow_version text,
    data        jsonb not null,
    created_at  timestamptz not null default now()
);
create index results_flow_idx on results (flow, created_at desc);
-- Supports "documents containing {...}" queries without knowing the documents' shape in advance.
create index results_data_idx on results using gin (data jsonb_path_ops);
