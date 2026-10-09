import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import test from 'node:test';
import { applyClientOverlay, transformClientSources } from './client-overlay.mjs';

// Small executable fixture retaining the inspected compiler's relevant syntax.
const chart = 'function chart({performanceHistory:e,serverRunning:t=!0,maxMemoryGB:r}){let f=e[e.length-1],p=f?.pzMemMB??f?.memoryMB??0,y=f?.hostSwapUsedGB,b=f?.hostSwapTotalGB;const d=x=>x;if(!f)return{latest:f};const rows=[{key:`pzMem`,label:d(`pzMemory`),value:[p,r]},{key:`cpu`,label:d(`hostCpu`)},{key:`hostMem`,label:d(`hostMemory`)},{key:`swap`,label:d(`hostSwap`)}];return{latest:f,rows:rows.map(e=>({...e,children:e.label})),swap:[y,b]}}';
const point = 'pzMemMB:e.pzMemUsed?Math.round(e.pzMemUsed/1048576):void 0';
const dashboard = `const history=e=>({${point}}),socket=e=>({${point}});const cpu=e=>({headline:t(\`verdict.hostCpu\`,{percent:e})}),mem=e=>({headline:t(\`verdict.hostMemory\`,{percent:Math.round(e*100)})});`;
const scope = 'game-container-v1';

async function fixture(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'panel-client-overlay-'));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  await fs.mkdir(path.join(dir, 'assets'));
  await fs.writeFile(path.join(dir, 'index.html'), '<script type="module" src="/assets/index-HASH.js"></script><link rel="modulepreload" href="/assets/api-HASH.js">');
  await fs.writeFile(path.join(dir, 'assets', 'index-HASH.js'), 'import "./api-HASH.js"; import("./Dashboard-HASH.js");');
  await fs.writeFile(path.join(dir, 'assets', 'api-HASH.js'), 'export const version="1.4.0";');
  await fs.writeFile(path.join(dir, 'assets', 'Dashboard-HASH.js'), 'import "./api-HASH.js";import "./DashboardPerformanceCharts-HASH.js";' + dashboard);
  await fs.writeFile(path.join(dir, 'assets', 'DashboardPerformanceCharts-HASH.js'), chart);
  await fs.writeFile(path.join(dir, 'build-info.json'), '{"panelVersion":"1.4.0","buildSha":"official"}');
  return dir;
}

test('real numeric JVM max overrides profile; CPU/RAM labels have container meaning', () => {
  const transformed = transformClientSources(chart, dashboard);
  const context = vm.createContext({});
  vm.runInContext(transformed.chart + ';this.render=chart;', context);
  const sample = { telemetryScope: scope, cpuPercent: 25, hostMemUsedGB: 6, hostMemTotalGB: 10,
    pzMemMB: 2048, jvmHeapMaxGB: 8, gameCpuLimitCores: 2.2, memoryMB: 9999,
    hostSwapUsedGB: 5, hostSwapTotalGB: 6 };
  const result = context.render({ performanceHistory: [sample], maxMemoryGB: 16 });
  assert.deepEqual(Array.from(result.rows[0].value), [2048, 8]);
  assert.equal(result.rows[0].children, 'JVM heap');
  assert.equal(result.rows[1].children, 'Game CPU');
  assert.match(result.rows[1].title, /2\.2 vCPU limit/);
  assert.equal(result.rows[2].children, 'Game RAM');
  assert.match(result.rows[2].title, /working set.*10 GiB limit/);
  assert.deepEqual(Array.from(result.swap), [undefined, undefined]);
  assert.equal(context.render({ performanceHistory: [{ ...sample, telemetryScope: undefined }] }).latest, undefined);
  assert.equal(context.render({ performanceHistory: [{ ...sample, jvmHeapMaxGB: undefined }] }).latest, undefined);
});

test('both history and socket carry scope and max; valid zero heap never becomes panel heap', () => {
  const transformed = transformClientSources(chart, dashboard);
  const context = vm.createContext({});
  vm.runInContext(transformed.dashboard + ';this.mapHistory=history;this.mapSocket=socket;this.verdictCpu=cpu;', context);
  for (const map of [context.mapHistory, context.mapSocket]) {
    const output = map({ telemetryScope: scope, jvmHeapMaxBytes: 8 * 2 ** 30, pzMemUsed: 0, gameCpuLimitCores: 2.2 });
    assert.equal(output.pzMemMB, 0);
    assert.equal(output.jvmHeapMaxGB, 8);
    assert.equal(output.telemetryScope, scope);
    assert.equal(output.gameCpuLimitCores, 2.2);
    assert.equal(map({ pzMemUsed: null }).pzMemMB, undefined);
  }
  assert.equal(context.verdictCpu(95).headline, 'Game CPU uses 95% of its CPU limit');
});

test('every JS URL is cache-busted, originals/provenance stay intact, repeated startup is stable', async t => {
  const dir = await fixture(t);
  const original = await fs.readFile(path.join(dir, 'index.html'), 'utf8');
  const result = await applyClientOverlay({ distDir: dir });
  assert.equal(result.verified, true);
  assert.equal(result.assetCount, 4);
  const index = await fs.readFile(path.join(dir, 'index.html'), 'utf8');
  assert.notEqual(index, original);
  assert.match(index, /index-HASH-pzscope-[a-f0-9]{16}\.js/);
  assert.match(index, /api-HASH-pzscope-[a-f0-9]{16}\.js/);
  const names = await fs.readdir(path.join(dir, 'assets'));
  assert.equal(names.length, 8);
  for (const name of names.filter(name => name.includes('-pzscope-'))) {
    const text = await fs.readFile(path.join(dir, 'assets', name), 'utf8');
    assert.doesNotMatch(text, /["']\.\/(?:api|Dashboard|DashboardPerformanceCharts)-HASH\.js["']/);
  }
  assert.equal(await fs.readFile(path.join(dir, 'assets', 'DashboardPerformanceCharts-HASH.js'), 'utf8'), chart);
  assert.equal(await fs.readFile(path.join(dir, 'build-info.json'), 'utf8'), '{"panelVersion":"1.4.0","buildSha":"official"}');
  assert.deepEqual(await applyClientOverlay({ distDir: dir }), result);
});

test('upstream anchor drift refuses before changing index or adding any asset', async t => {
  const dir = await fixture(t);
  const original = await fs.readFile(path.join(dir, 'index.html'), 'utf8');
  await fs.writeFile(path.join(dir, 'assets', 'Dashboard-HASH.js'), dashboard.replace('verdict.hostMemory', 'verdict.changed'));
  await assert.rejects(applyClientOverlay({ distDir: dir }), /memory_verdict_anchor_mismatch/);
  assert.equal(await fs.readFile(path.join(dir, 'index.html'), 'utf8'), original);
  assert.equal((await fs.readdir(path.join(dir, 'assets'))).length, 4);
});

test('cannot accept ambiguous chart chunk or corrupted already-installed output', async t => {
  const dir = await fixture(t);
  await fs.writeFile(path.join(dir, 'assets', 'DashboardPerformanceCharts-SECOND.js'), chart);
  await assert.rejects(applyClientOverlay({ distDir: dir }), /component_inventory/);
  await fs.unlink(path.join(dir, 'assets', 'DashboardPerformanceCharts-SECOND.js'));
  await applyClientOverlay({ distDir: dir });
  const changed = (await fs.readdir(path.join(dir, 'assets'))).find(n => n.startsWith('DashboardPerformanceCharts-') && n.includes('-pzscope-'));
  await fs.writeFile(path.join(dir, 'assets', changed), 'corrupted');
  await assert.rejects(applyClientOverlay({ distDir: dir }), /installed_asset_changed/);
});

test('interrupted switch safely resumes from durable manifest and checked output hashes', async t => {
  const dir = await fixture(t);
  const original = await fs.readFile(path.join(dir, 'index.html'), 'utf8');
  const result = await applyClientOverlay({ distDir: dir });
  const installed = await fs.readFile(path.join(dir, 'index.html'), 'utf8');
  await fs.writeFile(path.join(dir, 'index.html'), original);
  assert.deepEqual(await applyClientOverlay({ distDir: dir }), result);
  assert.equal(await fs.readFile(path.join(dir, 'index.html'), 'utf8'), installed);
});

test('asset symlinks and unsupported scope fail closed', async t => {
  const dir = await fixture(t);
  await assert.rejects(applyClientOverlay({ distDir: dir, scope: 'host' }), /unsupported_scope/);
  await fs.symlink(path.join(dir, 'assets', 'api-HASH.js'), path.join(dir, 'assets', 'linked-HASH.js'));
  await assert.rejects(applyClientOverlay({ distDir: dir }), /file_type_or_size/);
});
