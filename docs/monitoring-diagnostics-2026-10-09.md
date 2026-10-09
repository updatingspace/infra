# Monitoring acceptance, 2026-10-09

## Grafana: public acceptance remains failed

The earlier HTML/API-only check returned 200 while JavaScript downloads were incomplete.
`scripts/verify-access.py` now downloads every same-origin script and stylesheet from
Grafana login, requires successful curl completion and the correct MIME type, and
rejects HTML error bodies. It preserves TLS verification and sends Basic credentials
only to the same origin through curl stdin. Run it normally on the VM for the public
Cloudflare path, or with `--origin-ip 192.168.1.176` for a separately labelled origin check.

Measured with the same hostname, credentials and assets:

- Origin: all 7 CSS/JS files complete, including 5,107,384-byte JavaScript; real Chromium
  renders the Grafana login form with no failed requests.
- Public Cloudflare path from this home network: runtime JS returns HTTP 200 but
  stalls at approximately 20–24 KB and times out; Chromium reproduces the user's
  application-files error. HTTP/1.1, HTTP/2, gzip, identity encoding and cache-busting
  did not fix this.
- During the runtime request Caddy's origin connection sent and received TCP ACKs
  for 36,737 bytes, with an empty send queue. This is transport evidence, not proof
  of successful Cloudflare application processing.
- Cloudflare has no Worker route or Page Rule for these hosts. Its only custom
  response-settings rule matches ID, not Grafana. Rocket Loader and minification
  are disabled. DNS proxy remains enabled.
- An independent Cloudflare-hosted 135,355-byte cdnjs asset succeeds, even when
  resolving cdnjs to the same Cloudflare IP used by Grafana. A general Cloudflare
  outage or ISP-wide block is therefore not established.

No Caddy, DNS, firewall or application configuration was changed to hide this failure.
Public access is NOT accepted on the strength of the green origin test. Local DNS
routing is a possible operator-selected workaround, not a confirmed public fix.

## Alertmanager: delivery works; notifications remain pending

`--cluster.listen-address=` deliberately disables HA gossip for the single instance;
`Cluster Status: disabled` does not disable receiving alerts. Prometheus discovers
`http://alertmanager:9093/api/v2/alerts`; all 22 production rules evaluated without
errors. At the check time no production rule was firing and notification errors were zero.

An isolated temporary Prometheus evaluator fired a labelled synthetic rule into the
existing Alertmanager. The alert appeared active. The disposable evaluator was then
terminated, its exact temporary directory removed, and the test alert resolved;
the main Prometheus remained ready. Production rules and deployments were untouched.
The configured `local-ui` receiver has no external integration: Telegram delivery
has deliberately not been configured or tested yet.

## Route comparison after ID cutover

Fresh cache-MISS requests from HOME now completed for Kuma (2,272,187 decoded
bytes) and Portal (950,639 bytes), both through the CDG edge. Grafana's small
API health response succeeds, while its runtime JavaScript still times out before
headers in a 12-second probe. The same Grafana runtime, requested by local curl
through a temporary SSH SOCKS transport via the retained operator cloud VM,
completed through FRA in 1.151 seconds: 54,939 decoded bytes, SHA256
`a89f351e8bcf70f81012634e0d1344afb5df3456241832b17996f00e68f23fe5`, equal to origin.
TLS verification stayed enabled end-to-end; credentials remained in the local
curl process. The forwarding process was terminated after the bounded GET test.
This isolates a route-dependent failure; it does not establish which provider or
network segment causes it. CPU pressure, missing Basic-auth caching and gzip alone
are not supported as explanations: warm origin requests take about 13–23 ms,
the auth cache is enabled, and the same gzipped response completed externally.

A fresh HOME Chromium attempt still did not render GlitchTip's login; individual
reported pending chunks then completed through curl. A full external browser login was not exercised; the external evidence above
is limited to complete authenticated GET responses. No system/router DNS change has been applied; its location is an
operator choice. No speculative compression, auth or Cloudflare-proxy changes
were made to disguise the failure.

The subsequent controlled Grafana-only trial set upstream `Accept-Encoding` to
`identity`. Origin returned all 54,939 bytes with Content-Length, but the public
request still timed out. The trial was reverted; live Caddy was verified back at
SHA256 `a42c0259a66fc35e3f10c43b4b0fff4e4e9d16e73bee8a79a53873da22c097dc`,
including the final ID and Portal routes. A process-local Chromium HTTP/3 trial
also left GlitchTip login unrendered. Neither experiment changed global DNS or
Cloudflare settings, and no experimental proxy setting remains active.
