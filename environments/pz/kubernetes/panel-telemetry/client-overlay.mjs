/** Startup-only adapter for the official compiled client; no application data access. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';

const FORMAT = 1;
const SCOPE = 'game-container-v1';
const MARKER = '.pz-telemetry-client.json';
const SUFFIX = /-pzscope-[a-f0-9]{16}\.js$/;
const NAME = /^[A-Za-z0-9_-]+\.js$/;
const MAX_TOTAL = 32 * 1024 * 1024;
const hash = text => createHash('sha256').update(text).digest('hex');
const fail = reason => { throw new Error('client_overlay_' + reason); };

function replaceCount(text, expression, count, replacement, label) {
  const matches = [...text.matchAll(expression)];
  if (matches.length !== count) fail(label + '_anchor_mismatch');
  return text.replace(expression, replacement);
}

/** Deliberately recognizes the inspected v1.4.0 compiler structure, not arbitrary JS. */
export function transformClientSources(chartSource, dashboardSource, { scope = SCOPE } = {}) {
  if (scope !== SCOPE) fail('unsupported_scope');
  let chart = chartSource;
  const signature = /function ([$\w]+)\(\{performanceHistory:([$\w]+),serverRunning:([$\w]+)=!0,maxMemoryGB:([$\w]+)\}\)\{/g;
  const signatures = [...chart.matchAll(signature)];
  if (signatures.length !== 1) fail('chart_signature_anchor_mismatch');
  const [, , history, , maximum] = signatures[0];
  const latestMatch = [...chart.matchAll(/([$\w]+)=([$\w]+)\[\2\.length-1\],/g)];
  if (latestMatch.length !== 1 || latestMatch[0][2] !== history) fail('chart_latest_anchor_mismatch');
  const latest = latestMatch[0][1];
  // The backend also filters history. This independent guard prevents legacy
  // host samples being relabelled if the browser still receives old data.
  chart = chart.replace(signature, text => text
    + `${history}=${history}.filter(p=>p.telemetryScope===${JSON.stringify(scope)}&&Number.isFinite(p.cpuPercent)&&p.cpuPercent>=0&&Number.isFinite(p.hostMemUsedGB)&&p.hostMemUsedGB>=0&&Number.isFinite(p.hostMemTotalGB)&&p.hostMemTotalGB>0&&Number.isFinite(p.pzMemMB)&&p.pzMemMB>=0&&Number.isFinite(p.jvmHeapMaxGB)&&p.jvmHeapMaxGB>0&&Number.isFinite(p.gameCpuLimitCores)&&p.gameCpuLimitCores>0);`
    + `${maximum}=${history}.at(-1)?.jvmHeapMaxGB;`);
  chart = replaceCount(chart, /key:`pzMem`,label:([$\w]+)\(`pzMemory`\)/g, 1,
    'key:`pzMem`,label:`JVM heap`,title:`Game JVM heap / JVM maximum`', 'heap_label');
  chart = replaceCount(chart, /key:`cpu`,label:([$\w]+)\(`hostCpu`\)/g, 1,
    'key:`cpu`,label:`Game CPU`,title:`Game CPU (% of ${' + latest + '.gameCpuLimitCores} vCPU limit)`', 'cpu_label');
  chart = replaceCount(chart, /key:`hostMem`,label:([$\w]+)\(`hostMemory`\)/g, 1,
    'key:`hostMem`,label:`Game RAM`,title:`Game container working set / ${' + latest + '.hostMemTotalGB} GiB limit`', 'memory_label');
  chart = replaceCount(chart, /key:`swap`,label:([$\w]+)\(`hostSwap`\)/g, 1,
    'key:`swap`,label:`Game swap`,title:`Game swap is unavailable`', 'swap_label');
  // No host or made-up zero swap reading. Keep the original conditional JSX;
  // undefined values hide that row, even if a malformed snapshot supplies it.
  chart = replaceCount(chart, /([$\w]+)=([$\w]+)\?\.hostSwapUsedGB,([$\w]+)=\2\?\.hostSwapTotalGB/g, 1,
    (_text, used, _point, total) => `${used}=void 0,${total}=void 0`, 'swap_hidden');
  chart = replaceCount(chart, /children:([$\w]+)\.label\}/g, 1,
    (_text, metric) => `title:${metric}.title,children:${metric}.label}`, 'label_tooltip');
  chart = replaceCount(chart, /([$\w]+)\?\.pzMemMB\?\?\1\?\.memoryMB\?\?0/g, 1,
    (_text, point) => `${point}?.pzMemMB??0`, 'no_panel_heap_fallback');

  let dashboard = dashboardSource;
  dashboard = replaceCount(dashboard,
    /pzMemMB:([$\w]+)\.pzMemUsed\?Math\.round\(\1\.pzMemUsed\/1048576\):void 0/g, 2,
    (_text, sample) => `telemetryScope:${sample}.telemetryScope,gameCpuLimitCores:${sample}.gameCpuLimitCores,jvmHeapMaxGB:Number.isFinite(${sample}.jvmHeapMaxBytes)&&${sample}.jvmHeapMaxBytes>0?${sample}.jvmHeapMaxBytes/1073741824:void 0,pzMemMB:Number.isFinite(${sample}.pzMemUsed)&&${sample}.pzMemUsed>=0?Math.round(${sample}.pzMemUsed/1048576):void 0`,
    'history_and_socket_mapping');
  dashboard = replaceCount(dashboard, /headline:([$\w]+)\(`verdict\.hostCpu`,\{percent:([^{}]+)\}\)/g, 1,
    (_text, _translation, percent) => 'headline:`Game CPU uses ${' + percent + '}% of its CPU limit`', 'cpu_verdict');
  dashboard = replaceCount(dashboard, /headline:([$\w]+)\(`verdict\.hostMemory`,\{percent:([^{}]+)\}\)/g, 1,
    (_text, _translation, percent) => 'headline:`Game container RAM uses ${' + percent + '}% of its memory limit`', 'memory_verdict');
  return { chart, dashboard };
}

async function readFileBounded(file, maximum = MAX_TOTAL) {
  const stat = await fs.lstat(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > maximum) fail('file_type_or_size');
  return fs.readFile(file, 'utf8');
}

async function atomicWrite(file, contents, mode = 0o644) {
  const temporary = `${file}.tmp-${process.pid}`;
  const handle = await fs.open(temporary, 'wx', mode);
  try { await handle.writeFile(contents); await handle.sync(); }
  finally { await handle.close(); }
  await fs.rename(temporary, file);
}

async function validateCompleted(distDir, manifest, scope) {
  if (manifest.format !== FORMAT || manifest.scope !== scope
      || !Array.isArray(manifest.assets) || manifest.assets.length > 256
      || typeof manifest.index !== 'string' || manifest.index.length > 256 * 1024
      || manifest.indexHash !== hash(manifest.index)) fail('manifest_invalid');
  for (const item of manifest.assets) {
    if (!NAME.test(item.name) || !SUFFIX.test(item.name) || !/^[a-f0-9]{64}$/.test(item.hash)) fail('manifest_asset_invalid');
    if (hash(await readFileBounded(path.join(distDir, 'assets', item.name))) !== item.hash) fail('installed_asset_changed');
  }
  const index = await readFileBounded(path.join(distDir, 'index.html'), 256 * 1024);
  if (hash(index) === manifest.originalIndexHash) {
    // Recovery from interruption after all assets and the marker were durable
    // but before the single index switch. No partially-patched graph is served.
    await atomicWrite(path.join(distDir, 'index.html'), manifest.index);
  } else if (hash(index) !== manifest.indexHash) fail('installed_index_changed');
  return { scope, format: FORMAT, assetCount: manifest.assets.length, cacheKey: manifest.cacheKey, verified: true };
}

export async function applyClientOverlay({ distDir = '/app/client/dist', scope = SCOPE } = {}) {
  if (scope !== SCOPE) fail('unsupported_scope');
  for (const directory of [distDir, path.join(distDir, 'assets')]) {
    const info = await fs.lstat(directory);
    if (!info.isDirectory() || info.isSymbolicLink()) fail('directory_invalid');
  }
  const marker = path.join(distDir, MARKER);
  try {
    const previous = JSON.parse(await readFileBounded(marker, 1024 * 1024));
    return await validateCompleted(distDir, previous, scope);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const index = await readFileBounded(path.join(distDir, 'index.html'), 256 * 1024);
  if (/\bintegrity\s*=/.test(index) || index.includes('-pzscope-')) fail('index_incompatible');
  const names = (await fs.readdir(path.join(distDir, 'assets')))
    .filter(name => name.endsWith('.js') && !SUFFIX.test(name)).sort();
  if (!names.length || names.length > 256 || names.some(name => !NAME.test(name))) fail('asset_inventory_invalid');
  const sources = new Map();
  let total = 0;
  for (const name of names) {
    const source = await readFileBounded(path.join(distDir, 'assets', name));
    total += Buffer.byteLength(source);
    if (total > MAX_TOTAL) fail('total_asset_size');
    sources.set(name, source);
  }
  const chartNames = names.filter(name => /^DashboardPerformanceCharts-/.test(name));
  const dashboardNames = names.filter(name => /^Dashboard-/.test(name));
  if (chartNames.length !== 1 || dashboardNames.length !== 1) fail('component_inventory');
  const result = transformClientSources(sources.get(chartNames[0]), sources.get(dashboardNames[0]), { scope });
  sources.set(chartNames[0], result.chart);
  sources.set(dashboardNames[0], result.dashboard);
  // All JS graph URLs change together, including entry/preload chunks. This
  // avoids both 7-day cached original labels and duplicate module instances.
  const cacheKey = hash(JSON.stringify([FORMAT, scope, [...sources], index])).slice(0, 16);
  const renamed = new Map(names.map(name => [name, name.slice(0, -3) + '-pzscope-' + cacheKey + '.js']));
  const expression = new RegExp('(?<![A-Za-z0-9_-])(?:' + names.map(name => name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')(?![A-Za-z0-9_.-])', 'g');
  const rewrite = text => text.replace(expression, name => renamed.get(name));
  const updatedIndex = rewrite(index);
  if (updatedIndex === index || !/<script\b[^>]*\bsrc=["'][^"']+-pzscope-[a-f0-9]{16}\.js["']/.test(updatedIndex)) fail('entry_reference_missing');
  const outputs = [...sources].map(([name, source]) => ({ name: renamed.get(name), source: rewrite(source) }));
  const manifest = { format: FORMAT, scope, cacheKey, originalIndexHash: hash(index), indexHash: hash(updatedIndex), index: updatedIndex,
    assets: outputs.map(item => ({ name: item.name, hash: hash(item.source) })) };
  // All transformations/anchors are validated before any writes. Originals
  // remain intact, and index.html is switched only after every copy is ready.
  for (const item of outputs) {
    const file = path.join(distDir, 'assets', item.name);
    try {
      if (await readFileBounded(file) !== item.source) fail('output_collision');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      await atomicWrite(file, item.source);
    }
  }
  await atomicWrite(marker, JSON.stringify(manifest), 0o600);
  await atomicWrite(path.join(distDir, 'index.html'), updatedIndex);
  return validateCompleted(distDir, manifest, scope);
}
