# Portal LAN stage, 2026-10-09

The shared Caddy serves `portal.updspace.com` only to LAN/pod source addresses.
`/api/v1/*` preserves the path to the BFF; other paths reach the frontend, which
owns SPA fallback. Public Portal DNS still targets the cloud service. External
requests reaching this origin receive 503. Never substitute this stage acceptance
for a public cutover check.

Secret `edge/portal-origin-tls` contains a separate Let's Encrypt certificate only
for `portal.updspace.com`, expiring 2027-01-07. The key was generated on the VM in
`/opt/updspace-portal/acme` using pinned Certbot 5.8.0 in a temporary k3s pod.
DNS-01 used a single temporary `_acme-challenge.portal.updspace.com` TXT; both
authoritative nameservers were checked, then the exact TXT and temporary pod were
removed. Existing A/CNAME/proxy settings were not changed. The broad Cloudflare
Origin wildcard private key was not exported.

This manual stage certificate does not renew automatically. At final public cutover,
hand certificate management to shared Caddy ACME, or renew DNS-01 before expiry.
ID owner verified Portal↔ID Chromium login with MFA, consent, session and normal
TLS verification using local DNS overrides; public routing was unchanged.
