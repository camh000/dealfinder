/* Buy-It-Now feed: fixed-price listings under market, sorted by discount.
   No countdowns, no bids, no predictions — the price on screen is the price.

   Unlike auctions (where bidding corrects a silly start price), a BIN price
   is whatever the seller typed — and 90%-off fixed prices are eBay's classic
   scam shape (hijacked accounts, fake stock, "read description"). Anything
   at SCAM_DISCOUNT+ gets flagged and sunk to its own section, not hidden. */
const SCAM_DISCOUNT = 60;

let allDeals = [], cat = 'all';

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
    chips.push('<span class="chip" style="color:var(--loss);background:var(--loss-soft)" title="Fixed prices this far under market are usually fakes, empty boxes or hijacked accounts — read the listing very carefully.">⚠ TOO GOOD?</span>');
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
  const rows = allDeals.filter(d => cat === 'all' || d._cat === cat);
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
    const res = await fetch(`/api/bin-deals?type=all&min_discount=${disc}`);
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
  $$('#cat-pills .pill').forEach(x => x.classList.toggle('active', x === p));
  render();
}));
$('#f-disc').addEventListener('change', load);
$('#refresh-btn').addEventListener('click', load);
setInterval(() => { if (!document.hidden) load(); }, 5 * 60 * 1000);
load();
