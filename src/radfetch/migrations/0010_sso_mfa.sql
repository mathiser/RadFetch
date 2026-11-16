-- Single sign-on (OpenID Connect) and two-step sign-in (TOTP) for password accounts.
alter table users add column sso boolean not null default false;      -- signs in through the identity provider
alter table users add column oidc_subject text unique;                -- issuer|subject, set at first SSO sign-in
alter table users add column totp_secret text;                        -- set when enrollment starts
alter table users add column totp_enabled boolean not null default false;
alter table users add column totp_last_step bigint;                   -- last accepted time step: a code works once
alter table users add column recovery_codes text[] not null default '{}';  -- SHA-256 of unused one-time codes
alter table sessions add column sso boolean not null default false;

-- A sign-in in progress at the identity provider. Rows are single-use and short-lived.
create table oidc_logins (
    state       text primary key,
    nonce       text not null,
    verifier    text not null,
    created_at  timestamptz not null default now()
);
