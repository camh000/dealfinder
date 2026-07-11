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

/* ── nav highlight ── */
(() => {
  const page = document.body.dataset.page;
  $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.nav === page));
})();

/* ── nav badges + status line ── */
async function refreshChrome() {
  try {
    const [counts, stats] = await Promise.all([
      fetch('/api/deal-counts?window=2&min_discount=20').then(r => r.json()),
      fetch('/api/stats').then(r => r.json()),
    ]);
    if (counts.status === 'ok')
      $$('sup[data-count]').forEach(s => {
        const n = counts.counts[s.dataset.count];
        s.textContent = n > 0 ? n : '';
      });
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

/* ── account chip: shows who's signed in, links to settings ── */
(async () => {
  const chip = $('#acct-chip');
  if (!chip || document.body.dataset.page === 'login') return;
  try {
    const data = await fetch('/api/me').then(r => r.json());
    if (data.user) {
      chip.textContent = data.user.name;
      chip.title = data.user.admin ? 'signed in (admin) — account settings' : 'signed in — account settings';
      chip.style.display = '';
    }
  } catch { /* signed out or offline — chip stays hidden */ }
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
