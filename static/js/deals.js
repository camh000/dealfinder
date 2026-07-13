/* Deals page: one feed across every category. Fetch all active auction deals,
   then filter client-side by category chip + shared context filters (the same
   toolbar as Prices and BIN) + min discount / saving / sort. Rows carry a
   _cat tag so each renders with its own category's identity line. */

const LS_KEY = 'pcd-deal-filters';

let allDeals = [], allBundles = [], cat = 'all', ctx = {};
const saved = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
if (['all', 'gpu', 'cpu', 'hdd', 'ssd', 'ram'].includes(window.PRESELECT_CAT))
  cat = window.PRESELECT_CAT;
$('#f-window').value = saved.window || '2';
$('#f-disc').value = saved.disc ?? 20;
$('#f-save').value = saved.save ?? 0;
$('#f-sort').value = saved.sort || 'DealScore';

function persist() {
  localStorage.setItem(LS_KEY, JSON.stringify({
    window: $('#f-window').value, disc: $('#f-disc').value,
    save: $('#f-save').value, sort: $('#f-sort').value,
  }));
}

/* ── identity line, per row's own category ── */
function identity(d) {
  const c = d._cat;
  const chips = [`<span class="chip">${c.toUpperCase()}</span>`];
  const lot = Number(d.Quantity) > 1;
  if (lot) chips.push(`<span class="chip hot">×${d.Quantity} LOT</span>`);
  if (d.SurfacedAt && Date.now() - new Date(d.SurfacedAt).getTime() < 3600e3)
    chips.push('<span class="chip new">NEW</span>');
  let title = '', subs = [];
  if (c === 'gpu') {
    title = d.Model || '—';
    if (d.Brand) chips.push(`<span class="chip">${esc(d.Brand)}</span>`);
    if (d.VRAM && !(d.Model || '').includes('GB')) chips.push(`<span class="chip">${d.VRAM}GB</span>`);
  } else if (c === 'cpu') {
    title = d.Model || '—';
    if (d.Socket) chips.push(`<span class="chip">${esc(d.Socket)}</span>`);
    if (d.Cores) chips.push(`<span class="chip">${d.Cores} cores</span>`);
  } else if (c === 'ram') {
    title = `${d.CapacityGB}GB ${d.Type || ''}`.trim();
    if (d.KitConfig) chips.push(`<span class="chip">${esc(d.KitConfig)}</span>`);
    if (d.FormFactor === 'SODIMM') chips.push('<span class="chip">SODIMM</span>');
    if (d.Brand) chips.push(`<span class="chip">${esc(d.Brand)}</span>`);
    if (d.Speed) chips.push(`<span class="chip">${d.Speed}MHz</span>`);
  } else if (c === 'mobo') {
    title = `${d.Chipset || '—'}${d.FormFactor && d.FormFactor !== 'ATX' ? ' ' + d.FormFactor : ''}`;
    if (d.Socket) chips.push(`<span class="chip">${esc(d.Socket)}</span>`);
    if (d.Brand) chips.push(`<span class="chip">${esc(d.Brand)}</span>`);
  } else { // hdd / ssd
    title = `${fmtCap(d.CapacityGB)} ${d.Interface || 'SATA'}`;
    if (d.DriveType === 'External') chips.push('<span class="chip">EXT</span>');
    if (d.Brand) chips.push(`<span class="chip">${esc(d.Brand)}</span>`);
    if (c === 'hdd' && d.RPM) chips.push(`<span class="chip">${d.RPM.toLocaleString()} rpm</span>`);
    if (c === 'ssd' && d.Gen) chips.push(`<span class="chip">Gen${d.Gen}</span>`);
    if (d.FormFactor && d.FormFactor !== 'Ext') chips.push(`<span class="chip">${esc(d.FormFactor)}</span>`);
  }
  if (d.SurfacedAt) subs.push(`spotted ${timeAgo(d.SurfacedAt)}`);
  return `<h3><a href="/deal/${d.ID}" style="color:inherit">${esc(title)}</a></h3><div>${chips.join('')}</div>
          ${subs.length ? `<div class="sub faint">${subs.join(' · ')}</div>` : ''}`;
}

/* ── price-position bar: min→max range, tick=median, dot=now, hollow=predicted ── */
function posbar(d) {
  const qty = Number(d.Quantity) || 1;
  const lo = Number(d.MinMarketPrice) * qty, hi = Number(d.MaxMarketPrice) * qty;
  const med = Number(d.AvgMarketPrice) * qty;
  const now = Number(d.CurrentPrice);
  const pred = d.PremiumSamples > 0 ? Number(d.PredictedFinalPrice) : null;
  if (!(hi > lo)) return '';
  const pct = v => Math.max(0, Math.min(100, (v - lo) / (hi - lo) * 100)).toFixed(1);
  return `<div class="posbar">
      <div class="track"></div>
      <span class="tick" style="left:${pct(med)}%" title="market median ${fmtGBP(med)}"></span>
      ${pred != null ? `<span class="dot hollow" style="left:${pct(pred)}%" title="predicted final ${fmtGBP(pred)}"></span>` : ''}
      <span class="dot" style="left:${pct(now)}%" title="current ${fmtGBP(now)}"></span>
    </div>
    <div class="posbar-legend"><span>${fmtGBP0(lo)}</span><span><a href="${modelHref(d._cat, d)}" title="this model's market page" style="color:inherit">med ${fmtGBP0(med)}</a></span><span>${fmtGBP0(hi)}</span></div>`;
}

function priceCol(d) {
  const qty = Number(d.Quantity) || 1;
  const ship = Number(d.Shipping) || 0;
  const bits = [];
  bits.push(`<div class="price-now num">${fmtGBP(d.ItemPrice)}
      <span class="ship">${ship > 0 ? '+' + fmtGBP(ship) + ' delivery' : 'free delivery'}</span></div>`);
  const extra = [];
  if (qty > 1) extra.push(`${fmtGBP(d.PerUnitPrice)}/unit`);
  if (d.PremiumSamples > 0 && d.PredictedFinalPrice !== d.CurrentPrice)
    extra.push(`predicted final <b>${fmtGBP(d.PredictedFinalPrice)}</b> <span class="faint">(n=${d.PremiumSamples})</span>`);
  if (extra.length) bits.push(`<div class="sub pred">${extra.join(' · ')}</div>`);
  bits.push(posbar(d));
  return bits.join('');
}

function metaCol(d) {
  const disc = Number(d.PredictedDiscountPct ?? d.DiscountPct);
  return `<div class="countdown" ${endsAttr(d.EndTime)}></div>
    <div class="sub">${d.Bids || 0} bid${d.Bids === 1 ? '' : 's'}${
      d.SellerFeedbackPct != null && d.SellerFeedbackCount >= 3
        ? ` · ${d.SellerFeedbackPct}% seller` : ''}</div>
    <div style="margin-top:4px"><span class="discount-tag ${disc >= 30 ? 'big' : ''}">${disc.toFixed(1)}% off</span>
      <span class="sub faint">+${fmtGBP0(d.PotentialGain)}</span></div>
    <div class="spark-slot" data-spark="${d.ID}"></div>`;
}

function render() {
  const minDisc = Number($('#f-disc').value) || 0;
  const minSave = Number($('#f-save').value) || 0;
  const sortCol = $('#f-sort').value;
  let rows = allDeals.filter(d => cat === 'all' || d._cat === cat);
  rows = filterByCtx(rows, cat, ctx);
  rows = rows.filter(d => Number(d.DiscountPct) >= minDisc && Number(d.PotentialGain) >= minSave);
  rows = sortRows(rows, sortCol, sortCol === 'EndTime' || sortCol === 'ItemPrice');
  $('#deal-count').textContent = `${rows.length} deal${rows.length === 1 ? '' : 's'}`;
  const box = $('#rows');
  if (!rows.length) {
    box.innerHTML = '<div class="state">No deals match — widen the window or lower the filters.</div>';
    return;
  }
  box.innerHTML = rows.map(d => `
    <article class="row-card">
      <div class="id-col">${identity(d)}</div>
      <div class="price-col">${priceCol(d)}</div>
      <div class="meta-col">${metaCol(d)}</div>
      <a class="go" href="${safeUrl(d.URL)}" target="_blank" rel="noopener noreferrer">View →</a>
    </article>`).join('') + bundlesSection();
  loadSparklines(rows);
}

/* CPU+motherboard bundles — shown but not scored (their price covers two
   parts). Same category chip + context filters, no discount badge. */
function bundlesSection() {
  let b = allBundles.filter(d => cat === 'all' || d._cat === cat);
  b = filterByCtx(b, cat, ctx);
  if (!b.length) return '';
  return `<div class="sub faint" style="margin:16px 0 6px">
      ${b.length} CPU + motherboard bundle${b.length === 1 ? '' : 's'} —
      shown for reference, not scored (the price covers two parts):</div>` +
    b.map(d => {
      const ship = Number(d.Shipping) || 0;
      return `<article class="row-card">
        <div class="id-col">${identity(d)}
          <div><span class="chip hot">BUNDLE</span></div></div>
        <div class="price-col">
          <div class="price-now num">${fmtGBP(d.ItemPrice)}
            <span class="ship">${ship > 0 ? '+' + fmtGBP(ship) + ' delivery' : 'free delivery'}</span></div>
          <div class="sub faint">${d.ListingType === 'bin' ? 'fixed price' : (d.Bids || 0) + ' bids'}</div>
        </div>
        <div class="meta-col"><div class="countdown" ${endsAttr(d.EndTime)}></div></div>
        <a class="go" href="${safeUrl(d.URL)}" target="_blank" rel="noopener noreferrer">View →</a>
      </article>`;
    }).join('');
}

async function loadSparklines(rows) {
  const ids = rows.filter(d => d.SurfacedAt).map(d => d.ID).slice(0, 60);
  if (!ids.length) return;
  try {
    const res = await fetch('/api/snapshots?ids=' + ids.join(','));
    const data = await res.json();
    if (data.status !== 'ok') return;
    for (const [id, series] of Object.entries(data.series)) {
      const slot = $(`[data-spark="${id}"]`);
      if (slot && series.length >= 2)
        slot.innerHTML = sparkline(series) +
          `<div class="sub faint">${series.length} price checks</div>`;
    }
  } catch { /* sparklines are decoration — never block the page */ }
}

async function load() {
  $('#refresh-btn').disabled = true;
  try {
    const w = $('#f-window').value;
    // Fetch every category at the surfacing floor; the disc/save/ctx filters
    // narrow it client-side without a round-trip.
    const minDisc = Math.min(Number($('#f-disc').value) || 20, 20);
    const res = await fetch(`/api/deals?type=all&window=${w}&min_discount=${minDisc}`);
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'error');
    allDeals = data.deals;
    allBundles = data.bundles || [];
    render();
  } catch (e) {
    $('#rows').innerHTML = `<div class="state">Couldn’t load deals: ${esc(e.message)}</div>`;
  } finally {
    $('#refresh-btn').disabled = false;
  }
}

/* ── category chips + context filters (shared with Prices/BIN) ── */
$$('#cat-pills .pill').forEach(p => {
  p.classList.toggle('active', p.dataset.cat === cat);
  p.addEventListener('click', () => {
    cat = p.dataset.cat;
    ctx = {};                         // filters are per-type — reset on switch
    $$('#cat-pills .pill').forEach(x => x.classList.toggle('active', x === p));
    renderCtxFilters($('#ctx-filters'), cat, ctx, render);
    render();
  });
});
renderCtxFilters($('#ctx-filters'), cat, ctx, render);

/* ── "Watch these": an auction feed for the current category + filters ── */
$('#watch-btn').addEventListener('click', () => {
  const card = $('#watch-card');
  if (card.style.display !== 'none') { card.style.display = 'none'; return; }
  if (cat === 'all') {
    $('#deal-count').textContent = 'pick a category first (GPU/CPU/…) to watch';
    return;
  }
  card.style.display = '';
  const bits = [];
  for (const f of (CTX_FILTERS[cat] || [])) if (ctx[f.key]) {
    const opt = f.opts.find(o => o[0] === ctx[f.key]);
    bits.push(opt ? opt[1] : ctx[f.key]);
  }
  $('#watch-summary').innerHTML =
    `New auction deals for <b>${cat.toUpperCase()}</b> — ${esc(bits.length ? bits.join(' · ') : 'any model')} — push to your notification endpoint.`;
  if (!$('#w-disc').value) $('#w-disc').value = Math.max(20, Number($('#f-disc').value) || 20);
});

$('#w-save').addEventListener('click', async () => {
  $('#w-status').textContent = '…';
  try {
    const hasFilters = Object.keys(ctx).length > 0;
    const res = await fetch('/api/subscriptions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        category: cat, kind: 'discount_pct',
        scope_kind: hasFilters ? 'filter' : 'all',
        listing_type: 'auction', filters: ctx,
        min_discount: parseFloat($('#w-disc').value),
      }),
    });
    const data = await res.json();
    if (res.status === 401) { $('#w-status').innerHTML = 'sign in first — <a href="/login">login</a>'; return; }
    if (data.status !== 'ok') { $('#w-status').textContent = data.message || 'error'; return; }
    $('#w-status').innerHTML = data.has_endpoint
      ? 'feed created ✓ — manage it in <a href="/settings">settings</a>'
      : 'created ✓ — but set your <a href="/settings">notification endpoint</a> to receive it';
  } catch { $('#w-status').textContent = 'network error'; }
});

$('#f-window').addEventListener('change', () => { persist(); load(); });
$('#f-disc').addEventListener('input', () => { persist(); render(); });
$('#f-save').addEventListener('input', () => { persist(); render(); });
$('#f-sort').addEventListener('change', () => { persist(); render(); });
$('#refresh-btn').addEventListener('click', load);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
setInterval(() => { if (!document.hidden) load(); }, 5 * 60 * 1000);
load();
