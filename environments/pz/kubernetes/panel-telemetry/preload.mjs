import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { registerHooks } from 'node:module';
import { transformIndex, transformDatabase } from './server-transform.mjs';
import { primeTelemetry } from './provider.mjs';
import { applyClientOverlay } from './client-overlay.mjs';

// NODE_OPTIONS is inherited by diagnostic Node commands and child utilities.
// Only the panel entrypoint receives the overlay. An incompatible future entry
// has no telemetry health marker and must be rejected by the updater gate.
if (process.argv[1] && path.resolve(process.argv[1]) === '/app/server/index.js') {
  try {
    if (typeof registerHooks !== 'function') throw new Error('node_hooks_unavailable');
    const providerUrl = new URL('./provider.mjs', import.meta.url).href;
    const indexPath = '/app/server/index.js';
    const databasePath = '/app/server/database/init.js';
    const transformed = new Map([
      [pathToFileURL(indexPath).href, transformIndex(fs.readFileSync(indexPath, 'utf8'), providerUrl)],
      [pathToFileURL(databasePath).href, transformDatabase(fs.readFileSync(databasePath, 'utf8'), providerUrl)],
    ]);
    await applyClientOverlay({ distDir: '/app/client/dist', scope: process.env.PZ_TELEMETRY_SCOPE });
    registerHooks({
      load(url, context, nextLoad) {
        if (transformed.has(url)) return { format: 'module', source: transformed.get(url), shortCircuit: true };
        return nextLoad(url, context);
      },
    });
    await primeTelemetry();
    console.info('[panel-telemetry] compatibility verified; game-scoped telemetry enabled');
  } catch {
    console.error('[panel-telemetry] startup compatibility or configuration check failed');
    process.exit(1);
  }
}
