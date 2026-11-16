# Single sign-on and two-step sign-in

RadFetch can let people sign in through your organisation's identity provider
(single sign-on), and can require a second factor. The two are independent:

| Who | Signs in with | Second factor |
|---|---|---|
| People in your directory | Single sign-on (OpenID Connect) | Done by the identity provider; RadFetch can insist on it |
| RadFetch password accounts | Username and password | An authenticator app, set up in RadFetch |
| Programs | An API token | None: a token is the credential |

## Single sign-on

RadFetch speaks OpenID Connect, which Microsoft Entra ID, ADFS, Keycloak, Okta,
Google Workspace and most other identity providers offer.

### 1. Register RadFetch at the identity provider

Create an application (often called a "client" or "app registration") with:

| Setting | Value |
|---|---|
| Type | Web application, authorization code flow (a "confidential" client with a secret) |
| Redirect URI | `https://<your RadFetch address>/auth/oidc/callback` |
| Post-logout redirect URI | `https://<your RadFetch address>/` |
| Scopes | `openid profile email` |
| Group claim | Add the person's groups to the ID token, in a claim named `groups` |

Note the **issuer URL**, the **client ID** and the **client secret**.

### 2. Configure RadFetch

In `.env`:

```
RADFETCH_PUBLIC_URL=https://radfetch.hospital.example
RADFETCH_OIDC_ISSUER=https://login.hospital.example/realms/hospital
RADFETCH_OIDC_CLIENT_ID=radfetch
RADFETCH_OIDC_CLIENT_SECRET=...
RADFETCH_OIDC_ROLE_MAP={"radfetch-admins": "admin", "ai-team": "operator", "radiology": "viewer"}
```

Then restart the API (`docker compose up -d api`). The sign-in page now shows a
single sign-on button.

`RADFETCH_PUBLIC_URL` must be the address people use, and must match the
redirect URI registered at the provider. RadFetch refuses to start with single
sign-on configured and no public URL.

### 3. Decide who gets in, and with what role

`RADFETCH_OIDC_ROLE_MAP` maps directory groups to RadFetch roles.

- **The role comes from the groups at every sign-in.** Moving someone between groups in the directory changes their RadFetch role the next time they sign in. With several matching groups the highest role wins.
- **People in no mapped group are refused**, with a message saying they have not been given access. To admit everyone the provider knows, set `RADFETCH_OIDC_DEFAULT_ROLE` (for example `monitor`).
- **Individual exceptions.** An administrator can create an account for one person beforehand, marked "signs in with single sign-on" (or `radfetch user create <name> --role submitter --sso`). The name must be exactly the username the provider sends. That person is admitted with that role even without a mapped group.
- **Individual permissions and flow limits stay in RadFetch.** Grants, denies and flow restrictions set on the Users page are kept when the role changes.
- **Without a role map**, new people get the default role (or are refused if there is none), and roles are managed on the Users page.

How accounts are matched:

- At first sign-in an account is created, named after the provider's username claim.
- From then on the person is recognised by the provider's permanent identifier, so a later change of username at the provider does not create a second account.
- **A password account or service account is never taken over.** If the provider sends a username that already belongs to one, sign-in is refused with a message to ask an administrator.

### What to keep in mind

- **Keep one password administrator.** If the identity provider is down, nobody can use single sign-on. A local administrator account with a strong password and two-step sign-in is the way back in. `RADFETCH_PASSWORD_LOGIN=false` hides password sign-in altogether; only do that if you accept managing RadFetch from the command line during an outage.
- **The only administrator is never demoted by the directory.** If a group change would leave RadFetch without an administrator, that person keeps the role and the audit log records it.
- **Signing out** ends the RadFetch session and then the provider's, if the provider supports it.
- **Disabling** an account in RadFetch stops single sign-on for it too. Removing someone from the directory stops new sign-ins; a session already open lasts until it expires (`RADFETCH_SESSION_HOURS`), so disable the account in RadFetch as well if access must end at once.
- **With single sign-on configured, the interface is never open**, also before the first person has signed in.

### Provider notes

| Provider | Issuer URL | Notes |
|---|---|---|
| Keycloak | `https://<host>/realms/<realm>` | Add a "Group Membership" mapper to the client with claim name `groups`, "full group path" off |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant id>/v2.0` | Add a groups claim under Token configuration. Entra sends group **object IDs**, not names, so use the IDs as keys in the role map |
| ADFS | `https://<host>/adfs` | Issue group membership as a claim named `groups` |

Other settings, if your provider differs:

| Variable | Default | For |
|---|---|---|
| `RADFETCH_OIDC_USERNAME_CLAIM` | `preferred_username` | Which claim becomes the username (`email` and `upn` are common alternatives) |
| `RADFETCH_OIDC_GROUPS_CLAIM` | `groups` | Which claim lists the groups (`roles` at some providers) |
| `RADFETCH_OIDC_SCOPES` | `openid profile email` | Scopes requested |
| `RADFETCH_OIDC_BUTTON_LABEL` | `Sign in with single sign-on` | The button's text, e.g. "Sign in with your hospital account" |

## Requiring a second factor

### For single sign-on

The provider performs the second factor. The dependable place to require it is
at the provider: a policy that demands multi-factor authentication for the
RadFetch application.

RadFetch can additionally refuse any sign-in for which the provider does not
state that a second factor was used:

```
RADFETCH_OIDC_REQUIRE_AMR=mfa
```

This checks the token's `amr` ("authentication methods") claim for that value.
Providers differ in whether and how they send it, so try it with a test account
first: if sign-in is refused with "a second factor is required" even though one
was used, your provider does not send the claim this way. Some use the `acr`
claim instead; `RADFETCH_OIDC_REQUIRE_ACR` requires an exact value there, and
`RADFETCH_OIDC_ACR_VALUES` asks the provider for that level when sending people
to it.

### For password accounts

Each person can turn on two-step sign-in on the **Account** page: scan a QR code
with an authenticator app (Microsoft Authenticator, Google Authenticator,
1Password and others), enter the code it shows, and save the ten recovery codes.
From then on signing in needs the password and a code.

To make it mandatory for every password account:

```
RADFETCH_REQUIRE_MFA=true
```

A person without it can then sign in but do nothing except set it up, and it
cannot be turned off again.

Things to know:

- **Lost phone.** A recovery code works once in place of an app code. After signing in, set up the app again on the Account page. An administrator can also remove a person's two-step sign-in from the Users page (or `radfetch user reset-mfa <name>`); the person then sets it up afresh.
- **Wrong codes count** towards locking the account, like wrong passwords.
- **A code works once.** A code that has been used is refused if tried again.
- **Clocks matter.** Codes depend on the time; the server accepts one 30-second step either way. A phone or server with a wrong clock produces "that code is not right".
- **API tokens are not affected.** A token is for programs and has no second factor. Give tokens the lowest role that works and revoke them when they are no longer needed.
- **The setting covers password sign-ins only.** People signing in through single sign-on are not asked by RadFetch for a second factor on top of the provider's.
- The authenticator secret is stored in the database so that codes can be checked; recovery codes are stored only as hashes. Protect database backups accordingly.

## What has been tested

- The sign-in flow, role mapping, refusals, token verification (wrong signature, audience and nonce), replayed and expired attempts, and the second-factor requirement: in the automated tests, against an identity provider run inside the test.
- By hand in a browser against **Keycloak 26**: sign-in through the provider, roles from groups, refusal of a person without access, sign-out through the provider, and the two-step set-up with QR code and recovery codes.

Not tested against Entra ID, ADFS or other providers. The notes above for those
are from their documented behaviour, so verify with a test account.
