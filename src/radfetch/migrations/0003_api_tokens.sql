-- Named API tokens. Only a SHA-256 of each token is stored.
create table api_tokens (
    id          uuid primary key,
    name        text not null unique,
    role        text not null,
    token_hash  text not null unique,
    created_at  timestamptz not null default now(),
    revoked_at  timestamptz,
    constraint api_tokens_role_check check (role in ('viewer','operator'))
);
