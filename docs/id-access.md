# ID access to operational services

Grafana, Prometheus, Alertmanager, GlitchTip UI and Kuma administration require an
active UpdSpace ID account with BOTH `auth_user.is_staff` and
`usid_user.system_admin`. A verified email, an ID login alone or the application's
own administrator role does not satisfy this policy. Public Kuma status and game
UDP are outside this policy; GlitchTip SDK ingestion uses its project DSN.

## Request path

Browser → shared Caddy → oauth2-proxy session → live ID authorization → application.
The existing Cloudflare proxy remains enabled. Only the operator workstation has
an explicitly approved local hosts block for these five domains; see
`environments/workstation/README.md`. There is no competing HTTP edge.

ID issuer `https://id.updspace.com`, RS256, nonce and PKCE S256 are checked.
The confidential `observability` client has exactly seven callback addresses (five edge callbacks plus native Grafana and GlitchTip),
authorization-code/refresh grants and openid/email/profile/offline_access scopes.
`prompt=consent` replaces oauth2-proxy's legacy `approval_prompt=force`, which ID's
strict authorization parser rejects. Consent stays under the user's control.
The operator explicitly authorized initial client registration through root IaC
on the VM; no personal account, password, MFA bypass or user privilege grant was used.

`GET /oauth/observability-access` reuses ID's access-token verification: signature,
issuer, expiry, persisted token/client binding, revocation and active principal.
It reads staff and system_admin from the database on every request and accepts only
tokens issued for `observability`. Result: 204 allowed, 403 insufficient rights,
401 missing/expired/revoked token, 503 unavailable backend. Failure denies access.
There is no role cache in Caddy or oauth2-proxy. Open streaming connections have a
one-minute maximum lifetime; their reconnect repeats authorization.

Caddy removes caller-supplied identity headers and Authorization, gets the access
token only from its private session service, checks ID, then removes the token
before forwarding. Session cookies are host-only `__Host-observability`, Secure,
HttpOnly, SameSite=Lax; no `.updspace.com` domain cookie is shared with other apps.
All cookie chunks/refreshes are preserved. Sessions expire after eight hours and
refresh every five minutes; loss of a role is enforced at the next HTTP request.
OAuth2-proxy request/auth logs are disabled to avoid callback secrets in logs.

Grafana and Kuma Services are ClusterIP; old NodePorts 30030/30031 and the old LAN
NetworkPolicy grants are closed. Grafana uses native Generic OAuth with automatic
login, PKCE S256, refresh tokens and profile synchronization. Its immutable identity binding is ID `sub`, while the visible login uses
`preferred_username`; strict role mapping requires all active/staff/system_admin flags and grants
organization Admin, never Grafana server administrator. Password/basic authentication
is disabled; the old admin Secret and database are retained for recovery. Insecure
email-based account linking remains disabled. The client Secret is installed as
`observability/grafana-oidc`; scoped NetworkPolicies allow only Grafana → ID:8089.

GlitchTip advertises **UpdSpace ID** through its native OpenID Connect provider,
linked to the existing organization, UpdatingSpace LLC. New identities receive normal member
access; the existing owner remains unchanged. Public/social general registration
stays disabled. Provider configuration lives in `glitchtip/oidc.json` and is applied
idempotently by `configure-oidc.py`. Its callback is
`https://errors.updspace.com/accounts/oidc/updspace/login/callback/`.
Provider discovery uses the public HTTPS issuer via a pod-local host alias to the
common edge; certificate verification stays enabled. Existing password login is
retained for recovery. Kuma still has its own application login.

GlitchTip always fetches userinfo. For tokens issued to `observability`, ID applies
the same fresh active/staff/system_admin check to `/oauth/userinfo` before returning
identity data. This also protects native login after switching ID accounts; other
OAuth clients retain their existing userinfo behavior.
The public status API is a narrow GET/HEAD allowlist; administrator Socket.IO is
protected. Only numeric GlitchTip POST/OPTIONS envelope/minidump paths bypass ID.

## Reproduction and recovery

Implementation, quotas, client bootstrap and deployment order are in
`environments/home/observability-auth/README.md`. The ID patch and exact mutations
image digest are in `environments/home/id-platform/id/overlays/production`.
Changes to ID source must preserve this patch until it is incorporated upstream.

The existing encrypted ID backup now includes the oauth2-proxy Kubernetes Secret
and `observability-oidc.json`. Restore the latter to
`/opt/updspace-infra/private/observability-oidc.json`, root 0600, alongside the same
YDB client record. Do not generate a replacement secret when restoring an existing
client. Cookie-key loss invalidates sessions; client-secret loss prevents login.
The current workstation offsite policy remains disabled for ID/Portal.

Before the gate cutover, Caddy ConfigMap/Deployment were retained at
`/opt/updspace-infra/private/before-monitoring-id-edge.json`; original ID deployment
at `before-id-observability.json`. Old Basic credentials/Secret remain only for
rollback. Restore a consistent edge ConfigMap/Deployment pair and validate it
before restarting Caddy. Do not leave the new gate pointing at an old ID that lacks
the authorization endpoint, and never roll back to unauthenticated monitoring.
NodePorts need not be reopened for rollback.

## Evidence and limits

Policy truth table and the real isolated YDB OAuth exchange test passed, including
fresh flag changes under one access token, issuer/signature checks and revocation.
A real Caddy integration test covers forged headers, two cookie chunks, no token
leak to the backend, role rejection, revoked tokens and ID outages. CI runs it with
Caddy 2.11.4 and a pinned official archive checksum.

Live acceptance checks redirects for all five hosts, then follows each request to
ID and verifies that it reaches `/login`, public Kuma routes, closed NodePorts,
client secret recognition and GlitchTip Cloudflare ingestion. No production user
or privileged test identity was created. The operator subsequently confirmed native Grafana login, name and email sync
with a profile screenshot. Personal GlitchTip callback and actual production role
removal have not been verified.

Native OAuth acceptance uses the actual pinned Grafana image with a disposable
local database and mock IdP: rejects missing staff/admin combinations, provisions
an organization Admin, preserves its identity while name/email change, and does
not grant server administrator. GlitchTip's live headless start verifies CSRF,
provider discovery, exact HTTPS callback and S256. Personal consent/account login
is still operator-controlled; we did not accept consent on the operator's behalf.

Grafana Generic OAuth 13.2.3 does not consume the OIDC `picture` claim; its
profile-image mechanism is Gravatar-compatible. ID already exposes scoped `picture`,
but an ID-to-Grafana avatar integration is not part of this native login change.
