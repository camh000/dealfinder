/* Prediction-model insight page: scatter of predicted vs actual, signed-error
   histogram, per-category / per-bucket accuracy, live premium ratios. */

function scatter(rows) {
  const pts = rows.filter(r => r.PredictedFinal > 0 && r.FinalPrice > 0);
  if (pts.length < 3) return '<div class="state">Not enough resolved predictions yet.</div>';
  const W = 560, H = 380, P = 46;
  // log scale: deal prices span £10 heatsinks to £1500 GPUs
  const vals = pts.flatMap(r => [Number(r.PredictedFinal), Number(r.FinalPrice)]);
  const lo = Math.min(...vals) * 0.85, hi = Math.max(...vals) * 1.15;
  const lg = v => Math.log(v);
  const x = v => P + (lg(v) - lg(lo)) / (lg(hi) - lg(lo)) * (W - P - 14);
  const y = v => H - 30 - (lg(v) - lg(lo)) / (lg(hi) - lg(lo)) * (H - 44);
  const ticks = [10, 25, 50, 100, 250, 500, 1000, 2000].filter(t => t > lo && t < hi);
  const CAT_HUE = { GPU: 'var(--accent)', CPU: 'var(--gain)', HDD: 'var(--warn)', SSD: 'var(--loss)', RAM: 'var(--faint)' };
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="predicted vs actual">
    <line class="axis" x1="${P}" y1="${H - 30}" x2="${W - 14}" y2="${H - 30}"/>
    <line class="axis" x1="${P}" y1="14" x2="${P}" y2="${H - 30}"/>
    <line class="diag" x1="${x(lo)}" y1="${y(lo)}" x2="${x(hi)}" y2="${y(hi)}"/>
    ${ticks.map(t => `
      <text x="${x(t).toFixed(1)}" y="${H - 14}" text-anchor="middle">£${t}</text>
      <text x="${P - 6}" y="${(y(t) + 3).toFixed(1)}" text-anchor="end">£${t}</text>`).join('')}
    <text x="${W / 2}" y="${H - 2}" text-anchor="middle">predicted final</text>
    <text x="12" y="${H / 2}" text-anchor="middle" transform="rotate(-90 12 ${H / 2})">actual final</text>
    ${pts.map(r => `
      <a href="/deal/${r.EbayID}"><circle class="scpt" cx="${x(r.PredictedFinal).toFixed(1)}"
        cy="${y(r.FinalPrice).toFixed(1)}" r="4" fill="${CAT_HUE[r.Category] || 'var(--accent)'}">
        <title>${esc(r.Model || r.Category)} — predicted ${fmtGBP(r.PredictedFinal)}, closed ${fmtGBP(r.FinalPrice)} (${r.ErrPct > 0 ? '+' : ''}${r.ErrPct}%)</title>
      </circle></a>`).join('')}
    ${Object.entries(CAT_HUE).map(([c, hue], i) => `
      <circle cx="${P + 12 + i * 74}" cy="24" r="4" fill="${hue}"/>
      <text x="${P + 20 + i * 74}" y="27">${c}</text>`).join('')}
  </svg>`;
}

function histogram(bins) {
  const max = Math.max(...bins.map(b => b.n), 1);
  const W = 400, H = 300, P = 30;
  const bw = (W - P - 10) / bins.length;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="error distribution">
    <line class="axis" x1="${P}" y1="${H - 26}" x2="${W - 10}" y2="${H - 26}"/>
    ${bins.map((b, i) => {
      const h = b.n / max * (H - 60);
      const cx = P + i * bw;
      const zero = b.lo <= 0 && b.hi > 0;
      return `<rect class="bar ${zero ? 'hot' : ''}" x="${(cx + 2).toFixed(1)}" y="${(H - 26 - h).toFixed(1)}"
          width="${(bw - 4).toFixed(1)}" height="${h.toFixed(1)}">
          <title>${b.lo}% to ${b.hi}%: ${b.n} deal(s)</title></rect>
        ${b.n ? `<text x="${(cx + bw / 2).toFixed(1)}" y="${(H - 32 - h).toFixed(1)}" text-anchor="middle">${b.n}</text>` : ''}
        ${i % 2 === 0 ? `<text x="${cx.toFixed(1)}" y="${H - 10}" text-anchor="middle">${b.lo > 0 ? '+' : ''}${b.lo}%</text>` : ''}`;
    }).join('')}
  </svg>`;
}

const errCell = v => v == null ? '—'
  : `<span style="color:${Math.abs(v) <= 2 ? 'var(--gain)' : Math.abs(v) >= 10 ? 'var(--warn)' : 'inherit'}">${v > 0 ? '+' : ''}${v}%</span>`;

(async () => {
  try {
    const res = await fetch('/api/insights/predictions');
    const d = await res.json();
    if (d.status !== 'ok') throw new Error(d.message || 'error');

    const o = d.overall;
    $('#stats').innerHTML = o.n ? `
      <div class="stat"><b class="num">${o.n}</b><span>resolved predictions</span></div>
      <div class="stat"><b class="num">±${o.median_abs_err_pct}%</b><span>median error</span></div>
      <div class="stat" title="median signed error — positive means auctions close above the calls">
        <b class="num ${Math.abs(o.bias_pct) <= 3 ? 'good' : 'warn'}">${o.bias_pct > 0 ? '+' : ''}${o.bias_pct}%</b><span>bias</span></div>
      <div class="stat" title="error if we'd just assumed the final price equals the surfaced price">
        <b class="num">±${o.baseline_median_abs_err_pct}%</b><span>no-model error</span></div>
      <div class="stat"><b class="num">${o.within_10_pct}%</b><span>within ±10%</span></div>
      <div class="stat"><b class="num">${o.within_20_pct}%</b><span>within ±20%</span></div>`
      : '<div class="state">No resolved predictions yet — check back once tracked deals close.</div>';

    $('#scatter').innerHTML = scatter(d.rows);
    $('#hist').innerHTML = histogram(d.histogram);

    $('#cat-tbl tbody').innerHTML = d.by_category.map(c => `
      <tr><td><span class="chip">${esc(c.category)}</span></td>
        <td class="num dimcell">${c.n}</td>
        <td class="num">±${c.median_abs_err_pct}%</td>
        <td class="num">${errCell(c.bias_pct)}</td>
        <td class="num dimcell">±${c.baseline_median_abs_err_pct}%</td>
        <td>${c.median_abs_err_pct <= c.baseline_median_abs_err_pct
              ? '<span class="verdict win">BEATS IT</span>' : '<span class="verdict miss">BEHIND</span>'}</td>
      </tr>`).join('') || '<tr><td class="state" colspan="6">No data yet.</td></tr>';

    $('#bucket-tbl tbody').innerHTML = d.by_bucket.map(b => `
      <tr><td>${esc(b.bucket)} bid${b.bucket === '1-3' || b.bucket === '4+' ? 's' : ''}</td>
        <td class="num dimcell">${b.n}</td>
        <td class="num">±${b.median_abs_err_pct}%</td>
        <td class="num">${errCell(b.bias_pct)}</td></tr>`).join('')
      || '<tr><td class="state" colspan="4">No data yet.</td></tr>';

    $('#ratio-tbl tbody').innerHTML = d.ratios.map(r => `
      <tr><td><span class="chip">${esc(r.category)}</span></td>
        <td class="dimcell">${esc(r.bucket)}</td>
        <td class="num"><b>×${r.ratio.toFixed(2)}</b></td>
        <td class="num dimcell">${r.n}</td></tr>`).join('')
      || '<tr><td class="state" colspan="4">No premium history yet.</td></tr>';

    $('#recent-tbl tbody').innerHTML = d.rows.slice(0, 40).map(r => `
      <tr>
        <td class="dimcell">${fmtDateTime(r.EndTime)}</td>
        <td><span class="chip">${esc(r.Category)}</span></td>
        <td><a href="/deal/${r.EbayID}" style="color:inherit">${esc(r.Model || '—')}</a></td>
        <td class="num dimcell">${fmtGBP(r.SurfacedPrice)}</td>
        <td class="num">${fmtGBP(r.PredictedFinal)}</td>
        <td class="num"><b>${fmtGBP(r.FinalPrice)}</b></td>
        <td class="num">${errCell(r.ErrPct)}</td>
      </tr>`).join('') || '<tr><td class="state" colspan="7">Nothing resolved yet.</td></tr>';
  } catch (e) {
    $('#stats').innerHTML = `<div class="state">Couldn’t load: ${esc(e.message)}</div>`;
  }
})();
