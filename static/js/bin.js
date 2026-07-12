/* Buy-It-Now feed: fixed-price listings under market, sorted by discount.
   No countdowns, no bids, no predictions — the price on screen is the price.

   Unlike auctions (where bidding corrects a silly start price), a BIN price
   is whatever the seller typed. The scraper now skips "choose a capacity"
   variation listings (the usual source of impossible discounts), so anything
   still showing SCAM_DISCOUNT+ under market deserves suspicion — flagged and
   sunk to its own section, not hidden. */
const SCAM_DISCOUNT = 60;

let allDeals = [], cat = 'all', ctx = {};

function posbar(d) {
  const qty = Number(d.Quantity) || 1;
  const lo = Number(d.MinMarketPrice) * qty, hi = Number(d.MaxMarketPrice) * qty;
  const med = Number(d.AvgMarketPrice) * qty;
  const now = Number(d.CurrentPrice);
  if (!(hi > lo)) return '';
  const pct = v => Math.max(0, Math.min(100, (v - lo) / (hi - lo) * 100)).toFixed(1);
  return `<div class="posbar">
      <div class="track"></div>
      <span class="tick" style="left:${pct(med)}%" title="market median ${fmtGBP(med)}"></span>
      <span class="dot" style="left:${pct(now)}%" title="asking ${fmtGBP(now)}"></span>
    </div>
    <div class="posbar-legend"><span>${fmtGBP0(lo)}</span><span><a href="${modelHref(d._cat, d)}" style="color:inherit">med ${fmtGBP0(med)}</a></span><span>${fmtGBP0(hi)}</span></div>`;
}

function card(d) {
  const qty = Number(d.Quantity) || 1;
  const ship = Number(d.Shipping) || 0;
  const disc = Number(d.DiscountPct);
  const chips = [`<span class="chip">${d._cat.toUpperCase()}</span>`,
                 '<span class="chip new">BIN</span>'];
  if (disc >= SCAM_DISCOUNT)
    chips.push('<span class="chip" style="color:var(--loss);background:var(--loss-soft)" title="Fixed prices this far under market are rarely real — read the listing very carefully before buying.">⚠ TOO GOOD?</span>');
  if (qty > 1) chips.push(`<span class="chip hot">×${qty} LOT</span>`);
  return `<article class="row-card">
    <div class="id-col">
      <h3><a href="/deal/${d.ID}" style="color:inherit">${esc(d._label || '—')}</a></h3>
      <div>${chips.join('')}</div>
      ${d.SellerFeedbackPct != null && d.SellerFeedbackCount >= 3
        ? `<div class="sub faint">${d.SellerFeedbackPct}% seller (${d.SellerFeedbackCount})</div>` : ''}
    </div>
    <div class="price-col">
      <div class="price-now num">${fmtGBP(d.ItemPrice)}
        <span class="ship">${ship > 0 ? '+' + fmtGBP(ship) + ' delivery' : 'free delivery'}</span></div>
      ${qty > 1 ? `<div class="sub pred">${fmtGBP(d.PerUnitPrice)}/unit</div>` : ''}
      ${posbar(d)}
    </div>
    <div class="meta-col">
      <div style="margin-top:4px"><span class="discount-tag ${disc >= 30 ? 'big' : ''}">${disc.toFixed(1)}% off</span>
        <span class="sub faint">+${fmtGBP0(d.PotentialGain)}</span></div>
      <div class="sub faint" style="margin-top:6px">fixed price — no bidding</div>
    </div>
    <a class="go" href="${safeUrl(d.URL)}" target="_blank" rel="noopener noreferrer">Buy →</a>
  </article>`;
}

function render() {
  let rows = allDeals.filter(d => cat === 'all' || d._cat === cat);
  rows = filterByCtx(rows, cat, ctx);
  $('#bin-count').textContent = `${rows.length} listing${rows.length === 1 ? '' : 's'}`;
  if (!rows.length) {
    $('#rows').innerHTML = '<div class="state">Nothing under the bar right now — lower the min discount or check back after the next sweep.</div>';
    return;
  }
  const real = rows.filter(d => Number(d.DiscountPct) < SCAM_DISCOUNT);
  const sus = rows.filter(d => Number(d.DiscountPct) >= SCAM_DISCOUNT);
  $('#rows').innerHTML = real.map(card).join('') +
    (sus.length ? `<div class="sub faint" style="margin:14px 0 6px">Probably too good to be true — ${sus.length} listing${sus.length === 1 ? '' : 's'} more than ${SCAM_DISCOUNT}% under market:</div>` +
      sus.map(card).join('') : '');
}

async function load() {
  $('#refresh-btn').disabled = true;
  try {
    const disc = Math.max(5, Math.min(Number($('#f-disc').value) || 10, 90));
    const added = Number($('#f-added').value) || 0;   // hours since first seen; 0 = any
    const res = await fetch(`/api/bin-deals?type=all&min_discount=${disc}&added_within=${added}`);
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'error');
    allDeals = data.deals;
    render();
  } catch (e) {
    $('#rows').innerHTML = `<div class="state">Couldn’t load: ${esc(e.message)}</div>`;
  } finally {
    $('#refresh-btn').disabled = false;
  }
}

$$('#cat-pills .pill').forEach(p => p.addEventListener('click', () => {
  cat = p.dataset.cat;
  ctx = {};                       // filters are per-type — reset on switch
  $$('#cat-pills .pill').forEach(x => x.classList.toggle('active', x === p));
  renderCtxFilters($('#ctx-filters'), cat, ctx, render);
  render();
}));
$('#f-disc').addEventListener('change', load);
$('#f-added').addEventListener('change', load);
$('#refresh-btn').addEventListener('click', load);

/* ── "Watch this": save the current category + filters + min-discount as a
      BIN subscription (discount_pct / listing type bin), managed in Settings ── */
function ctxSummary() {
  const defs = (typeof CTX_FILTERS !== 'undefined' && CTX_FILTERS[cat]) || [];
  const bits = [];
  for (const f of defs) if (ctx[f.key]) {
    const opt = f.opts.find(o => o[0] === ctx[f.key]);
    bits.push(opt ? opt[1] : ctx[f.key]);
  }
  return bits.length ? bits.join(' · ') : 'any model';
}

$('#watch-btn').addEventListener('click', () => {
  const card = $('#watch-card');
  if (card.style.display !== 'none') { card.style.display = 'none'; return; }
  if (cat === 'all') {
    $('#bin-count').textContent = 'pick a category first (GPU/CPU/…) to watch';
    return;
  }
  card.style.display = '';
  $('#watch-summary').innerHTML =
    `Watching <b>${cat.toUpperCase()}</b> — ${esc(ctxSummary())}. New Buy-It-Now finds matching this push to your notification endpoint.`;
  if (!$('#w-disc').value) $('#w-disc').value = $('#f-disc').value || 20;
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
        listing_type: 'bin', filters: ctx,
        min_discount: parseFloat($('#w-disc').value),
      }),
    });
    const data = await res.json();
    if (res.status === 401) { $('#w-status').innerHTML = 'sign in first — <a href="/login">login</a>'; return; }
    if (data.status !== 'ok') { $('#w-status').textContent = data.message || 'error'; return; }
    $('#w-status').innerHTML = data.has_endpoint
      ? 'watch created ✓ — manage it in <a href="/settings">settings</a>'
      : 'created ✓ — but set your <a href="/settings">notification endpoint</a> to receive it';
  } catch { $('#w-status').textContent = 'network error'; }
});

setInterval(() => { if (!document.hidden) load(); }, 5 * 60 * 1000);
load();
