/* Settings: appearance (local), account + user admin (sessions), the user's
   own notification endpoint, and their subscriptions. Sections show/hide based
   on /api/me: bootstrap mode, signed-in user, admin. */

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
  $('#notify-card').style.display = me ? '' : 'none';
  $('#alerts-card').style.display = me ? '' : 'none';
  $('#users-card').style.display = (me && me.admin) ? '' : 'none';
  $('#bin-card').style.display = (bootstrap || (me && me.admin)) ? '' : 'none';
  if (me) $('#acct-who').textContent = `— signed in as ${me.name}${me.admin ? ' (admin)' : ''}`;
  if (me && me.admin) loadUsers();
  if (me) { loadEndpoint(); loadSubscriptions(); }
  if (bootstrap || (me && me.admin)) loadBinSettings();
}

/* ── the user's own notification endpoint ── */
async function loadEndpoint() {
  try {
    const data = await fetch('/api/my-endpoint').then(r => r.json());
    if (data.status !== 'ok') return;
    const e = data.endpoint;
    $('#ep-url').value = e.HaUrl || '';
    $('#ep-service').value = e.NotifyService || '';
    $('#ep-enabled').checked = e.NotifyEnabled !== false;
    $('#ep-token').placeholder = e.TokenSet ? 'saved — blank to keep' : 'long-lived access token';
  } catch { /* leave placeholders */ }
}

$('#ep-save')?.addEventListener('click', async () => {
  $('#ep-status').textContent = 'saving…';
  try {
    const res = await fetch('/api/my-endpoint', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ha_url: $('#ep-url').value,
        notify_service: $('#ep-service').value,
        ha_token: $('#ep-token').value,
        enabled: $('#ep-enabled').checked,
      }),
    });
    const data = await res.json();
    $('#ep-status').textContent = data.status === 'ok' ? 'saved ✓' : (data.message || 'error');
    if (data.status === 'ok') { $('#ep-token').value = ''; loadEndpoint(); }
  } catch { $('#ep-status').textContent = 'network error'; }
});

/* ── BIN watcher sweep settings (admin) — the per-user targeting lives in
      BIN subscriptions, created on /bin and listed above ── */
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
    if (!confirm('Delete this user (and their subscriptions)?')) return;
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

/* ── subscriptions (unified: category feeds + BIN watches + model price alerts) ── */
const _typeWord = { auction: 'auction', bin: 'BIN', any: 'any listing' };

function subTrigger(a) {
  if (a.Kind === 'discount_pct') return `new ${_typeWord[a.ListingType] || 'listing'} deal`;
  return a.Kind === 'median_price' ? 'median drops below' : 'listing available below';
}
function subThreshold(a) {
  return a.Kind === 'discount_pct' ? `${a.MinDiscount}% off` : fmtGBP(a.TargetPrice);
}
function subWhat(a) {
  const label = esc(a.Label || a.Category.toUpperCase());
  if (a.ScopeKind === 'group')
    return `<a href="/model/${a.Category}?${new URLSearchParams(a.GroupParams)}">${label}</a>`;
  const icon = a.Kind === 'discount_pct' && a.ListingType === 'bin' ? '🔔 ' : '';
  return `<span title="${a.ScopeKind === 'all' ? 'whole category' : 'filtered'}">${icon}${label}</span>`;
}

async function loadSubscriptions() {
  const data = await fetch('/api/subscriptions').then(r => r.json()).catch(() => ({}));
  const box = $('#alerts-list');
  if (data.status !== 'ok') { box.innerHTML = '<p class="help">Couldn’t load subscriptions.</p>'; return; }
  if (!data.subscriptions.length) {
    box.innerHTML = '<p class="help">No subscriptions yet — hit “Alert me” on a model page, or “Watch this” on the <a href="/bin">Buy It Now</a> page.</p>';
    return;
  }
  box.innerHTML = `<div class="tbl-wrap" style="box-shadow:none;border:none">
    <table class="tbl"><thead><tr><th>What</th><th class="m-hide">Trigger</th><th class="num">Threshold</th>
      <th>Last</th><th>On</th><th></th></tr></thead>
    <tbody>${data.subscriptions.map(a => `
      <tr data-aid="${a.ID}" data-kind="${a.Kind}">
        <td>${subWhat(a)}</td>
        <td class="dimcell m-hide">${subTrigger(a)}</td>
        <td class="num"><input class="al-thr" type="number" min="1" step="${a.Kind === 'discount_pct' ? '5' : '1'}"
            value="${a.Kind === 'discount_pct' ? a.MinDiscount : a.TargetPrice}" style="width:74px">${a.Kind === 'discount_pct' ? '%' : ''}</td>
        <td class="dimcell">${a.LastFiredAt ? timeAgo(a.LastFiredAt) : 'never'}</td>
        <td><input class="al-en" type="checkbox"${a.Enabled ? ' checked' : ''}></td>
        <td><button class="btn-ghost al-save" style="padding:2px 8px;font-size:12px">save</button>
            <button class="btn-ghost btn-danger al-del" style="padding:2px 8px;font-size:12px">×</button></td>
      </tr>`).join('')}</tbody></table></div>
    <span class="sub" id="al-edit-status"></span>`;
  $$('#alerts-list tr[data-aid]').forEach(tr => {
    const aid = tr.dataset.aid, kind = tr.dataset.kind;
    $('.al-save', tr).onclick = async () => {
      const body = { enabled: $('.al-en', tr).checked };
      const thr = parseFloat($('.al-thr', tr).value);
      if (kind === 'discount_pct') body.min_discount = thr; else body.target_price = thr;
      const res = await fetch(`/api/subscriptions/${aid}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) });
      const d = await res.json();
      $('#al-edit-status').textContent = d.status === 'ok' ? 'saved ✓' : (d.message || 'error');
    };
    $('.al-del', tr).onclick = async () => {
      if (!confirm('Delete this subscription?')) return;
      await fetch(`/api/subscriptions/${aid}`, { method: 'DELETE' });
      loadSubscriptions();
    };
  });
}

loadMe();
