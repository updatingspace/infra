import assert from 'node:assert/strict';
import test from 'node:test';
import { createProvider, durationMs, parseHeap, quantity } from './provider.mjs';

const SERVER = '806c8584-0e2b-4c53-a255-3c244cde82ba';
const TIME = Date.parse('2026-09-30T19:30:00Z');
const ENV = {
  PZ_TELEMETRY_NAMESPACE: 'zomboid', PZ_TELEMETRY_POD: 'zomboid-0', PZ_TELEMETRY_CONTAINER: 'zomboid',
  PZ_TELEMETRY_SERVER_ID: SERVER, PZ_TELEMETRY_SCOPE: 'game-container-v1',
  PZ_TELEMETRY_PROMETHEUS_URL: 'http://zomboid:9090/metrics',
  KUBERNETES_SERVICE_HOST: '10.43.0.1', KUBERNETES_SERVICE_PORT: '443',
};
const HEAP = 'jvm_memory_used_bytes{area="heap",id="Eden"} 1073741824\n'
  + 'jvm_memory_used_bytes{area="heap",id="Old"} 2147483648\n'
  + 'jvm_memory_used_bytes{area="nonheap",id="Meta"} 123456\n'
  + 'jvm_memory_max_bytes{area="heap",id="Eden"} -1\n'
  + 'jvm_memory_max_bytes{area="heap",id="Old"} 8589934592\n';

function fixture() {
  const calls = [];
  let time = TIME;
  const pod = {
    metadata: { name: 'zomboid-0', namespace: 'zomboid', uid: 'fixture-pod',
      labels: { 'app.kubernetes.io/name': 'zomboid' },
      ownerReferences: [{ kind: 'StatefulSet', name: 'zomboid', uid: 'fixture-sts', controller: true }] },
    spec: { containers: [{ name: 'zomboid', resources: { limits: { cpu: '2200m', memory: '10Gi' } } }] },
    status: { phase: 'Running', containerStatuses: [{ name: 'zomboid', ready: true, containerID: 'containerd://fixture',
      state: { running: { startedAt: '2026-09-30T18:00:00Z' } } }] },
  };
  const metrics = {
    metadata: { name: 'zomboid-0', namespace: 'zomboid' }, timestamp: '2026-09-30T19:29:45Z', window: '30s',
    containers: [{ name: 'zomboid', usage: { cpu: '40m', memory: '6532636Ki' } }],
  };
  const options = {
    env: ENV, now: () => time, readText: path => path.endsWith('/token') ? 'fixture-token' : 'fixture-ca',
    request: async (url, config) => {
      calls.push({ url, config });
      if (url.startsWith('http://zomboid:9090')) return HEAP;
      return JSON.stringify(url.includes('metrics.k8s.io') ? metrics : pod);
    },
  };
  return { calls, pod, metrics, options, advance: milliseconds => { time += milliseconds; } };
}

test('game CPU uses the 2.2-core quota, memory uses working set/10Gi, heap remains separate', async () => {
  const f = fixture();
  const provider = createProvider(f.options);
  const sample = await provider.sampleForServer({ id: SERVER });
  assert.equal(sample.cpuLimitCores, 2.2);
  assert.equal(sample.cpuUsageCores, 0.04);
  assert.ok(Math.abs(sample.cpuPercent - 1.8181818181818181) < 1e-9);
  assert.equal(sample.memoryLimitBytes, 10 * 1024 ** 3);
  assert.equal(sample.memoryUsedBytes, 6532636 * 1024);
  assert.equal(sample.jvmHeapUsedBytes, 3 * 1024 ** 3);
  assert.equal(sample.jvmHeapMaxBytes, 8 * 1024 ** 3);
  assert.equal(provider.health().ready, true);
  assert.equal(f.calls.length, 4);
  assert.equal(f.calls.filter(call => call.config.headers?.Authorization === 'Bearer fixture-token').length, 3);
  assert.equal(f.calls.find(call => call.url.startsWith('http:')).config.headers, undefined);
  assert.ok(f.calls.filter(call => call.url.startsWith('https:')).every(call => call.url.endsWith('/namespaces/zomboid/pods/zomboid-0')));
});

test('another active server never obtains this game telemetry and legacy host history stays excluded', async () => {
  const f = fixture();
  const provider = createProvider(f.options);
  assert.equal(await provider.sampleForServer({ id: 'different-server' }), null);
  assert.equal(f.calls.length, 0);
  const rows = [
    { serverId: SERVER, cpuUsage: 92 },
    { serverId: SERVER, telemetryScope: 'game-container-v1', cpuUsage: 5 },
    { serverId: 'other', telemetryScope: 'game-container-v1', cpuUsage: 99 },
  ];
  assert.deepEqual(provider.filterHistory(rows), [rows[1]]);
  assert.equal(rows.length, 3, 'old records are preserved');
});

test('stale, wrong-container, pre-restart and unready metrics yield no sample or host fallback', async () => {
  for (const change of [
    f => { f.metrics.timestamp = '2026-09-30T19:00:00Z'; },
    f => { f.metrics.containers[0].name = 'panel'; },
    f => { f.metrics.metadata.name = 'other-pod'; },
    f => { f.pod.status.containerStatuses[0].state.running.startedAt = '2026-09-30T19:29:50Z'; },
    f => { f.pod.status.containerStatuses[0].state.running.startedAt = '2026-09-30T19:29:30Z'; },
    f => { f.pod.status.containerStatuses[0].ready = false; },
    f => { f.pod.metadata.ownerReferences[0].name = 'different-game'; },
    f => { f.pod.metadata.deletionTimestamp = '2026-09-30T19:29:30Z'; },
  ]) {
    const f = fixture();
    change(f);
    const provider = createProvider(f.options);
    assert.equal(await provider.sampleForServer({ id: SERVER }), null);
    assert.equal(provider.health().ready, false);
  }
});

test('pod UID or container change during metrics collection invalidates the sample', async () => {
  for (const field of ['uid', 'containerID']) {
    const f = fixture();
    const request = f.options.request;
    let reads = 0;
    f.options.request = async (url, options) => {
      const text = await request(url, options);
      if (!url.includes('/api/v1/namespaces')) return text;
      if (++reads !== 2) return text;
      const value = JSON.parse(text);
      if (field === 'uid') value.metadata.uid = 'new-pod';
      else value.status.containerStatuses[0].containerID = 'containerd://new';
      return JSON.stringify(value);
    };
    const provider = createProvider(f.options);
    assert.equal(await provider.sampleForServer({ id: SERVER }), null);
    assert.equal(provider.health().ready, false);
  }
  assert.equal(durationMs('30s'), 30_000);
  assert.equal(durationMs('1m0.5s'), 60_500);
  assert.throws(() => durationMs('unknown'));
});

test('a failed credential read or network sample can recover and never fabricates zeros', async () => {
  const f = fixture();
  let broken = true;
  const provider = createProvider({ ...f.options, readText: path => {
    if (broken) throw new Error('fixture-private-text-never-emitted');
    return f.options.readText(path);
  } });
  assert.equal(await provider.refresh(), null);
  broken = false;
  assert.ok(await provider.refresh());
  assert.equal(provider.health().ready, true);
  broken = true;
  assert.equal(await provider.refresh(), null);
  assert.equal(provider.health().ready, false);
  f.advance(200_000);
  assert.equal(provider.health().ready, false);
});

test('heap parser ignores native memory and unbounded pools, rejects missing and duplicate values', () => {
  assert.deepEqual(parseHeap(HEAP), { jvmHeapUsedBytes: 3 * 1024 ** 3, jvmHeapMaxBytes: 8 * 1024 ** 3 });
  assert.throws(() => parseHeap(''), /unavailable/);
  assert.throws(() => parseHeap(HEAP + 'jvm_memory_used_bytes{area="heap",id="Old"} 10\n'), /duplicate/);
  assert.equal(quantity('40000000n', 'cpu'), 0.04);
  assert.equal(quantity('2.2', 'cpu'), 2.2);
  assert.throws(() => quantity('NaN', 'cpu'));
});
