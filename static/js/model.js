/* Model detail: one market group's stats, monthly trend chart, live
   auctions, and the individual recent sales behind the median. */

const CAT = window.PAGE_CATEGORY;
const params = new URLSearchParams(location.search);

const MONTH_ABBR = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
// "2026-07" → "Jul '26" — unambiguous, unlike the old "26-07" (read as a day).
const monthLabel = m => {
  const [y, mo] = m.split('-');
  return `${MONTH_ABBR[+mo - 1]} '${y.slice(2)}`;
};

function chart(trend, median, predictions, windowStart) {
  if (!trend || trend.length < 2)
    return '<div class="state">Not enough monthly history yet.</div>';
  // On phones a 720-unit viewBox renders unreadably small — emit a narrow
  // chart with thinned labels instead.
  const narrow = isNarrowScreen();
  const W = narrow ? 380 : 720, H = narrow ? 200 : 180,
        P = narrow ? 30 : 34, PR = narrow ? 56 : 76;
  const step = narrow && trend.length > 4 ? 2 : 1;   // label every other month
  const preds = (predictions || []).filter(v => v != null);
  const domain = trend.map(t => t.median).concat(median != null ? [median] : [], preds);
  const lo = Math.min(...domain) * 0.92, hi = Math.max(...domain) * 1.05;
  const x = i => P + i / (trend.length - 1) * (W - P - PR);
  const y = v => H - 24 - (v - lo) / ((hi - lo) || 1) * (H - 48);
  const pts = trend.map((t, i) => `${x(i).toFixed(1)},${y(t.median).toFixed(1)}`).join(' ');

  // Shade the months that fall OUTSIDE the 120-day median window. The median
  // uses only the last 120 days; the chart shows ~6 months for trend context,
  // so this makes clear which points actually feed the headline median.
  let outWindow = '';
  if (windowStart) {
    const cut = new Date(windowStart).getTime();
    const monthEnd = m => { const [yy, mm] = m.split('-').map(Number); return Date.UTC(yy, mm, 1); };
    const firstIn = trend.findIndex(t => monthEnd(t.month) > cut);
    if (firstIn > 0) {                       // some months are out of window
      const dx = (x(firstIn - 1) + x(firstIn)) / 2;
      outWindow = `<rect x="${P}" y="${(H - 24 - (H - 48)).toFixed(1)}" width="${(dx - P).toFixed(1)}"
          height="${(H - 48).toFixed(1)}" fill="var(--fg)" opacity="0.05"/>
        <line x1="${dx.toFixed(1)}" y1="10" x2="${dx.toFixed(1)}" y2="${H - 24}"
          stroke="var(--faint,#888)" stroke-width="1" stroke-dasharray="3 3"/>
        <text x="${(dx + 4).toFixed(1)}" y="16" font-size="9" fill="var(--faint,#888)">120-day median →</text>`;
    }
  }

  // Track every text label we draw so nothing overlaps.
  const placed = [];
  const collides = (px, py) =>
    placed.some(q => Math.abs(q.y - py) < 11 && Math.abs(q.x - px) < 58);
  const monthLabels = trend.map((t, i) => {
    if (i % step !== 0) return '';
    const tx = x(i), ty = y(t.median) - 8;
    placed.push({ x: tx, y: ty });
    return `<text x="${tx.toFixed(1)}" y="${ty.toFixed(1)}" text-anchor="middle">${fmtGBP0(t.median)}</text>`;
  }).join('');

  // 120-day median reference line, labelled at the line itself — but only
  // where the label won't sit on top of anything else.
  let medLine = '';
  if (median != null) {
    const my = y(median);
    medLine = `<line class="medline" x1="${P}" y1="${my.toFixed(1)}" x2="${W - PR + 6}" y2="${my.toFixed(1)}">
      <title>120-day median ${fmtGBP(median)}</title></line>`;
    const label = `med ${fmtGBP0(median)}`;
    for (const cy of [my + 3, my - 9, my + 14]) {
      if (!collides(W - PR + 36, cy) && cy > 10 && cy < H - 26) {
        medLine += `<text class="medlabel" x="${W - PR + 9}" y="${cy.toFixed(1)}">${label}</text>`;
        placed.push({ x: W - PR + 36, y: cy });
        break;
      }
    }
  }

  // Predicted finals for live listings: graphical markers only, no text.
  const px = W - PR - 8;
  const predMarks = preds.map(v => `
    <circle class="predpt" cx="${px}" cy="${y(v).toFixed(1)}" r="4.5">
      <title>predicted final ${fmtGBP(v)}</title></circle>`).join('');

  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="monthly median prices">
    ${outWindow}
    <line class="axis" x1="${P}" y1="${H - 24}" x2="${W - PR + 6}" y2="${H - 24}"/>
    ${medLine}
    <polyline class="line" points="${pts}"/>
    ${trend.map((t, i) => `
      <circle class="pt" cx="${x(i).toFixed(1)}" cy="${y(t.median).toFixed(1)}" r="3">
        <title>${t.month}: ${fmtGBP(t.median)} (n=${t.n})</title></circle>
      ${i % step === 0 ? `<text x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle">${monthLabel(t.month)}</text>` : ''}
    `).join('')}
    ${monthLabels}
    ${predMarks}
  </svg>`;
}

/* ── price alert: pop the form, prefill target from the median ── */
let groupMedian = null;
/* Each "Fire when" option maps to a subscription: a %-discount trigger for
   deal alerts (auction / BIN) or a £-target trigger for price alerts. The form
   swaps between the % and £ input, and each explains itself. */
const ALERT_KINDS = {
  auction_deal: { trigger: 'discount_pct', listing_type: 'auction', field: 'disc',
    help: 'Pushes once per listing when an auction for this model appears at least this far under its market median — and only when the predicted final still clears the discount, so a 99p-start with days left never counts.' },
  bin_deal: { trigger: 'discount_pct', listing_type: 'bin', field: 'disc',
    help: 'Pushes when a fixed-price (Buy It Now) listing for this model appears at least this far under its market median.' },
  listing_price: { trigger: 'listing_price', listing_type: 'any', field: 'target',
    help: '"Genuinely available" means a Buy-It-Now at that price, or an auction in its final 2 hours whose predicted final also clears the target — a 99p-start with days left never counts.' },
  median_price: { trigger: 'median_price', listing_type: 'any', field: 'target',
    help: 'Pushes when the 120-day sold median for this model itself drops under your target.' },
};

function syncAlertForm() {
  const spec = ALERT_KINDS[$('#al-kind').value];
  $('#al-disc-fg').style.display = spec.field === 'disc' ? '' : 'none';
  $('#al-target-fg').style.display = spec.field === 'target' ? '' : 'none';
  $('#al-help').textContent = spec.help;
  if (spec.field === 'target' && groupMedian != null && !$('#al-target').value)
    $('#al-target').value = (groupMedian * 0.85).toFixed(2);
}

$('#al-kind').addEventListener('change', syncAlertForm);

$('#alert-btn').addEventListener('click', () => {
  const card = $('#alert-card');
  if (card.style.display !== 'none') { card.style.display = 'none'; return; }
  card.style.display = '';
  syncAlertForm();
});

$('#al-save').addEventListener('click', async () => {
  $('#al-status').textContent = '…';
  const spec = ALERT_KINDS[$('#al-kind').value];
  const body = {
    category: CAT,
    scope_kind: 'group',
    group: Object.fromEntries(params),
    label: groupLabel(CAT, Object.fromEntries(params)),
    kind: spec.trigger,
    listing_type: spec.listing_type,
  };
  if (spec.field === 'disc') body.min_discount = parseFloat($('#al-disc').value);
  else body.target_price = parseFloat($('#al-target').value);
  try {
    const res = await fetch('/api/subscriptions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (res.status === 401) { $('#al-status').innerHTML = 'sign in first — <a href="/login">login</a>'; return; }
    if (data.status !== 'ok') { $('#al-status').textContent = data.message || 'error'; return; }
    $('#al-status').innerHTML = data.has_endpoint
      ? 'alert created ✓ — manage it in <a href="/settings">settings</a>'
      : 'created ✓ — but set your <a href="/settings">notification endpoint</a> to receive it';
  } catch { $('#al-status').textContent = 'network error'; }
});

(async () => {
  $('#model-title').textContent = groupLabel(CAT, Object.fromEntries(params));
  try {
    const res = await fetch(`/api/model-detail?type=${CAT}&${params.toString()}`);
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'error');

    const s = data.stats;
    groupMedian = s.median;
    $('#model-sub').textContent = `${CAT.toUpperCase()} market group`;
    $('#stats').innerHTML = `
      <div class="stat"><b class="num">${fmtGBP(s.median)}</b><span>median (120d)</span></div>
      <div class="stat"><b class="num">${fmtGBP0(s.min)}–${fmtGBP0(s.max)}</b><span>range</span></div>
      <div class="stat"><b class="num">${s.n}</b><span>sales in window</span></div>
      <div class="stat"><b class="num">${data.live.length}</b><span>live tracked</span></div>`;

    $('#trend-chart').innerHTML = chart(
      data.trend, data.stats.median,
      data.live.map(l => l.PredictedFinalPrice), data.window_start);

    if (data.live.length) {
      $('#live-card').style.display = '';
      $('#live-tbl tbody').innerHTML = data.live.map(l => `
        <tr>
          <td><a href="/deal/${l.ID}" style="color:inherit">${esc(l.Title)}</a>${Number(l.Quantity) > 1 ? ` <span class="chip hot">×${l.Quantity}</span>` : ''}</td>
          <td class="num">${fmtGBP(l.ItemPrice)}${Number(l.Shipping) > 0 ? `<div class="dimcell">+${fmtGBP(l.Shipping)} del.</div>` : ''}${l.PredictedFinalPrice != null ? `<div class="dimcell">pred ~${fmtGBP(l.PredictedFinalPrice)}</div>` : ''}</td>
          <td class="num dimcell m-hide">${l.Bids || 0}</td>
          <td><span class="countdown" ${endsAttr(l.EndTime)}></span></td>
          <td class="m-hide"><a href="${safeUrl(l.URL)}" target="_blank" rel="noopener noreferrer">view</a></td>
        </tr>`).join('');
    }

    $('#sold-tbl tbody').innerHTML = data.sold.length ? data.sold.map(r => `
      <tr>
        <td class="dimcell">${fmtDate(r.SoldDate)}</td>
        <td><a href="/deal/${r.ID}" style="color:inherit">${esc(r.Title)}</a>${Number(r.Quantity) > 1 ? ` <span class="chip hot">×${r.Quantity}</span>` : ''}</td>
        <td class="num"><b>${fmtGBP(r.Price)}</b></td>
        <td class="m-hide"><a href="${safeUrl(r.URL)}" target="_blank" rel="noopener noreferrer">view</a></td>
      </tr>`).join('')
      : '<tr><td class="state" colspan="4">No recorded sales for this group yet.</td></tr>';
  } catch (e) {
    $('#sold-tbl tbody').innerHTML =
      `<tr><td class="state" colspan="4">Couldn’t load: ${esc(e.message)}</td></tr>`;
    $('#trend-chart').innerHTML = '';
  }
})();
