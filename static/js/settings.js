/* Settings: appearance (local), account + user admin (sessions), price
   alerts, and notification recipients. Sections show/hide based on
   /api/me: bootstrap mode, signed-in user, admin. */

const NOTIFY_CATS = ['GPU', 'CPU', 'HDD', 'SSD', 'RAM'];
let recipients = [];
let me = null, bootstrap = false;

/* ── appearance (unchanged, per-browser) ── */
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

/* ── account sections ── */
async function loadMe() {
  try {
    const data = await fetch('/api/me').then(r => r.json());
    me = data.user; bootstrap = data.bootstrap;
  } catch { me = null; bootstrap = false; }
  const guest = !me && !bootstrap;
  $('#guest-card').style.display = guest ? '' : 'none';
  $('#bootstrap-card').style.display = bootstrap ? '' : 'none';
  $('#account-card').style.display = me ? '' : 'none';
  $('#alerts-card').style.display = (me || bootstrap) ? '' : 'none';
  $('#users-card').style.display = (me && me.admin) ? '' : 'none';
  $('#bin-card').style.display = (bootstrap || (me && me.admin)) ? '' : 'none';
  // recipients API is login-gated — a guest would just see an error
  $('#notify-card').style.display = guest ? 'none' : '';
  if (me) $('#acct-who').textContent = `— signed in as ${me.name}${me.admin ? ' (admin)' : ''}`;
  if (me && me.admin) loadUsers();
  if (me || bootstrap) loadAlerts();
  if (bootstrap || (me && me.admin)) loadBinSettings();
}

/* ── BIN watcher sweep settings (admin) — the per-user targeting lives in
      watches (bin_new alerts), created on /bin and listed above ── */
async function loadBinSettings() {
  try {
    const cfg = await fetch('/api/bin-settings').then(r => r.json());
    if (cfg.status !== 'ok') return;
    $('#bin-scan').value = cfg.scan_minutes;
    $('#bin-disc').value = cfg.min_discount;
    $('#bin-enabled').checked = cfg.enabled;
  } catch { /* card just keeps its placeholders */ }
}

$('#bin-save')?.addEventListener('click', async () => {
  $('#bin-status').textContent = '…';
  try {
    const res = await fetch('/api/bin-settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scan_minutes: Number($('#bin-scan').value),
        min_discount: Number($('#bin-disc').value),
        enabled: $('#bin-enabled').checked,
      }),
    });
    const data = await res.json();
    $('#bin-status').textContent = data.status === 'ok'
      ? 'saved ✓ — live within a minute' : (data.message || 'error');
  } catch { $('#bin-status').textContent = 'network error'; }
});

$('#boot-create')?.addEventListener('click', async () => {
  $('#boot-status').textContent = '…';
  const res = await fetch('/api/users', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: $('#boot-user').value, password: $('#boot-pass').value }),
  });
  const data = await res.json();
  $('#boot-status').textContent = data.status === 'ok' ? 'created — you are the admin' : (data.message || 'error');
  if (data.status === 'ok') setTimeout(() => location.reload(), 700);
});

$('#pw-save')?.addEventListener('click', async () => {
  const res = await fetch('/api/password', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: $('#pw-new').value }),
  });
  const data = await res.json();
  $('#pw-status').textContent = data.status === 'ok' ? 'changed ✓' : (data.message || 'error');
});

$('#logout-btn')?.addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST' });
  location.href = '/login';
});

/* ── users (admin) ── */
async function loadUsers() {
  const data = await fetch('/api/users').then(r => r.json());
  if (data.status !== 'ok') return;
  $('#users-list').innerHTML = `<div class="tbl-wrap" style="box-shadow:none;border:none">
    <table class="tbl"><thead><tr><th>User</th><th>Role</th><th>Created</th><th></th></tr></thead>
    <tbody>${data.users.map(u => `
      <tr><td>${esc(u.Username)}</td>
        <td class="dimcell">${u.IsAdmin ? 'admin' : 'user'}</td>
        <td class="dimcell">${fmtDate(u.CreatedAt)}</td>
        <td>${u.ID === me.id ? '<span class="dimcell">you</span>'
             : `<button class="btn-ghost btn-danger" data-del-user="${u.ID}" style="padding:2px 10px;font-size:12px">delete</button>`}</td>
      </tr>`).join('')}</tbody></table></div>`;
  $$('#users-list [data-del-user]').forEach(b => b.onclick = async () => {
    if (!confirm('Delete this user (and their alerts)?')) return;
    await fetch(`/api/users/${b.dataset.delUser}`, { method: 'DELETE' });
    loadUsers();
  });
}

$('#nu-create')?.addEventListener('click', async () => {
  $('#nu-status').textContent = '…';
  const res = await fetch('/api/users', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: $('#nu-name').value, password: $('#nu-pass').value,
                           is_admin: $('#nu-admin').checked }),
  });
  const data = await res.json();
  $('#nu-status').textContent = data.status === 'ok' ? 'added ✓' : (data.message || 'error');
  if (data.status === 'ok') { $('#nu-name').value = ''; $('#nu-pass').value = ''; loadUsers(); }
});

/* ── alerts & watches (unified: model price alerts + BIN watches) ── */
let alertRecipients = [];

function alertTrigger(a) {
  if (a.Kind === 'bin_new') return `new BIN ≥ ${a.MinDiscount}% off`;
  return a.Kind === 'median_below' ? 'median drops below' : 'listing available below';
}
function alertThreshold(a) {
  return a.Kind === 'bin_new' ? `${a.MinDiscount}% off` : fmtGBP(a.TargetPrice);
}
function alertLink(a) {
  const label = esc(a.Label || a.Category.toUpperCase());
  if (a.Kind === 'bin_new') return `<span title="BIN watch">🔔 ${label}</span>`;
  return `<a href="/model/${a.Category}?${new URLSearchParams(a.GroupParams)}">${label}</a>`;
}

async function loadAlerts() {
  const [data, recips] = await Promise.all([
    fetch('/api/alerts').then(r => r.json()),
    fetch('/api/notify-settings').then(r => r.json()).catch(() => ({})),
  ]);
  alertRecipients = (recips.recipients || []).filter(r => r.Enabled !== false);
  const box = $('#alerts-list');
  if (data.status !== 'ok') { box.innerHTML = '<p class="help">Couldn’t load alerts.</p>'; return; }
  if (!data.alerts.length) {
    box.innerHTML = '<p class="help">No alerts yet — hit “Alert me” on a model page, or “Watch this” on the <a href="/bin">Buy It Now</a> page.</p>';
    return;
  }
  const recOpts = (sel) => alertRecipients.map(r =>
    `<option value="${r.ID}"${r.ID === sel ? ' selected' : ''}>${esc(r.Name || 'recipient ' + r.ID)}</option>`).join('')
    || '<option value="">—</option>';
  box.innerHTML = `<div class="tbl-wrap" style="box-shadow:none;border:none">
    <table class="tbl"><thead><tr><th>What</th><th class="m-hide">Trigger</th><th class="num">Threshold</th>
      <th class="m-hide">Notifies</th><th>Last</th><th>On</th><th></th></tr></thead>
    <tbody>${data.alerts.map(a => `
      <tr data-aid="${a.ID}" data-kind="${a.Kind}">
        <td>${alertLink(a)}</td>
        <td class="dimcell m-hide">${alertTrigger(a)}</td>
        <td class="num"><input class="al-thr" type="number" min="1" step="${a.Kind === 'bin_new' ? '5' : '1'}"
            value="${a.Kind === 'bin_new' ? a.MinDiscount : a.TargetPrice}" style="width:74px">${a.Kind === 'bin_new' ? '%' : ''}</td>
        <td class="dimcell m-hide"><select class="al-rec">${recOpts(a.RecipientID)}</select></td>
        <td class="dimcell">${a.LastFiredAt ? timeAgo(a.LastFiredAt) : 'never'}</td>
        <td><input class="al-en" type="checkbox"${a.Enabled ? ' checked' : ''}></td>
        <td><button class="btn-ghost al-save" style="padding:2px 8px;font-size:12px">save</button>
            <button class="btn-ghost btn-danger al-del" style="padding:2px 8px;font-size:12px">×</button></td>
      </tr>`).join('')}</tbody></table></div>
    <span class="sub" id="al-edit-status"></span>`;
  $$('#alerts-list tr[data-aid]').forEach(tr => {
    const aid = tr.dataset.aid, kind = tr.dataset.kind;
    $('.al-save', tr).onclick = async () => {
      const body = { enabled: $('.al-en', tr).checked,
                     recipient_id: $('.al-rec', tr).value ? Number($('.al-rec', tr).value) : null };
      const thr = parseFloat($('.al-thr', tr).value);
      if (kind === 'bin_new') body.min_discount = thr; else body.target_price = thr;
      const res = await fetch(`/api/alerts/${aid}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) });
      const d = await res.json();
      $('#al-edit-status').textContent = d.status === 'ok' ? 'saved ✓' : (d.message || 'error');
    };
    $('.al-del', tr).onclick = async () => {
      if (!confirm('Delete this alert?')) return;
      await fetch(`/api/alerts/${aid}`, { method: 'DELETE' });
      loadAlerts();
    };
  });
}

/* ── recipients (unchanged behaviour) ── */
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

loadMe();
load();
