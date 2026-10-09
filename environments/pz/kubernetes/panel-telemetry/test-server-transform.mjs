import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { transformIndex, transformDatabase } from './server-transform.mjs';

const PROVIDER = 'file:///opt/panel-telemetry/provider.mjs';
const INDEX = `app.get("/api/health", (req, res) => {
  res.json({
    status: "ok",
    version: _pkgVersion,
  });
});
async function sample() {
      const hostMem = os.totalmem();
      const hostMemFree = os.freemem();
      const cpuUsage = getCpuUsage();
      const pzMemBytes = await getPzProcessMemory();
      const swap = await getSwapSnapshot();
      const activeServerForSnapshot = await getActiveServer().catch(() => null);
      const snapshot = {
        serverId: activeServerForSnapshot?.id ?? null,
        hostMemTotal: hostMem, hostMemUsed: hostMem - hostMemFree, cpuUsage, pzMemUsed: pzMemBytes
      };
      await recordPerformanceSnapshot(snapshot);
      io.to('perf').emit('perf:snapshot', snapshot);
}`;
const DATABASE = `export async function getPerformanceHistory(limit = 60, serverId = undefined) {
  const source = rows;
  return source.slice(-safeLimit);
}`;

test('one adapted snapshot feeds persisted history and live socket, with honest heap and no node swap', () => {
  const result = transformIndex(INDEX, PROVIDER);
  assert.match(result, /if \(!gameTelemetry\) return/);
  assert.match(result, /hostMem = gameTelemetry.memoryLimitBytes/);
  assert.match(result, /pzMemBytes = gameTelemetry.jvmHeapUsedBytes/);
  assert.match(result, /const swap = null/);
  assert.match(result, /telemetryScope: gameTelemetry.scope/);
  assert.match(result, /jvmHeapMaxBytes: gameTelemetry.jvmHeapMaxBytes/);
  assert.match(result, /telemetry: telemetryHealth\(\)/);
  assert.match(result, /recordPerformanceSnapshot\(snapshot\)/);
  assert.match(result, /emit\('perf:snapshot', snapshot\)/);
  assert.doesNotMatch(result, /os\.totalmem\(\)|os\.freemem\(\)|await getPzProcessMemory/);
});

test('incompatible or duplicated anchors abort instead of silently displaying host values', () => {
  assert.throws(() => transformIndex(INDEX.replace('os.totalmem()', 'other.totalmem()'), PROVIDER), /incompatible_sampling/);
  assert.throws(() => transformIndex(INDEX + INDEX, PROVIDER), /incompatible_sampling/);
  assert.throws(() => transformDatabase(DATABASE.replace('return source.slice', 'return source.filter'), PROVIDER), /incompatible_history_filter/);
});

test('history filter is applied without deleting stored records', () => {
  const result = transformDatabase(DATABASE, PROVIDER);
  assert.match(result, /return filterTelemetryHistory\(source\)\.slice\(-safeLimit\)/);
  assert.doesNotMatch(result, /clearPerformanceHistory|\.splice\(/);
});

test('a profile switch during async sampling cannot persist or broadcast the other game', async () => {
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const body = transformIndex(INDEX, PROVIDER).replace(/^import[^\n]*\n/, '');
  const run = new AsyncFunction('app', 'getActiveServer', 'sampleForServer', 'recordPerformanceSnapshot', 'io',
    `${body}\nawait sample();`);
  const telemetry = { scope: 'game-container-v1', memoryLimitBytes: 10, memoryUsedBytes: 6, cpuPercent: 2,
    jvmHeapUsedBytes: 3, jvmHeapMaxBytes: 8, cpuLimitCores: 2.2, cpuUsageCores: 0.044, podUid: 'pod-a' };
  for (const [secondId, available, expectedCount] of [['game-a', true, 1], ['game-b', true, 0], ['game-a', false, 0]]) {
    const recorded = [], emitted = [];
    let reads = 0;
    await run({ get() {} }, async () => ({ id: reads++ ? secondId : 'game-a' }), async () => available ? telemetry : null,
      async snapshot => recorded.push(snapshot), { to: () => ({ emit: (_event, snapshot) => emitted.push(snapshot) }) });
    assert.equal(recorded.length, expectedCount);
    assert.equal(emitted.length, expectedCount);
    if (expectedCount) {
      assert.equal(recorded[0], emitted[0]);
      assert.equal(recorded[0].hostMemUsed, 6);
      assert.equal(recorded[0].pzMemUsed, 3);
      assert.equal(recorded[0].serverId, 'game-a');
    }
  }
});

test('reviewed upstream v1.4.0 source satisfies every strict server anchor', { skip: !fs.existsSync('/tmp/pz-panel-upstream-v1.4.0/server/index.js') }, () => {
  assert.ok(transformIndex(fs.readFileSync('/tmp/pz-panel-upstream-v1.4.0/server/index.js', 'utf8'), PROVIDER));
  assert.ok(transformDatabase(fs.readFileSync('/tmp/pz-panel-upstream-v1.4.0/server/database/init.js', 'utf8'), PROVIDER));
});
