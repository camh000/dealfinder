/* Settings: appearance (theme + density, stored locally) and notification
   recipients CRUD against /api/notify-settings. */

const NOTIFY_CATS = ['GPU', 'CPU', 'HDD', 'SSD', 'RAM'];
let recipients = [];

/* ── appearance ── */
(() => {
  const themePref = localStorage.getItem('pcd-theme') || 'system';
  $$('input[name="theme"]').forEach(r => {
    r.checked = r.value === themePref;
    r.addEventListener('change', () => {
      if (r.value === 'system') {
        localStorage.removeItem('pcd-theme');
        document.documentElement.dataset.theme =
          matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
      } else {
        localStorage.setItem('pcd-theme', r.value);
        document.documentElement.dataset.theme = r.value;
      }
    });
  });
  const density = localStorage.getItem('pcd-density') === 'compact' ? 'compact' : 'comfortable';
  $$('input[name="density"]').forEach(r => {
    r.checked = r.value === density;
    r.addEventListener('change', () => {
      localStorage.setItem('pcd-density', r.value);
      document.documentElement.dataset.density = r.value;
    });
  });
})();

/* ── recipients ── */
function card(r, idx) {
  const cats = r.Categories || [];
  return `<div class="card" data-idx="${idx}" style="background:var(--surface2)">
    <div class="form-grid">
      <div class="fg"><label>Name</label>
        <input type="text" class="rc-name" value="${esc(r.Name || '')}" placeholder="e.g. Dad"></div>
      <div class="fg"><label>Home Assistant URL</label>
        <input type="text" class="rc-url" value="${esc(r.HaUrl || '')}" placeholder="http://192.168.10.254:8123"></div>
      <div class="fg"><label>Notify service</label>
        <input type="text" class="rc-service" value="${esc(r.NotifyService || '')}" placeholder="mobile_app_phone"></div>
      <div class="fg"><label>HA token</label>
        <input type="password" class="rc-token" placeholder="${r.TokenSet ? 'saved — blank to keep' : 'long-lived access token'}"></div>
    </div>
    <div class="check-row">
      ${NOTIFY_CATS.map(c => `<label><input type="checkbox" class="rc-cat" value="${c}"
        ${cats.includes(c) ? 'checked' : ''}> ${c}</label>`).join('')}
      <label><input type="checkbox" class="rc-enabled" ${r.Enabled !== false ? 'checked' : ''}> Enabled</label>
      <span style="flex:1"></span>
      <button class="btn" data-save="${idx}">Save</button>
      <button class="btn-ghost btn-danger" data-del="${idx}">Delete</button>
      <span class="rc-status sub"></span>
    </div>
  </div>`;
}

function render() {
  const box = $('#recipients');
  box.innerHTML = recipients.length
    ? recipients.map((r, i) => card(r, i)).join('')
    : '<p class="help">No recipients yet — add one to get deal pushes.</p>';
  $$('#recipients [data-save]').forEach(b => b.onclick = () => save(Number(b.dataset.save)));
  $$('#recipients [data-del]').forEach(b => b.onclick = () => del(Number(b.dataset.del)));
}

async function load() {
  try {
    const res = await fetch('/api/notify-settings');
    const data = await res.json();
    recipients = data.recipients || [];
    render();
  } catch {
    $('#recipients').innerHTML = '<p class="help">Couldn’t load recipients.</p>';
  }
}

async function save(idx) {
  const el = $(`#recipients [data-idx="${idx}"]`);
  const status = $('.rc-status', el);
  const body = {
    id: recipients[idx].ID || undefined,
    name: $('.rc-name', el).value,
    ha_url: $('.rc-url', el).value,
    notify_service: $('.rc-service', el).value,
    ha_token: $('.rc-token', el).value,
    enabled: $('.rc-enabled', el).checked,
    categories: $$('.rc-cat', el).filter(c => c.checked).map(c => c.value),
  };
  status.textContent = 'saving…';
  try {
    const res = await fetch('/api/notify-settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    status.textContent = data.status === 'ok' ? 'saved ✓' : (data.message || 'error');
    if (data.status === 'ok') load();
  } catch { status.textContent = 'network error'; }
}

async function del(idx) {
  const r = recipients[idx];
  if (!r.ID) { recipients.splice(idx, 1); render(); return; }
  if (!confirm(`Delete recipient "${r.Name}"?`)) return;
  await fetch(`/api/notify-settings/${r.ID}`, { method: 'DELETE' });
  load();
}

$('#add-recipient').onclick = () => {
  recipients.push({ Name: '', HaUrl: '', NotifyService: '', Categories: [...NOTIFY_CATS], Enabled: true });
  render();
};

load();
