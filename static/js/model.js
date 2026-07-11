/* Model detail: one market group's stats, monthly trend chart, live
   auctions, and the individual recent sales behind the median. */

const CAT = window.PAGE_CATEGORY;
const params = new URLSearchParams(location.search);

function chart(trend, median, predictions) {
  if (!trend || trend.length < 2)
    return '<div class="state">Not enough monthly history yet.</div>';
  const W = 720, H = 180, P = 34, PR = 76;   // extra right pad for the median label
  const preds = (predictions || []).filter(v => v != null);
  const domain = trend.map(t => t.median).concat(median != null ? [median] : [], preds);
  const lo = Math.min(...domain) * 0.92, hi = Math.max(...domain) * 1.05;
  const x = i => P + i / (trend.length - 1) * (W - P - PR);
  const y = v => H - 24 - (v - lo) / ((hi - lo) || 1) * (H - 48);
  const pts = trend.map((t, i) => `${x(i).toFixed(1)},${y(t.median).toFixed(1)}`).join(' ');

  // Track every text label we draw so nothing overlaps.
  const placed = [];
  const collides = (px, py) =>
    placed.some(q => Math.abs(q.y - py) < 11 && Math.abs(q.x - px) < 58);
  const monthLabels = trend.map((t, i) => {
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
    <line class="axis" x1="${P}" y1="${H - 24}" x2="${W - PR + 6}" y2="${H - 24}"/>
    ${medLine}
    <polyline class="line" points="${pts}"/>
    ${trend.map((t, i) => `
      <circle class="pt" cx="${x(i).toFixed(1)}" cy="${y(t.median).toFixed(1)}" r="3">
        <title>${t.month}: ${fmtGBP(t.median)} (n=${t.n})</title></circle>
      <text x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle">${t.month.slice(2)}</text>
    `).join('')}
    ${monthLabels}
    ${predMarks}
  </svg>`;
}

/* ── price alert: pop the form, prefill target from the median ── */
let groupMedian = null;
$('#alert-btn').addEventListener('click', async () => {
  const card = $('#alert-card');
  if (card.style.display !== 'none') { card.style.display = 'none'; return; }
  card.style.display = '';
  if (groupMedian != null && !$('#al-target').value)
    $('#al-target').value = (groupMedian * 0.85).toFixed(2);
  if (!$('#al-recipient').options.length) {
    try {
      const data = await fetch('/api/notify-settings').then(r => r.json());
      const recips = (data.recipients || []).filter(r => r.Enabled !== false);
      $('#al-recipient').innerHTML = recips.length
        ? recips.map(r => `<option value="${r.ID}">${esc(r.Name || 'recipient ' + r.ID)}</option>`).join('')
        : '<option value="">no recipients — add one in settings</option>';
    } catch { $('#al-recipient').innerHTML = '<option value="">couldn’t load recipients</option>'; }
  }
});

$('#al-save').addEventListener('click', async () => {
  $('#al-status').textContent = '…';
  try {
    const res = await fetch('/api/alerts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        category: CAT,
        group: Object.fromEntries(params),
        label: groupLabel(CAT, Object.fromEntries(params)),
        kind: $('#al-kind').value,
        target_price: parseFloat($('#al-target').value),
        recipient_id: $('#al-recipient').value ? Number($('#al-recipient').value) : null,
      }),
    });
    const data = await res.json();
    if (res.status === 401) { $('#al-status').innerHTML = 'sign in first — <a href="/login">login</a>'; return; }
    $('#al-status').textContent = data.status === 'ok'
      ? 'alert created ✓ — manage it in settings' : (data.message || 'error');
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
      data.live.map(l => l.PredictedFinalPrice));

    if (data.live.length) {
      $('#live-card').style.display = '';
      $('#live-tbl tbody').innerHTML = data.live.map(l => `
        <tr>
          <td><a href="/deal/${l.ID}" style="color:inherit">${esc(l.Title)}</a>${Number(l.Quantity) > 1 ? ` <span class="chip hot">×${l.Quantity}</span>` : ''}</td>
          <td class="num">${fmtGBP(l.ItemPrice)}${Number(l.Shipping) > 0 ? `<div class="dimcell">+${fmtGBP(l.Shipping)} del.</div>` : ''}${l.PredictedFinalPrice != null ? `<div class="dimcell">pred ~${fmtGBP(l.PredictedFinalPrice)}</div>` : ''}</td>
          <td class="num dimcell">${l.Bids || 0}</td>
          <td><span class="countdown" ${endsAttr(l.EndTime)}></span></td>
          <td><a href="${safeUrl(l.URL)}" target="_blank" rel="noopener noreferrer">view</a></td>
        </tr>`).join('');
    }

    $('#sold-tbl tbody').innerHTML = data.sold.length ? data.sold.map(r => `
      <tr>
        <td class="dimcell">${fmtDate(r.SoldDate)}</td>
        <td><a href="/deal/${r.ID}" style="color:inherit">${esc(r.Title)}</a>${Number(r.Quantity) > 1 ? ` <span class="chip hot">×${r.Quantity}</span>` : ''}</td>
        <td class="num"><b>${fmtGBP(r.Price)}</b></td>
        <td><a href="${safeUrl(r.URL)}" target="_blank" rel="noopener noreferrer">view</a></td>
      </tr>`).join('')
      : '<tr><td class="state" colspan="4">No recorded sales for this group yet.</td></tr>';
  } catch (e) {
    $('#sold-tbl tbody').innerHTML =
      `<tr><td class="state" colspan="4">Couldn’t load: ${esc(e.message)}</td></tr>`;
    $('#trend-chart').innerHTML = '';
  }
})();
