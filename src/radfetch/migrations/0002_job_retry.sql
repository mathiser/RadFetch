-- Manual retries: run attempts are counted from the last retry, not from the job's creation.
alter table jobs add column retried_at timestamptz;
