/* Shared runtime: theme toggle, nav state, status line, formatters,
   countdown engine. Every page loads this. */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmtGBP = v => (v == null || isNaN(v)) ? '—'
  : '£' + Number(v).toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtGBP0 = v => (v == null || isNaN(v)) ? '—'
  : '£' + Math.round(Number(v)).toLocaleString('en-GB');

const fmtCap = gb => gb == null ? '—'
  : gb >= 1000 ? (gb / 1000).toFixed(gb % 1000 === 0 ? 0 : 1) + 'TB' : gb + 'GB';

const fmtDate = iso => {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? '—' : d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
};
const fmtDateTime = iso => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' +
         d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
};
const timeAgo = iso => {
  if (!iso) return '';
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  if (mins < 48 * 60) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
};

const safeUrl = url => {
  try {
    const p = new URL(url);
    return (p.protocol === 'https:' || p.protocol === 'http:') ? esc(url) : '#';
  } catch { return '#'; }
};

/* ── theme ── */
$('#theme-btn')?.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('pcd-theme', next);
});

/* ── nav highlight (top links + mobile tab bar) ── */
(() => {
  const page = document.body.dataset.page;
  $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.nav === page));
  $$('#tabbar a').forEach(a => a.classList.toggle('active', a.dataset.tab === page));
})();

/* ── nav badges + status line ── */
async function refreshChrome() {
  try {
    const [counts, stats] = await Promise.all([
      fetch('/api/deal-counts?window=2&min_discount=20').then(r => r.json()),
      fetch('/api/stats').then(r => r.json()),
    ]);
    if (counts.status === 'ok') {
      // One "Deals" badge = total live deals across every category.
      const total = Object.values(counts.counts).reduce((a, b) => a + b, 0);
      $$('sup[data-count]').forEach(s => { s.textContent = total > 0 ? total : ''; });
    }
    const line = $('#status-line'), dot = $('#live-dot');
    if (stats.active_listings != null && line) {
      const total = (stats.active_listings + stats.sold_listings).toLocaleString('en-GB');
      line.textContent = `${total} listings · scraped ${timeAgo(stats.last_scrape_at)}`;
      const ageMin = stats.last_scrape_at
        ? (Date.now() - new Date(stats.last_scrape_at).getTime()) / 60000 : Infinity;
      dot.className = 'dot ' + (ageMin < 90 ? 'ok' : 'stale');
      dot.title = 'last full scrape: ' + fmtDateTime(stats.last_scrape_at);
    }
  } catch { /* offline / DB down — chrome stays quiet */ }
}
refreshChrome();
setInterval(refreshChrome, 5 * 60 * 1000);

/* ── account chip: who's signed in (links to settings), or a sign-in
      link for guests browsing read-only ── */
(async () => {
  const chip = $('#acct-chip');
  if (!chip || document.body.dataset.page === 'login') return;
  try {
    const data = await fetch('/api/me').then(r => r.json());
    if (data.user) {
      chip.textContent = data.user.name;
      chip.title = data.user.admin ? 'signed in (admin) — account settings' : 'signed in — account settings';
      chip.style.display = '';
    } else if (!data.bootstrap) {
      chip.textContent = 'Sign in';
      chip.href = '/login';
      chip.title = 'browsing as guest (read-only) — sign in to manage alerts and settings';
      chip.style.display = '';
    }
  } catch { /* offline — chip stays hidden */ }
})();

/* ── countdown engine: any element with data-ends-ms ticks 1/s ── */
function fmtCountdown(sec) {
  if (sec >= 86400) return Math.floor(sec / 86400) + 'd ' + Math.floor((sec % 86400) / 3600) + 'h';
  if (sec >= 3600) return Math.floor(sec / 3600) + 'h ' + Math.floor((sec % 3600) / 60) + 'm';
  if (sec >= 60) return Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
  return sec + 's';
}
setInterval(() => {
  $$('[data-ends-ms]').forEach(el => {
    const diff = Math.floor((Number(el.dataset.endsMs) - Date.now()) / 1000);
    if (diff <= 0) { el.textContent = 'ended'; el.classList.remove('urgent'); return; }
    el.textContent = fmtCountdown(diff);
    el.classList.toggle('urgent', diff < 600);
  });
}, 1000);

const endsAttr = iso => {
  if (!iso) return '';
  const ms = new Date(iso).getTime();
  return isNaN(ms) ? '' : `data-ends-ms="${ms}"`;
};

/* ── context-aware filters (shared by Prices + BIN) ──
   Each entry: dropdown options + a row predicate. Options are [value, label]
   pairs; '' = any. Rows carry the same category attribute fields on both
   pages (guide groups and BIN deal rows both come from the category tables). */
const CTX_FILTERS = {
  gpu: [
    { key: 'series', label: 'Series',
      opts: [['RTX', 'RTX'], ['GTX', 'GTX'], ['RX', 'Radeon RX'], ['ARC', 'Intel Arc']],
      match: (r, v) => (r.Model || '').toUpperCase().startsWith(v) },
  ],
  cpu: [
    { key: 'family', label: 'Family',
      opts: [['i3', 'Core i3'], ['i5', 'Core i5'], ['i7', 'Core i7'], ['i9', 'Core i9'],
             ['ryzen 3', 'Ryzen 3'], ['ryzen 5', 'Ryzen 5'], ['ryzen 7', 'Ryzen 7'],
             ['ryzen 9', 'Ryzen 9'], ['xeon', 'Xeon']],
      match: (r, v) => (r.Model || '').toLowerCase().includes(v) },
  ],
  hdd: [
    { key: 'iface', label: 'Interface', opts: [['SATA', 'SATA'], ['SAS', 'SAS']],
      match: (r, v) => (r.Interface || 'SATA') === v },
    { key: 'type', label: 'Type', opts: [['Internal', 'Internal'], ['External', 'External']],
      match: (r, v) => (r.DriveType || 'Internal') === v },
    { key: 'mincap', label: 'Min size',
      opts: [['1000', '1TB+'], ['4000', '4TB+'], ['8000', '8TB+'], ['12000', '12TB+']],
      match: (r, v) => Number(r.CapacityGB) >= Number(v) },
  ],
  ssd: [
    { key: 'iface', label: 'Interface', opts: [['NVMe', 'NVMe'], ['SATA', 'SATA'], ['USB', 'USB / portable']],
      match: (r, v) => (r.Interface || '') === v },
    { key: 'mincap', label: 'Min size',
      opts: [['500', '500GB+'], ['1000', '1TB+'], ['2000', '2TB+'], ['4000', '4TB+']],
      match: (r, v) => Number(r.CapacityGB) >= Number(v) },
  ],
  ram: [
    { key: 'type', label: 'Type', opts: [['DDR3', 'DDR3'], ['DDR4', 'DDR4'], ['DDR5', 'DDR5']],
      match: (r, v) => (r.Type || '') === v },
    { key: 'ff', label: 'Form', opts: [['DIMM', 'Desktop (DIMM)'], ['SODIMM', 'Laptop (SODIMM)']],
      match: (r, v) => (r.FormFactor || 'DIMM') === v },
    { key: 'kit', label: 'Kit',
      opts: [['1x', 'single stick'], ['2x', '2-stick kit'], ['4x', '4-stick kit'], ['?', 'unstated']],
      match: (r, v) => v === '?' ? !r.KitConfig : (r.KitConfig || '').toLowerCase().startsWith(v) },
    { key: 'mincap', label: 'Min size',
      opts: [['8', '8GB+'], ['16', '16GB+'], ['32', '32GB+'], ['64', '64GB+']],
      match: (r, v) => Number(r.CapacityGB) >= Number(v) },
  ],
};

/* Render the per-type filter dropdowns into `container` and wire them to
   mutate `ctx` + fire onChange. No filters for a category → container hidden. */
function renderCtxFilters(container, cat, ctx, onChange) {
  const defs = CTX_FILTERS[cat];
  if (!container) return;
  if (!defs) { container.style.display = 'none'; container.innerHTML = ''; return; }
  container.style.display = '';
  container.innerHTML = defs.map(f => `
    <label>${f.label}
      <select data-ctx="${f.key}">
        <option value="">any</option>
        ${f.opts.map(([v, l]) => `<option value="${esc(v)}"${ctx[f.key] === v ? ' selected' : ''}>${esc(l)}</option>`).join('')}
      </select>
    </label>`).join('');
  $$('select', container).forEach(sel => sel.addEventListener('change', () => {
    if (sel.value) ctx[sel.dataset.ctx] = sel.value; else delete ctx[sel.dataset.ctx];
    onChange();
  }));
}

/* Apply the active `ctx` selections to a row list for category `cat`. */
function filterByCtx(rows, cat, ctx) {
  for (const f of (CTX_FILTERS[cat] || []))
    if (ctx[f.key]) rows = rows.filter(r => f.match(r, ctx[f.key]));
  return rows;
}

/* ── shared model-page link builder ── */
function modelHref(cat, row) {
  const p = new URLSearchParams();
  if (cat === 'gpu' || cat === 'cpu') p.set('Model', row.Model ?? '');
  else if (cat === 'ram') {
    p.set('Type', row.Type ?? ''); p.set('CapacityGB', row.CapacityGB ?? '');
    p.set('FormFactor', row.FormFactor ?? '');
    p.set('KitConfig', row.KitConfig ?? '');
  } else {
    p.set('CapacityGB', row.CapacityGB ?? ''); p.set('Interface', row.Interface ?? '');
    p.set('DriveType', row.DriveType ?? '');
  }
  return `/model/${cat}?${p.toString()}`;
}

function groupLabel(cat, g) {
  if (cat === 'gpu' || cat === 'cpu') return g.Model || '—';
  if (cat === 'ram')
    return `${g.CapacityGB}GB ${g.Type || ''}${g.FormFactor === 'SODIMM' ? ' SODIMM' : ''}${g.KitConfig ? ' (' + g.KitConfig + ')' : ''}`.trim();
  const kind = cat === 'ssd' ? ' SSD' : '';
  return `${fmtCap(Number(g.CapacityGB))} ${g.Interface || ''}${kind}${g.DriveType === 'External' ? ' External' : ''}`.trim();
}

/* ── narrow-screen test for chart builders: a 720-unit viewBox squeezed
      into a phone renders 5px text; charts emit a smaller viewBox instead ── */
const isNarrowScreen = () => window.innerWidth < 520;

/* ── sort helper ── */
function sortRows(arr, col, asc) {
  if (!col) return arr;
  return [...arr].sort((a, b) => {
    const va = a[col], vb = b[col];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    const cmp = (typeof va === 'number' && typeof vb === 'number')
      ? va - vb : String(va).localeCompare(String(vb));
    return asc ? cmp : -cmp;
  });
}

/* ── sparkline: series [[minutesLeft, price, bids],...] → tiny svg ── */
function sparkline(series, w = 90, h = 22) {
  if (!series || series.length < 2) return '';
  const prices = series.map(p => p[1]);
  const lo = Math.min(...prices), hi = Math.max(...prices);
  const span = (hi - lo) || 1;
  const pts = series.map((p, i) =>
    `${(i / (series.length - 1) * (w - 4) + 2).toFixed(1)},${(h - 3 - (p[1] - lo) / span * (h - 6)).toFixed(1)}`
  ).join(' ');
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline points="${pts}"/></svg>`;
}

/* ── PWA ── */
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
