# Sign-in

RadFetch offers several ways to sign in. Pick by the size of the set-up and what
your organisation already has. They can be combined.

| Way in | Suits | Set up with |
|---|---|---|
| [Simple sign-in](#simple-sign-in) | One person or a small team on one machine | One setting |
| [RadFetch accounts](#radfetch-accounts) | A team without a central directory | `radfetch user create` or the Users page |
| [LDAP](#ldap-and-active-directory) | An organisation with Active Directory or another LDAP directory | A few settings; people use their directory password |
| [OpenID Connect](#openid-connect) | Microsoft Entra ID, ADFS 2016+, Keycloak, Okta and most modern providers | Register RadFetch at the provider |
| [SAML 2.0](#saml-20) | ADFS and other providers that offer SAML | Register RadFetch at the provider |
| API tokens | Programs and scripts | The Account page, or `radfetch token create` |

Prefer single sign-on (OpenID Connect or SAML) where your organisation offers
it: people never type their directory password into RadFetch, and the
organisation's own sign-in policy, including its second factor, applies. LDAP is
for when single sign-on is not available.

[Two-step sign-in](#two-step-sign-in) adds an authenticator-app code to any
password sign-in.

Until any of these is set up, RadFetch is open to whoever can reach it. That is
only acceptable on a machine nobody else can reach.

## Simple sign-in

For a small set-up. Put one password in `.env`:

```
RADFETCH_SIMPLE_PASSWORD=a-long-password-for-the-team
```

Restart (`docker compose up -d`). The sign-in page now asks only for that
password, and everyone who knows it signs in as the built-in administrator
`admin`.

- **Changing it:** edit `.env` and restart. The old password stops working.
- **Forgotten it:** read it in `.env`.
- **Locked out** after ten wrong tries: wait 15 minutes, or restart.
- It must be at least 10 characters. Two-step sign-in can be added on top (Account page).
- Everyone is the same person in the audit log. When that starts to matter, create accounts (below); the shared password keeps working next to them, and "Sign in with a username instead" on the sign-in page leads to them.

## RadFetch accounts

Accounts with their own password, role and permissions. See
[Users and permissions](users.md).

```bash
docker compose exec api radfetch user create anna --role admin
```

## Roles for people from a directory

LDAP, OpenID Connect and SAML share how a person is given access:

```
RADFETCH_GROUP_ROLE_MAP={"radfetch-admins": "admin", "ai-team": "operator", "radiology": "viewer"}
```

- **The role comes from the person's directory groups at every sign-in.** Moving someone between groups changes their role the next time they sign in. With several matching groups the highest role wins. Group names compare without regard to case.
- **People in no mapped group are refused**, with a message saying they have not been given access. To admit everyone the directory knows, set `RADFETCH_EXTERNAL_DEFAULT_ROLE` (for example `monitor`).
- **Individual exceptions.** An administrator can create an account for one person beforehand, marked "signs in through the directory" (or `radfetch user create <name> --role submitter --sso`). The name must be exactly the directory's username. That person is admitted with that role even without a mapped group.
- **Individual permissions and flow limits stay in RadFetch.** Grants, denies and flow restrictions set on the Users page are kept when the role changes.
- **A person is recognised by the directory's permanent identifier**, so a later change of username does not create a second account.
- **A RadFetch password account or service account is never taken over** by someone from the directory with the same name. Sign-in is refused with a message to ask an administrator.
- **The only administrator is never demoted by the directory.** If a group change would leave RadFetch without an administrator, that person keeps the role and the audit log records it.

`RADFETCH_OIDC_ROLE_MAP` and `RADFETCH_OIDC_DEFAULT_ROLE` are older names for the
two settings and still work.

## LDAP and Active Directory

People type their directory username and password into RadFetch's own sign-in
form. RadFetch checks the password with the directory and never stores it.
RadFetch password accounts (such as a local administrator) are checked by
RadFetch and never sent to the directory, so they keep working when the
directory does not.

### Active Directory

Ask the directory team for a read-only service account, then:

```
RADFETCH_LDAP_URL=ldaps://dc1.hospital.example:636
RADFETCH_LDAP_BIND_DN=CN=svc-radfetch,OU=Service Accounts,DC=hospital,DC=example
RADFETCH_LDAP_BIND_PASSWORD=...
RADFETCH_LDAP_USER_BASE=OU=Users,DC=hospital,DC=example
RADFETCH_GROUP_ROLE_MAP={"RadFetch-Admins": "admin", "Radiology": "viewer"}
```

The defaults fit Active Directory: people are found by `sAMAccountName` and
their groups come from `memberOf`. A group in the role map can be written as its
name (`Radiology`) or its full DN.

Without a service account, RadFetch can sign in as the person directly:

```
RADFETCH_LDAP_URL=ldaps://dc1.hospital.example:636
RADFETCH_LDAP_USER_DN_TEMPLATE={username}@hospital.example
RADFETCH_LDAP_USER_BASE=DC=hospital,DC=example
```

### OpenLDAP and others

```
RADFETCH_LDAP_URL=ldaps://ldap.example.org
RADFETCH_LDAP_BIND_DN=cn=radfetch,ou=services,dc=example,dc=org
RADFETCH_LDAP_BIND_PASSWORD=...
RADFETCH_LDAP_USER_BASE=ou=people,dc=example,dc=org
RADFETCH_LDAP_USER_FILTER=(uid={username})
RADFETCH_LDAP_USERNAME_ATTRIBUTE=uid
```

If the directory does not fill in `memberOf`, let RadFetch search for groups:

```
RADFETCH_LDAP_GROUP_BASE=ou=groups,dc=example,dc=org
RADFETCH_LDAP_GROUP_FILTER=(member={user_dn})
```

(use `uniqueMember` for `groupOfUniqueNames` groups). In Active Directory the same
search with `(member:1.2.840.113556.1.4.1941:={user_dn})` includes **nested**
groups, which `memberOf` does not.

### Encryption

Passwords travel to the directory, so the connection must be encrypted:
`ldaps://`, or `ldap://` with `RADFETCH_LDAP_START_TLS=true`. RadFetch refuses to
start with plain `ldap://`; `RADFETCH_LDAP_ALLOW_INSECURE=true` overrides that
for a test directory only.

The directory's certificate is checked. Give the CA that issued it in
`RADFETCH_LDAP_CA_FILE` (mounted into the `api` container) unless the system
trusts it already. Use the host name the certificate is issued for in the URL:
an IP address is not accepted even when the certificate lists it.

### Things to know

- If the directory cannot be reached, or its certificate is not trusted, people see "The identity provider could not be reached" rather than "wrong password"; the API log says which.
- Ten wrong passwords in a row lock sign-in for that person in RadFetch for 15 minutes, before the directory's own lock-out policy is reached.
- The service account needs only to read people (and groups, with a group search). It never needs to write.

## OpenID Connect

### 1. Register RadFetch at the provider

Create an application ("client", "app registration") with:

| Setting | Value |
|---|---|
| Type | Web application, authorization code flow (a confidential client with a secret) |
| Redirect URI | `https://<your RadFetch address>/auth/oidc/callback` |
| Post-logout redirect URI | `https://<your RadFetch address>/` |
| Scopes | `openid profile email` |
| Group claim | The person's groups in the ID token, in a claim named `groups` |

### 2. Configure RadFetch

```
RADFETCH_PUBLIC_URL=https://radfetch.hospital.example
RADFETCH_OIDC_ISSUER=https://login.hospital.example/realms/hospital
RADFETCH_OIDC_CLIENT_ID=radfetch
RADFETCH_OIDC_CLIENT_SECRET=...
RADFETCH_GROUP_ROLE_MAP={"radfetch-admins": "admin", "radiology": "viewer"}
```

Restart the API. The sign-in page gets a single sign-on button.

| Provider | Issuer URL | Notes |
|---|---|---|
| Keycloak | `https://<host>/realms/<realm>` | Add a "Group Membership" mapper to the client with claim name `groups`, "full group path" off |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant id>/v2.0` | Add a groups claim under Token configuration. Entra sends group **object IDs**, not names: use the IDs as keys in the role map |
| ADFS 2016 and later | `https://<host>/adfs` | Issue group membership as a claim named `groups` |

Adjustments if the provider differs: `RADFETCH_OIDC_USERNAME_CLAIM` (default
`preferred_username`; `email` and `upn` are common), `RADFETCH_OIDC_GROUPS_CLAIM`
(default `groups`; `roles` at some providers), `RADFETCH_OIDC_SCOPES`,
`RADFETCH_OIDC_BUTTON_LABEL`.

## SAML 2.0

### 1. Register RadFetch at the provider

RadFetch publishes its SAML metadata at
`https://<your RadFetch address>/auth/saml/metadata`. Most providers can import
it. Otherwise enter:

| Setting | Value |
|---|---|
| Entity ID (identifier, "relying party trust" identifier) | `https://<your RadFetch address>/auth/saml/metadata` |
| Assertion consumer service (reply URL), HTTP-POST | `https://<your RadFetch address>/auth/saml/acs` |
| Signing | Sign the assertion, or the response, or both, with SHA-256 |
| Encryption | Off (encrypted assertions are not supported) |
| NameID | A stable identifier for the person, such as their username |
| Attributes | The person's groups, in an attribute named `groups` |

In ADFS: add a relying party trust from the metadata URL, and claim rules that
send "Token-Groups - Unqualified Names" as an outgoing claim named `groups` and
"SAM-Account-Name" as the Name ID.

### 2. Configure RadFetch

```
RADFETCH_PUBLIC_URL=https://radfetch.hospital.example
RADFETCH_SAML_IDP_METADATA=https://adfs.hospital.example/FederationMetadata/2007-06/FederationMetadata.xml
RADFETCH_GROUP_ROLE_MAP={"RadFetch-Admins": "admin", "Radiology": "viewer"}
```

`RADFETCH_SAML_IDP_METADATA` can also be a file path (mount it into the `api`
container). The metadata is re-read every hour, so a signing-certificate renewal
at the provider is picked up without a restart.

Adjustments: `RADFETCH_SAML_USERNAME_ATTRIBUTE` (default: use the NameID),
`RADFETCH_SAML_GROUPS_ATTRIBUTE` (default `groups`; ADFS may send the long claim
URI, which works as a value here too), `RADFETCH_SAML_ENTITY_ID`,
`RADFETCH_SAML_BUTTON_LABEL`.

### How answers are checked

RadFetch accepts a SAML answer only if all of these hold: it is signed with the
provider's certificate from its metadata (SHA-256 or stronger; SHA-1 is refused),
it holds exactly one assertion, the assertion is from the provider, meant for
RadFetch, not expired, and answers the sign-in RadFetch started. Everything is
read only from the part the signature covers, which defeats the known
"signature wrapping" attacks. Each answer works once.

## Both kinds of single sign-on

- **Several at once.** OpenID Connect and SAML can both be configured; the sign-in page shows a button for each.
- **`RADFETCH_PUBLIC_URL` is required** and must be the address people use. RadFetch refuses to start with single sign-on configured and no public URL.
- **Use HTTPS.** Over plain HTTP a SAML sign-in cannot be tied to the browser that started it, so a sign-in could be completed in someone else's browser. Over HTTPS RadFetch prevents this for both kinds.
- **Keep one password administrator** for when the provider is down. `RADFETCH_PASSWORD_LOGIN=false` hides the password form; only do that if you accept managing RadFetch from the command line during an outage.
- **Signing out** of an OpenID Connect session also signs out at the provider, if it supports that.
- **Removing someone at the provider** stops new sign-ins. An open RadFetch session lasts until it expires (`RADFETCH_SESSION_HOURS`), so disable the account in RadFetch too if access must end at once.

## Two-step sign-in

### With single sign-on

The provider performs the second factor. The dependable place to require it is
at the provider: a policy that demands multi-factor authentication for RadFetch.

RadFetch can additionally refuse sign-ins for which the provider does not state
that a second factor was used:

- OpenID Connect: `RADFETCH_OIDC_REQUIRE_AMR=mfa` checks the token's `amr` claim. `RADFETCH_OIDC_REQUIRE_ACR` requires an exact `acr` value, and `RADFETCH_OIDC_ACR_VALUES` asks the provider for it.
- SAML: `RADFETCH_SAML_REQUIRE_AUTHN_CONTEXT` requires an authentication context, for ADFS `http://schemas.microsoft.com/claims/multipleauthn`.

Providers differ in whether and how they report this, so try it with a test
account before relying on it.

### With a password (RadFetch accounts, simple sign-in and LDAP)

Each person can turn on two-step sign-in on the **Account** page: scan a QR code
with an authenticator app (Microsoft Authenticator, Google Authenticator,
1Password and others), enter the code it shows, and save the ten recovery codes.
From then on signing in needs the password and a code.

```
RADFETCH_REQUIRE_MFA=true
```

makes it mandatory: a person without it can sign in but do nothing except set it
up, and it cannot be turned off again.

- **Lost phone.** A recovery code works once in place of an app code. An administrator can also remove a person's two-step sign-in (Users page, or `radfetch user reset-mfa <name>`).
- **Wrong codes count** towards the lock-out, like wrong passwords. A used code is refused if tried again.
- **Clocks matter.** One 30-second step either way is accepted.
- **API tokens are not affected.** Give tokens the lowest role that works.
- The authenticator secret is stored in the database so that codes can be checked; recovery codes are stored only as hashes. Protect database backups accordingly.

## What has been tested

- In the automated tests: every way in, with an OpenID Connect provider, a SAML provider (including tampered, forged, replayed and "signature wrapping" answers) and an LDAP directory simulated inside the tests.
- By hand against real servers: **Keycloak 26** over OpenID Connect and over SAML, in a browser, and **OpenLDAP 2.4** over LDAPS and StartTLS, with groups from `memberOf` and from a group search, including refusal of an untrusted certificate and of an unencrypted connection.

Not tested against Microsoft Entra ID, ADFS or Active Directory itself. The notes
for those come from their documented behaviour, so verify with a test account.
