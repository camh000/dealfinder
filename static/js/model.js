/* Model detail: one market group's stats, monthly trend chart, live
   auctions, and the individual recent sales behind the median. */

const CAT = window.PAGE_CATEGORY;
const params = new URLSearchParams(location.search);

function chart(trend) {
  if (!trend || trend.length < 2)
    return '<div class="state">Not enough monthly history yet.</div>';
  const W = 720, H = 180, P = 34;
  const meds = trend.map(t => t.median);
  const lo = Math.min(...meds) * 0.92, hi = Math.max(...meds) * 1.05;
  const x = i => P + i / (trend.length - 1) * (W - P * 2);
  const y = v => H - 24 - (v - lo) / ((hi - lo) || 1) * (H - 48);
  const pts = trend.map((t, i) => `${x(i).toFixed(1)},${y(t.median).toFixed(1)}`).join(' ');
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="monthly median prices">
    <line class="axis" x1="${P}" y1="${H - 24}" x2="${W - P}" y2="${H - 24}"/>
    <polyline class="line" points="${pts}"/>
    ${trend.map((t, i) => `
      <circle class="pt" cx="${x(i).toFixed(1)}" cy="${y(t.median).toFixed(1)}" r="3">
        <title>${t.month}: ${fmtGBP(t.median)} (n=${t.n})</title></circle>
      <text x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle">${t.month.slice(2)}</text>
      <text x="${x(i).toFixed(1)}" y="${(y(t.median) - 8).toFixed(1)}" text-anchor="middle">${fmtGBP0(t.median)}</text>
    `).join('')}
  </svg>`;
}

(async () => {
  $('#model-title').textContent = groupLabel(CAT, Object.fromEntries(params));
  try {
    const res = await fetch(`/api/model-detail?type=${CAT}&${params.toString()}`);
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'error');

    const s = data.stats;
    $('#model-sub').textContent = `${CAT.toUpperCase()} market group`;
    $('#stats').innerHTML = `
      <div class="stat"><b class="num">${fmtGBP(s.median)}</b><span>median (120d)</span></div>
      <div class="stat"><b class="num">${fmtGBP0(s.min)}–${fmtGBP0(s.max)}</b><span>range</span></div>
      <div class="stat"><b class="num">${s.n}</b><span>sales in window</span></div>
      <div class="stat"><b class="num">${data.live.length}</b><span>live tracked</span></div>`;

    $('#trend-chart').innerHTML = chart(data.trend);

    if (data.live.length) {
      $('#live-card').style.display = '';
      $('#live-tbl tbody').innerHTML = data.live.map(l => `
        <tr>
          <td>${esc(l.Title)}${Number(l.Quantity) > 1 ? ` <span class="chip hot">×${l.Quantity}</span>` : ''}</td>
          <td class="num">${fmtGBP(l.ItemPrice)}${Number(l.Shipping) > 0 ? `<div class="dimcell">+${fmtGBP(l.Shipping)} del.</div>` : ''}</td>
          <td class="num dimcell">${l.Bids || 0}</td>
          <td><span class="countdown" ${endsAttr(l.EndTime)}></span></td>
          <td><a href="${safeUrl(l.URL)}" target="_blank" rel="noopener noreferrer">view</a></td>
        </tr>`).join('');
    }

    $('#sold-tbl tbody').innerHTML = data.sold.length ? data.sold.map(r => `
      <tr>
        <td class="dimcell">${fmtDate(r.SoldDate)}</td>
        <td>${esc(r.Title)}${Number(r.Quantity) > 1 ? ` <span class="chip hot">×${r.Quantity}</span>` : ''}</td>
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
