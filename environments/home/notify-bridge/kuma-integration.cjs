// Run inside the pinned Kuma container; input includes credentials and key.
const fs = require('node:fs');
const { io } = require('/app/node_modules/socket.io-client');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const socket = io('http://127.0.0.1:3001', { transports: ['websocket'], reconnection: false });
let monitors = {}, notifications = [];
socket.on('monitorList', x => { monitors = x; });
socket.on('notificationList', x => { notifications = x; });
const call = (event, ...args) => new Promise((resolve, reject) => {
  socket.timeout(20000).emit(event, ...args, (error, result) => {
    if (error || result?.ok === false) reject(new Error('Kuma operation failed: ' + event));
    else resolve(result);
  });
});
(async () => {
  await new Promise((resolve, reject) => { socket.once('info', resolve); socket.once('connect_error', reject); });
  await call('login', { ...input.credentials, token: '' });
  await call('getMonitorList');
  const existing = notifications.find(n => n.name === 'Notify Bridge');
  const definition = {
    name: 'Notify Bridge', type: 'webhook', active: true, isDefault: false,
    webhookURL: 'http://notify-bridge.notifications.svc.cluster.local:8080/webhooks/kuma',
    httpMethod: 'post', webhookContentType: 'json',
    webhookAdditionalHeaders: JSON.stringify({ Authorization: 'Bearer ' + input.key })
  };
  const saved = await call('addNotification', definition, existing?.id || null);
  let attached = 0;
  for (const monitor of Object.values(monitors)) {
    if (!monitor.active) continue;
    const current = (await call('getMonitor', monitor.id)).monitor;
    if (current.notificationIDList?.[saved.id]) continue;
    await call('editMonitor', { ...current, notificationIDList: { ...current.notificationIDList, [saved.id]: true } });
    attached++;
  }
  await call('testNotification', definition);
  console.log(JSON.stringify({ notification: saved.id, attached, receiverTest: 'accepted', phoneDelivery: 'not-proven-by-this-test' }));
})().catch(() => { console.error('Kuma integration failed'); process.exitCode = 1; }).finally(() => socket.disconnect());
