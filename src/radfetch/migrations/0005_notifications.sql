-- Outbox of notifications, written in the same transaction as the change that causes them
-- and delivered by the sender.
create table notifications (
    id                bigserial primary key,
    kind              text not null,       -- job_failed | batch_finished | pacs_unreachable
    ref               uuid,                -- job or batch the notification is about
    detail            jsonb not null default '{}'::jsonb,
    state             text not null default 'pending',
    attempts          int  not null default 0,
    not_before        timestamptz not null default now(),
    lease_owner       text,
    lease_expires_at  timestamptz,
    error             text,
    created_at        timestamptz not null default now(),
    constraint notifications_state_check check (state in ('pending','sent','failed','skipped'))
);
create index notifications_claim_idx on notifications (state, not_before);

-- A trigger rather than application code, so no path that finishes a job can forget to notify.
create function notify_job_finished() returns trigger language plpgsql as $$
begin
    if new.batch_id is null then
        if new.state = 'failed' then
            insert into notifications (kind, ref) values ('job_failed', new.id);
        end if;
    else
        -- Serialise on the batch row: of two jobs finishing at once, the later one sees the
        -- earlier one committed and reports the batch as finished exactly once.
        perform 1 from batches where id = new.batch_id for update;
        if not exists (select 1 from jobs where batch_id = new.batch_id
                       and state not in ('done','failed','cancelled')) then
            insert into notifications (kind, ref) values ('batch_finished', new.batch_id);
        end if;
    end if;
    return null;
end $$;

create trigger jobs_notify_finished
    after update of state on jobs
    for each row
    when (new.state in ('done','failed','cancelled') and old.state not in ('done','failed','cancelled'))
    execute function notify_job_finished();
