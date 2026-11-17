# Upgrade notes

How to upgrade is in [Administration](administration.md#upgrading): pull, then
`docker compose up -d --build`. Database changes are applied automatically. This
page lists the changes that need something from you, newest first.

## LDAP, SAML and simple sign-in

- Nothing changes until you configure them; see [Sign-in](sign-in.md).
- `RADFETCH_OIDC_ROLE_MAP` and `RADFETCH_OIDC_DEFAULT_ROLE` are now `RADFETCH_GROUP_ROLE_MAP` and `RADFETCH_EXTERNAL_DEFAULT_ROLE`, shared by all directory sign-ins. The old names still work.
- A single sign-on attempt is now tied to the browser that started it. A sign-in left half-way across a restart may need to be started again.

## Single sign-on and two-step sign-in

- Nothing changes until you configure them; see [Single sign-on and two-step sign-in](sign-in.md).
- Usernames may now contain `@`, so that e-mail style names from an identity provider work.
- With `RADFETCH_OIDC_ISSUER` set, `RADFETCH_PUBLIC_URL` becomes mandatory, and the interface is no longer open before the first account exists.

## Executors take single steps; GPU and CPU executors

- **Service names changed.** The Compose service `executor` is replaced by `executor-gpu` and `executor-cpu`. Update scripts, monitoring and any `docker-compose.override.yml` that name the old service. After upgrading, remove the old container:

    ```bash
    docker compose up -d --build --remove-orphans
    ```

- **Check that every kind of step has an executor.** Steps marked `gpu: true` are taken only by `executor-gpu`, all others only by `executor-cpu`. The Dashboard's Executors tile turns red if a step is waiting for a kind that is not running.
- **Steps running during the upgrade are started again.** Let running work finish first if a rerun is costly:

    ```bash
    docker compose stop gateway
    ```

    wait until the Dashboard shows no running jobs, then upgrade.
- **Job states read differently.** A job is `running` from its first step to its last, including while a step waits to be retried. It no longer returns to `ready` between attempts.
- **`RADFETCH_MAX_STAGED_JOBS` counts more.** It now covers every job waiting for an executor, also between steps. If batches fetch more slowly than before, the executors are the bottleneck; raise the setting only if you have disk to spare.

## Flows with several steps

- Existing flows work unchanged; a flow with a single `image` is one step named `main`.
- With more than one delivered step, a `directory` destination gets a subfolder per step.

## Structured results

- Nothing to do. Flows that write `/output/results.json` now have it stored; add the `results.view` permission to accounts below the `viewer` role that should see it.

## User accounts replace named tokens

- **Existing tokens keep working.** Each became a service account with the same name and role.
- **`radfetch token create` changed.** It now takes a username: create the account first with `radfetch user create`, then a token for it.
- **Create an administrator.** Until an account with a password exists, nobody can sign in to the web interface (API tokens still work):

    ```bash
    docker compose exec api radfetch user create <name> --role admin
    ```

- **Set `RADFETCH_PUBLIC_URL`** to the `https://` address of the interface, so that session cookies are marked secure.

## Standby replicas

- Nothing to do for a single-host setup. The gateway and push ingress now hold a database lock while active; a second copy started by mistake stands by instead of doubling the load on the PACS.

## Earlier

- **Renamed from the working name.** Environment variables are prefixed `RADFETCH_`, the command is `radfetch`, and container labels are `radfetch.*`.
