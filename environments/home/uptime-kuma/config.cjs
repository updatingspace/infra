// Run inside the pinned Kuma container. Credentials arrive over stdin, never argv.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { isDeepStrictEqual } = require('node:util');
const { io } = require('/app/node_modules/socket.io-client');
const desired = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/config.json', 'utf8'));
const apply = process.argv.includes('--apply');
const credentials = JSON.parse(fs.readFileSync(0, 'utf8'));
assert.equal(desired.settings.disableAuth, false);
assert.equal(new Set(desired.monitors.map(m => m.name)).size, desired.monitors.length);
for (const m of desired.monitors) {
    if (m.type === 'http') {
        assert.equal(m.ignoreTls, false);
        const u = new URL(m.url);
        assert(['http:', 'https:'].includes(u.protocol) && !u.username && !u.password);
    } else {
        assert.equal(m.type, 'gamedig', 'Unsupported monitor type');
        assert.equal(m.game, 'minecraft');
        assert.equal(m.hostname, 'minecraft.minecraft.svc.cluster.local');
        assert.equal(m.port, 25565);
        assert.equal(m.gamedigGivenPortOnly, true);
    }
    assert(m.interval >= 20 && typeof m.active === 'boolean');
}
const socket = io('http://127.0.0.1:3001', { transports: ['websocket'], reconnection: false });
let monitors = {};
socket.on('monitorList', value => { monitors = value; });
const call = (event, ...args) => new Promise((resolve, reject) => {
    socket.timeout(20000).emit(event, ...args, (error, result) => {
        if (error || result?.ok === false) reject(new Error(event + ': ' + (error?.message || result.msg)));
        else resolve(result);
    });
});
const differences = (actual, expected) => Object.keys(expected).filter(key => {
    const value = typeof expected[key] === 'boolean' ? Boolean(actual[key]) : actual[key];
    return !isDeepStrictEqual(value, expected[key]);
});
(async () => {
    await new Promise((resolve, reject) => { socket.once('info', resolve); socket.once('connect_error', reject); });
    assert.equal(await call('needSetup'), false, 'Initialize/restore the administrator account separately');
    await call('login', { ...credentials, token: '' });
    await call('getMonitorList');
    let drift = 0;
    for (const definition of desired.monitors) {
        let matches = Object.values(monitors).filter(m => m.name === definition.name);
        assert(matches.length <= 1, 'Ambiguous monitor name: ' + definition.name);
        let existing = matches[0];
        if (!existing) {
            drift++;
            console.log(JSON.stringify({ monitor: definition.name, missing: true }));
            if (!apply) continue;
            const added = await call('add', {
                ...definition, method: 'GET', accepted_statuscodes: ['200-299'],
                resendInterval: 0, upsideDown: false, expiryNotification: true,
                domainExpiryNotification: false, notificationIDList: {},
                saveResponse: false, saveErrorResponse: false,
                kafkaProducerBrokers: [], kafkaProducerSaslOptions: {},
                rabbitmqNodes: [], conditions: []
            });
            await call('getMonitorList');
            existing = monitors[added.monitorID];
            assert(existing && existing.name === definition.name, 'Created monitor was not returned');
        }
        const current = (await call('getMonitor', existing.id)).monitor;
        const changed = differences(current, definition);
        if (!changed.length) continue;
        drift++;
        console.log(JSON.stringify({ monitor: definition.name, changed }));
        if (apply) {
            await call('editMonitor', { ...current, ...definition });
            if (Boolean(current.active) !== definition.active) await call(definition.active ? 'resumeMonitor' : 'pauseMonitor', existing.id);
        }
    }
    const settings = (await call('getSettings')).data;
    const changedSettings = differences(settings, desired.settings);
    if (changedSettings.length) {
        drift++;
        console.log(JSON.stringify({ settings: changedSettings }));
        if (apply) await call('setSettings', { ...settings, ...desired.settings }, null);
    }
    const slug = desired.statusPage.slug;
    const currentPage = (await call('getStatusPage', slug)).config;
    const response = await fetch('http://127.0.0.1:3001/api/status-page/' + slug);
    assert(response.ok);
    const page = await response.json();
    const nameById = Object.fromEntries(Object.values(monitors).map(m => [m.id, m.name]));
    const groups = page.publicGroupList.map(g => ({ name: g.name, monitors: g.monitorList.map(m => ({ name: nameById[m.id], sendUrl: Boolean(m.url) })) }));
    if (differences(currentPage, desired.statusPage).length || !isDeepStrictEqual(groups, desired.publicGroups)) {
        drift++;
        console.log(JSON.stringify({ statusPage: slug, changed: true }));
        if (apply) await call('saveStatusPage', slug, { ...currentPage, ...desired.statusPage }, currentPage.icon,
            desired.publicGroups.map(g => ({ name: g.name, monitorList: g.monitors.map(m => ({
                id: Object.values(monitors).find(x => x.name === m.name).id, sendUrl: m.sendUrl
            })) })));
    }
    console.log(JSON.stringify({ mode: apply ? 'apply' : 'check', monitors: desired.monitors.length, drift, notificationsAndCredentialsUntouched: true }));
    if (drift && !apply) process.exitCode = 1;
})().catch(e => { console.error(e.message); process.exitCode = 1; }).finally(() => socket.disconnect());
