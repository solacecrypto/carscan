// Subscribe this browser to push notifications (needs HTTPS and a VAPID key from the server).
(function () {
  const btn = document.getElementById('push-enable');
  const status = document.getElementById('push-status');
  if (!btn) return;
  function say(t) { if (status) status.textContent = t; }
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) { say('This browser cannot do push notifications.'); btn.disabled = true; return; }
  function b64ToU8(b64) {
    const pad = '='.repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }
  async function current() {
    const reg = await navigator.serviceWorker.register('/sw.js');
    return { reg, sub: await reg.pushManager.getSubscription() };
  }
  async function refresh() {
    const { sub } = await current();
    btn.textContent = sub ? 'Turn off on this device' : 'Enable on this device';
    say(sub ? 'Push notifications are on for this device.' : (Notification.permission === 'denied' ? 'Blocked in the browser settings.' : 'Off on this device.'));
  }
  btn.addEventListener('click', async () => {
    const { reg, sub } = await current();
    const token = document.querySelector('meta[name=csrf]').content;
    if (sub) {
      await fetch('/push/unsubscribe', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token }, body: JSON.stringify({ endpoint: sub.endpoint }) });
      await sub.unsubscribe();
    } else {
      const key = await (await fetch('/push/key')).text();
      if (!key) { say('The server has no push keys yet (VAPID_* in .env).'); return; }
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') { await refresh(); return; }
      const fresh = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToU8(key) });
      await fetch('/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token }, body: JSON.stringify(fresh.toJSON()) });
    }
    await refresh();
  });
  refresh();
})();
