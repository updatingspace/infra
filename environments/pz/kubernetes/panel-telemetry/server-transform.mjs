function replaceOnce(source, before, after, name) {
  if (source.split(before).length !== 2) throw new Error(`telemetry_incompatible_${name}`);
  return source.replace(before, after);
}

export function transformIndex(source, providerUrl) {
  let result = source;
  result = replaceOnce(result,
    '      const hostMem = os.totalmem();\n      const hostMemFree = os.freemem();\n      const cpuUsage = getCpuUsage();',
    '      const telemetryServer = await getActiveServer().catch(() => null);\n'
      + '      const gameTelemetry = await sampleForServer(telemetryServer);\n'
      + '      if (!gameTelemetry) return;\n'
      + '      const hostMem = gameTelemetry.memoryLimitBytes;\n'
      + '      const hostMemFree = hostMem - gameTelemetry.memoryUsedBytes;\n'
      + '      const cpuUsage = gameTelemetry.cpuPercent;', 'sampling');
  result = replaceOnce(result, '      const pzMemBytes = await getPzProcessMemory();',
    '      const pzMemBytes = gameTelemetry.jvmHeapUsedBytes;', 'heap');
  result = replaceOnce(result, '      const swap = await getSwapSnapshot();',
    '      const swap = null; // No node swap is presented as game-container swap.', 'swap');
  result = replaceOnce(result, '      const activeServerForSnapshot = await getActiveServer().catch(() => null);',
    '      const activeServerForSnapshot = telemetryServer;', 'server_scope');
  result = replaceOnce(result, '        serverId: activeServerForSnapshot?.id ?? null,',
    '        serverId: activeServerForSnapshot?.id ?? null,\n'
      + '        telemetryScope: gameTelemetry.scope,\n'
      + '        telemetryPodUid: gameTelemetry.podUid,\n'
      + '        gameCpuLimitCores: gameTelemetry.cpuLimitCores,\n'
      + '        gameCpuUsageCores: gameTelemetry.cpuUsageCores,\n'
      + '        jvmHeapMaxBytes: gameTelemetry.jvmHeapMaxBytes,', 'snapshot');
  result = replaceOnce(result, '      await recordPerformanceSnapshot(snapshot);',
    '      const telemetryServerAfter = await getActiveServer().catch(() => null);\n'
      + '      if (String(telemetryServerAfter?.id) !== String(telemetryServer.id)) return;\n'
      + '      await recordPerformanceSnapshot(snapshot);', 'final_server_scope');
  result = replaceOnce(result, 'app.get("/api/health", (req, res) => {\n  res.json({\n    status: "ok",',
    'app.get("/api/health", (req, res) => {\n  res.json({\n    telemetry: telemetryHealth(),\n    status: "ok",', 'health');
  return `import { sampleForServer, telemetryHealth } from ${JSON.stringify(providerUrl)};\n${result}`;
}

export function transformDatabase(source, providerUrl) {
  if (source.split('export async function getPerformanceHistory(limit = 60, serverId = undefined) {').length !== 2) {
    throw new Error('telemetry_incompatible_history_function');
  }
  const result = replaceOnce(source, '  return source.slice(-safeLimit);',
    '  return filterTelemetryHistory(source).slice(-safeLimit);', 'history_filter');
  return `import { filterTelemetryHistory } from ${JSON.stringify(providerUrl)};\n${result}`;
}
