import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';

const SCOPE = 'game-container-v1';
const MAX_AGE_MS = 120_000;
const ACCOUNT = '/var/run/secrets/kubernetes.io/serviceaccount';

function requireValue(condition, reason) {
  if (!condition) throw new Error(reason);
}

export function quantity(value, kind) {
  requireValue(typeof value === 'string', 'quantity_missing');
  const match = /^(\d+(?:\.\d+)?)([A-Za-z]*)$/.exec(value);
  requireValue(match, 'quantity_invalid');
  const factors = kind === 'cpu'
    ? { '': 1, m: 1e-3, u: 1e-6, n: 1e-9 }
    : { '': 1, Ki: 1024, Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4, k: 1000, K: 1000, M: 1e6, G: 1e9 };
  requireValue(Object.hasOwn(factors, match[2]), 'quantity_suffix_invalid');
  const number = Number(match[1]) * factors[match[2]];
  requireValue(Number.isFinite(number) && number >= 0, 'quantity_out_of_range');
  return number;
}

export function parseHeap(text) {
  let used = 0;
  let maximum = 0;
  let usedCount = 0;
  let maxCount = 0;
  const seen = new Set();
  for (const line of text.split('\n')) {
    const match = /^jvm_memory_(used|max)_bytes\{([^}]*)\}\s+(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*$/.exec(line);
    if (!match || !/(?:^|,)\s*area="heap"(?:,|$)/.test(match[2])) continue;
    const key = `${match[1]}:${match[2]}`;
    requireValue(!seen.has(key), 'duplicate_heap_series');
    seen.add(key);
    const number = Number(match[3]);
    requireValue(Number.isFinite(number), 'heap_value_invalid');
    if (match[1] === 'used') {
      requireValue(number >= 0, 'heap_used_invalid');
      used += number;
      usedCount++;
    } else if (number >= 0) {
      maximum += number;
      maxCount++;
    } // A pool max of -1 means unspecified; the bounded old-generation pool supplies the heap maximum.
  }
  requireValue(usedCount > 0 && maxCount > 0 && maximum > 0 && used <= maximum, 'heap_metrics_unavailable');
  return { jvmHeapUsedBytes: used, jvmHeapMaxBytes: maximum };
}

export function durationMs(value) {
  requireValue(typeof value === 'string' && value.length < 100, 'metrics_window_invalid');
  const factors = { ns: 1e-6, us: 1e-3, 'µs': 1e-3, ms: 1, s: 1000, m: 60_000, h: 3_600_000 };
  const matches = [...value.matchAll(/(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)/g)];
  const result = matches.reduce((sum, match) => sum + Number(match[1]) * factors[match[2]], 0);
  requireValue(matches.map(match => match[0]).join('') === value && result > 0 && result <= MAX_AGE_MS, 'metrics_window_invalid');
  return result;
}

export function requestText(url, { headers = {}, ca, timeoutMs = 5000, maxBytes = 4 * 1024 ** 2 } = {}) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const transport = target.protocol === 'https:' ? https : http;
    const request = transport.request(target, { headers, ca, rejectUnauthorized: true }, response => {
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error('telemetry_http_rejected'));
        return;
      }
      const chunks = [];
      let size = 0;
      response.on('data', chunk => {
        size += chunk.length;
        if (size > maxBytes) {
          request.destroy(new Error('telemetry_response_too_large'));
          return;
        }
        chunks.push(chunk);
      });
      response.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
      response.on('error', () => reject(new Error('telemetry_response_failed')));
    });
    // A total deadline, not an inactivity timeout which a trickle could extend.
    const timer = setTimeout(() => request.destroy(new Error('telemetry_timeout')), timeoutMs);
    request.on('close', () => clearTimeout(timer));
    request.on('error', () => reject(new Error('telemetry_request_failed')));
    request.end();
  });
}

export function createProvider({ env = process.env, request = requestText, readText = path => fs.readFileSync(path, 'utf8'), now = Date.now } = {}) {
  const namespace = env.PZ_TELEMETRY_NAMESPACE;
  const podName = env.PZ_TELEMETRY_POD;
  const containerName = env.PZ_TELEMETRY_CONTAINER;
  const serverId = env.PZ_TELEMETRY_SERVER_ID;
  const scope = env.PZ_TELEMETRY_SCOPE;
  requireValue(namespace === 'zomboid' && podName === 'zomboid-0' && containerName === 'zomboid', 'telemetry_target_invalid');
  requireValue(scope === SCOPE && /^[0-9a-f-]{36}$/i.test(serverId || ''), 'telemetry_scope_invalid');
  const host = env.KUBERNETES_SERVICE_HOST;
  const port = env.KUBERNETES_SERVICE_PORT_HTTPS || env.KUBERNETES_SERVICE_PORT || '443';
  requireValue(net.isIP(host || '') !== 0 && /^\d+$/.test(port) && Number(port) <= 65535 && Number(port) > 0, 'kubernetes_address_invalid');
  const api = `https://${host.includes(':') ? `[${host}]` : host}:${port}`;
  const prometheus = new URL(env.PZ_TELEMETRY_PROMETHEUS_URL);
  requireValue(prometheus.protocol === 'http:' && prometheus.port === '9090' && prometheus.pathname === '/metrics'
    && ['zomboid', 'zomboid.zomboid', 'zomboid.zomboid.svc', 'zomboid.zomboid.svc.cluster.local'].includes(prometheus.hostname)
    && !prometheus.username && !prometheus.password && !prometheus.search && !prometheus.hash, 'prometheus_target_invalid');
  let sample = null;
  let sampledAt = 0;
  let lastAttemptSucceeded = false;
  let inFlight = null;

  function podIdentity(pod) {
    requireValue(pod.metadata?.name === podName && pod.metadata?.namespace === namespace
      && typeof pod.metadata?.uid === 'string' && !pod.metadata.deletionTimestamp && pod.status?.phase === 'Running'
      && pod.metadata.labels?.['app.kubernetes.io/name'] === 'zomboid'
      && (pod.metadata.ownerReferences || []).some(owner => owner.kind === 'StatefulSet'
        && owner.name === 'zomboid' && owner.controller === true && typeof owner.uid === 'string'), 'game_pod_invalid');
    const statuses = (pod.status?.containerStatuses || []).filter(row => row.name === containerName);
    requireValue(statuses.length === 1 && statuses[0].ready === true
      && typeof statuses[0].containerID === 'string' && statuses[0].containerID.length > 0
      && Number.isFinite(Date.parse(statuses[0].state?.running?.startedAt)), 'game_container_invalid');
    return { uid: pod.metadata.uid, containerId: statuses[0].containerID, startedAt: statuses[0].state.running.startedAt };
  }

  async function refresh() {
    if (inFlight) return inFlight;
    inFlight = Promise.resolve().then(async () => {
      try {
        const token = readText(`${ACCOUNT}/token`).trim();
        const ca = readText(`${ACCOUNT}/ca.crt`);
        requireValue(token.length > 0, 'service_account_token_missing');
        const options = { ca, headers: { Authorization: `Bearer ${token}` }, maxBytes: 1024 ** 2 };
        const [podText, metricsText, heapText] = await Promise.all([
          request(`${api}/api/v1/namespaces/${namespace}/pods/${podName}`, options),
          request(`${api}/apis/metrics.k8s.io/v1beta1/namespaces/${namespace}/pods/${podName}`, options),
          request(prometheus.href, { maxBytes: 4 * 1024 ** 2 }),
        ]);
        const pod = JSON.parse(podText);
        const metrics = JSON.parse(metricsText);
        const identity = podIdentity(pod);
        const containers = (pod.spec?.containers || []).filter(row => row.name === containerName);
        const usage = (metrics.containers || []).filter(row => row.name === containerName);
        requireValue(containers.length === 1 && usage.length === 1
          && metrics.metadata?.name === podName && metrics.metadata?.namespace === namespace, 'game_container_invalid');
        const timestamp = Date.parse(metrics.timestamp);
        const started = Date.parse(identity.startedAt);
        requireValue(Number.isFinite(timestamp) && timestamp - durationMs(metrics.window) >= started
          && now() - timestamp <= MAX_AGE_MS && timestamp - now() <= 30_000, 'game_metrics_stale');
        const cpuLimitCores = quantity(containers[0].resources?.limits?.cpu, 'cpu');
        const memoryLimitBytes = quantity(containers[0].resources?.limits?.memory, 'memory');
        const cpuUsageCores = quantity(usage[0].usage?.cpu, 'cpu');
        const memoryUsedBytes = quantity(usage[0].usage?.memory, 'memory');
        requireValue(cpuLimitCores > 0 && memoryLimitBytes > 0, 'game_limits_missing');
        const after = podIdentity(JSON.parse(await request(`${api}/api/v1/namespaces/${namespace}/pods/${podName}`, options)));
        requireValue(after.uid === identity.uid && after.containerId === identity.containerId
          && after.startedAt === identity.startedAt, 'game_changed_during_sampling');
        sample = {
          scope, podUid: pod.metadata.uid, timestamp: new Date(timestamp).toISOString(),
          cpuLimitCores, cpuUsageCores, cpuPercent: Math.min(100, cpuUsageCores / cpuLimitCores * 100),
          memoryLimitBytes, memoryUsedBytes, ...parseHeap(heapText),
        };
        sampledAt = now();
        lastAttemptSucceeded = true;
        return sample;
      } catch {
        // Missing/stale telemetry must never become host data or a fabricated zero.
        lastAttemptSucceeded = false;
        return null;
      } finally {
        inFlight = null;
      }
    });
    return inFlight;
  }

  return {
    refresh,
    async sampleForServer(server) {
      if (String(server?.id) !== serverId) return null;
      if (sample && lastAttemptSucceeded && now() - sampledAt < 15_000) return sample;
      return refresh();
    },
    filterHistory(rows) {
      return rows.filter(row => row.telemetryScope === scope && String(row.serverId) === serverId);
    },
    health() {
      const fresh = sample && now() - Date.parse(sample.timestamp) <= MAX_AGE_MS;
      return { adapter: 'kubernetes-game-v1', compatibility: 'verified', scope,
        ready: Boolean(fresh && lastAttemptSucceeded),
        lastSampleAt: sample?.timestamp ?? null,
        cpuLimitCores: sample?.cpuLimitCores ?? null,
        memoryLimitBytes: sample?.memoryLimitBytes ?? null };
    },
  };
}

let singleton;
function provider() { return singleton ??= createProvider(); }
export const sampleForServer = server => provider().sampleForServer(server);
export const filterTelemetryHistory = rows => provider().filterHistory(rows);
export const telemetryHealth = () => provider().health();
export const primeTelemetry = () => provider().refresh();
