-- User accounts with roles and per-user adjustments. API tokens now belong to a user.
create table users (
    id             uuid primary key,
    username       text not null unique,
    password_hash  text,                       -- null: no password login (service account, tokens only)
    role           text not null,
    grants         text[] not null default '{}',   -- permissions added on top of the role
    denies         text[] not null default '{}',   -- permissions removed from the role
    allowed_flows  text[],                         -- null: every flow
    disabled       boolean not null default false,
    failed_logins  int not null default 0,
    locked_until   timestamptz,
    created_at     timestamptz not null default now(),
    last_login_at  timestamptz,
    constraint users_role_check check (role in ('monitor','viewer','submitter','operator','admin'))
);

create table sessions (
    token_hash  text primary key,
    user_id     uuid not null references users(id) on delete cascade,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null
);

-- Tokens created before accounts existed become service accounts with the same name and role.
insert into users (id, username, role)
    select gen_random_uuid(), name, role from api_tokens;
alter table api_tokens add column user_id uuid references users(id) on delete cascade;
update api_tokens t set user_id = u.id from users u where u.username = t.name;
alter table api_tokens alter column user_id set not null;
alter table api_tokens drop column role;
