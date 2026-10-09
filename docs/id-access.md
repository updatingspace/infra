# ID gate for operational services

User requirement: only an authenticated staff account with the system/network
administrator role may access Grafana, Prometheus or Alertmanager from outside.
Expected issuer is `https://id.updspace.com`. Exact role identifier, staff claim,
client registration and authorization endpoint await the ID owner contract.
Do not infer that any logged-in ID user, a verified email, or Grafana Admin role
satisfies this requirement.

Replace the shared `monitoring_access` snippet with a verified OIDC/session gate
that enforces BOTH staff and the required role on every service/API request.
Use an existing ID gateway or a standard OIDC proxy once the actual ID claims are
confirmed; do not implement a new identity system in Caddy config.

Before removing the temporary Basic password, verify unauthenticated rejection,
ordinary-user rejection, staff-without-role rejection, nonstaff-with-role
rejection, allowed-user access to all three UI/API endpoints, expiry/revocation,
role removal and fail-closed behavior when ID is unavailable. Prevent direct
internet bypass to NodePorts/backends and do not trust client-supplied identity
headers. Keep current Grafana authentication until its SSO mapping is verified.
The public status page and game UDP do not inherit this admin-only policy.

## Source contract verified by the ID migration owner, 2026-10-09

Production source `5bfa8dad`: authorize `/oauth/authorize`, token `/oauth/token`,
userinfo `/oauth/userinfo`, JWKS `/.well-known/jwks.json`, RS256 and PKCE S256.
Userinfo includes `master_flags.system_admin` from `usid_user.system_admin` plus
active/suspended/banned status. It does NOT include a staff claim. No observability
OIDC client has been created. Therefore the required conjunction is not currently
implementable by simply configuring an existing claim. The ID owner must provide
an authoritative staff+role authorization contract before the SSO cutover.
