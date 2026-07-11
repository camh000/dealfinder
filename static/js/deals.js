/* Deals page: fetch → filter → render card rows with price-position bars,
   sparklines from DealSnapshots, freshness chips, live countdowns. */

const CAT = window.PAGE_CATEGORY;
const LS_KEY = 'pcd-deal-filters';

let allDeals = [];
const saved = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
$('#f-window').value = saved.window || '2';
$('#f-disc').value = saved.disc ?? 20;
$('#f-save').value = saved.save ?? 0;
$('#f-sort').value = saved.sort || 'DealScore';

function persist() {
  localStorage.setItem(LS_KEY, JSON.stringify({
    window: $('#f-window').value, disc: $('#f-disc').value,
    save: $('#f-save').value, sort: $('#f-sort').value, brand: $('#f-brand').value,
  }));
}

/* ── identity line per category ── */
function identity(d) {
  const chips = [];
  const lot = Number(d.Quantity) > 1;
  if (lot) chips.push(`<span class="chip hot">×${d.Quantity} LOT</span>`);
  if (d.SurfacedAt && Date.now() - new Date(d.SurfacedAt).getTime() < 3600e3)
    chips.push('<span class="chip new">NEW</span>');
  let title = '', subs = [];
  if (CAT === 'gpu') {
    title = d.Model || '—';
    if (d.Brand) chips.push(`<span class="chip">${esc(d.Brand)}</span>`);
    if (d.VRAM && !(d.Model || '').includes('GB')) chips.push(`<span class="chip">${d.VRAM}GB</span>`);
  } else if (CAT === 'cpu') {
    title = d.Model || '—';
    if (d.Socket) chips.push(`<span class="chip">${esc(d.Socket)}</span>`);
    if (d.Cores) chips.push(`<span class="chip">${d.Cores} cores</span>`);
  } else if (CAT === 'ram') {
    title = `${d.CapacityGB}GB ${d.Type || ''}`.trim();
    if (d.KitConfig) chips.push(`<span class="chip">${esc(d.KitConfig)}</span>`);
    if (d.FormFactor === 'SODIMM') chips.push('<span class="chip">SODIMM</span>');
    if (d.Brand) chips.push(`<span class="chip">${esc(d.Brand)}</span>`);
    if (d.Speed) chips.push(`<span class="chip">${d.Speed}MHz</span>`);
  } else { // hdd / ssd
    title = `${fmtCap(d.CapacityGB)} ${d.Interface || 'SATA'}`;
    if (d.DriveType === 'External') chips.push('<span class="chip">EXT</span>');
    if (d.Brand) chips.push(`<span class="chip">${esc(d.Brand)}</span>`);
    if (CAT === 'hdd' && d.RPM) chips.push(`<span class="chip">${d.RPM.toLocaleString()} rpm</span>`);
    if (CAT === 'ssd' && d.Gen) chips.push(`<span class="chip">Gen${d.Gen}</span>`);
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
    <div class="posbar-legend"><span>${fmtGBP0(lo)}</span><span>med ${fmtGBP0(med)}</span><span>${fmtGBP0(hi)}</span></div>`;
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
  const brand = $('#f-brand').value;
  const minDisc = Number($('#f-disc').value) || 0;
  const minSave = Number($('#f-save').value) || 0;
  const sortCol = $('#f-sort').value;
  let rows = allDeals.filter(d =>
    (!brand || d.Brand === brand) &&
    Number(d.DiscountPct) >= minDisc &&
    Number(d.PotentialGain) >= minSave);
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
    </article>`).join('');
  loadSparklines(rows);
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

function populateBrands() {
  const brands = [...new Set(allDeals.map(d => d.Brand).filter(Boolean))].sort();
  const sel = $('#f-brand');
  const cur = saved.brand || '';
  sel.innerHTML = '<option value="">all</option>' +
    brands.map(b => `<option${b === cur ? ' selected' : ''}>${esc(b)}</option>`).join('');
}

async function load() {
  $('#refresh-btn').disabled = true;
  try {
    const w = $('#f-window').value;
    const minDisc = Math.min(Number($('#f-disc').value) || 20, 20);
    const res = await fetch(`/api/deals?type=${CAT}&window=${w}&min_discount=${minDisc}`);
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'error');
    allDeals = data.deals;
    populateBrands();
    render();
  } catch (e) {
    $('#rows').innerHTML = `<div class="state">Couldn’t load deals: ${esc(e.message)}</div>`;
  } finally {
    $('#refresh-btn').disabled = false;
  }
}

$('#f-window').addEventListener('change', () => { persist(); load(); });
$('#f-brand').addEventListener('change', () => { persist(); render(); });
$('#f-disc').addEventListener('input', () => { persist(); render(); });
$('#f-save').addEventListener('input', () => { persist(); render(); });
$('#f-sort').addEventListener('change', () => { persist(); render(); });
$('#refresh-btn').addEventListener('click', load);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
setInterval(() => { if (!document.hidden) load(); }, 5 * 60 * 1000);
load();
