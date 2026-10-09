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
