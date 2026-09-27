(() => {
  const KEY = 'peopleSystemEditUnlocked';
  if (
    sessionStorage.getItem('peopleSystemAdminPin')
    && (sessionStorage.getItem(KEY) === 'true' || sessionStorage.getItem('peopleSystemMasterAdminUnlocked') === 'true')
  ) return;

  const showInlineGate = (message = '') => {
    document.body.innerHTML = `
      <main style="font-family:Georgia,serif;max-width:420px;margin:72px auto;padding:24px">
        <h1 style="margin:0 0 10px">Edit locked</h1>
        <p style="margin:0 0 16px;color:#5f6f79">Enter the Edit PIN to continue.</p>
        ${message ? `<p style="color:#9f3a30">${message}</p>` : ''}
        <form id="admin-inline-gate" style="display:grid;gap:12px">
          <input id="admin-inline-pin" type="password" autocomplete="current-password" placeholder="Edit PIN" style="font:inherit;padding:12px;border:1px solid #c9d5dc;border-radius:8px">
          <button type="submit" style="font:inherit;padding:12px;border:0;border-radius:8px;background:#1f6f8b;color:white;cursor:pointer">Unlock</button>
        </form>
        <p style="margin-top:16px;display:flex;gap:12px;align-items:center"><a href="/">Return to viewer</a><a class="button-link" href="/help.html" aria-label="Help" title="Help" style="width:28px;min-width:28px;height:28px;min-height:28px;padding:0;border-radius:50%;border:1px solid rgba(80,88,96,.14);background:rgba(255,255,255,.18);color:rgba(83,92,101,.68);text-decoration:none;display:inline-flex;align-items:center;justify-content:center;font-weight:700">?</a></p>
      </main>
    `;
    document.documentElement.style.visibility = 'visible';
    const form = document.getElementById('admin-inline-gate');
    const input = document.getElementById('admin-inline-pin');
    input?.focus();
    form?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const value = input.value.trim();
      const response = await fetch('/api/admin/edit-check', { method: 'POST', headers: { 'X-Admin-Pin': value } });
      if (response.ok) {
        sessionStorage.setItem(KEY, 'true');
        sessionStorage.setItem('peopleSystemAdminPin', value);
        window.location.reload();
        return;
      }
      showInlineGate('That PIN was not accepted.');
    });
  };

  document.documentElement.style.visibility = 'hidden';
  window.addEventListener('DOMContentLoaded', () => {
    let attempts = 0;
    try {
      while (attempts < 3) {
        const value = window.prompt('Edit PIN required');
        if (value === null) break;
        const request = new XMLHttpRequest();
        request.open('POST', '/api/admin/edit-check', false);
        request.setRequestHeader('X-Admin-Pin', value);
        request.send();
        if (request.status >= 200 && request.status < 300) {
          sessionStorage.setItem(KEY, 'true');
          sessionStorage.setItem('peopleSystemAdminPin', value);
          document.documentElement.style.visibility = 'visible';
          return;
        }
        attempts += 1;
      }
    } catch (error) {
      showInlineGate();
      return;
    }
    showInlineGate('Refresh or enter the PIN below to continue.');
  });
})();
