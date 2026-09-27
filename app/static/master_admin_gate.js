(() => {
  const KEY = 'peopleSystemMasterAdminUnlocked';
  if (sessionStorage.getItem(KEY) === 'true' && sessionStorage.getItem('peopleSystemAdminPin')) return;

  const showInlineGate = (message = '') => {
    document.body.innerHTML = `
      <main style="font-family:Georgia,serif;max-width:440px;margin:72px auto;padding:24px">
        <h1 style="margin:0 0 10px">Master locked</h1>
        <p style="margin:0 0 16px;color:#5f6f79">Enter the Master PIN to continue. These pages can change imported data, records, or system settings.</p>
        ${message ? `<p style="color:#9f3a30">${message}</p>` : ''}
        <form id="master-inline-gate" style="display:grid;gap:12px">
          <input id="master-inline-pin" type="password" autocomplete="current-password" placeholder="Master PIN" style="font:inherit;padding:12px;border:1px solid #c9d5dc;border-radius:8px">
          <button type="submit" style="font:inherit;padding:12px;border:0;border-radius:8px;background:#1f6f8b;color:white;cursor:pointer">Unlock</button>
        </form>
        <p style="margin-top:16px;display:flex;gap:12px;align-items:center"><a href="/">Return to viewer</a><a class="button-link" href="/help.html" aria-label="Help" title="Help" style="width:28px;min-width:28px;height:28px;min-height:28px;padding:0;border-radius:50%;border:1px solid rgba(80,88,96,.14);background:rgba(255,255,255,.18);color:rgba(83,92,101,.68);text-decoration:none;display:inline-flex;align-items:center;justify-content:center;font-weight:700">?</a></p>
      </main>
    `;
    document.documentElement.style.visibility = 'visible';
    const form = document.getElementById('master-inline-gate');
    const input = document.getElementById('master-inline-pin');
    input?.focus();
    form?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const value = input.value.trim();
      const response = await fetch('/api/admin/master-check', { method: 'POST', headers: { 'X-Admin-Pin': value } });
      if (response.ok) {
        sessionStorage.setItem(KEY, 'true');
        sessionStorage.setItem('peopleSystemAdminUnlocked', 'true');
        sessionStorage.setItem('peopleSystemAdminPin', value);
        window.location.reload();
        return;
      }
      showInlineGate('That Master PIN was not accepted.');
    });
  };

  document.documentElement.style.visibility = 'hidden';
  window.addEventListener('DOMContentLoaded', () => showInlineGate());
})();
