/* Prediction-model insight page: scatter of predicted vs actual, signed-error
   histogram, per-category / per-bucket accuracy, live premium ratios. */

function scatter(rows) {
  const pts = rows.filter(r => r.PredictedFinal > 0 && r.FinalPrice > 0);
  if (pts.length < 3) return '<div class="state">Not enough resolved predictions yet.</div>';
  const narrow = isNarrowScreen();
  const W = narrow ? 380 : 560, H = narrow ? 340 : 380, P = narrow ? 40 : 46;
  // log scale: deal prices span £10 heatsinks to £1500 GPUs
  const vals = pts.flatMap(r => [Number(r.PredictedFinal), Number(r.FinalPrice)]);
  const lo = Math.min(...vals) * 0.85, hi = Math.max(...vals) * 1.15;
  const lg = v => Math.log(v);
  const x = v => P + (lg(v) - lg(lo)) / (lg(hi) - lg(lo)) * (W - P - 14);
  const y = v => H - 30 - (lg(v) - lg(lo)) / (lg(hi) - lg(lo)) * (H - 44);
  let ticks = [10, 25, 50, 100, 250, 500, 1000, 2000].filter(t => t > lo && t < hi);
  if (narrow) ticks = ticks.filter((_, i) => i % 2 === 0);
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
    ${Object.entries(CAT_HUE).map(([c, hue], i) => {
      const sp = narrow ? 62 : 74, x0 = narrow ? P : P + 12;
      return `<circle cx="${x0 + i * sp}" cy="24" r="4" fill="${hue}"/>
      <text x="${x0 + 8 + i * sp}" y="27">${c}</text>`;
    }).join('')}
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

const TREND_MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const trendDay = iso => { const [, mo, da] = iso.split('-'); return `${+da} ${TREND_MON[+mo - 1]}`; };

function trend(series) {
  if (!series || series.length < 2)
    return '<div class="state">Need a couple of days of resolved deals to plot a trend.</div>';
  const narrow = isNarrowScreen();
  const W = narrow ? 380 : 720, H = narrow ? 200 : 200, P = narrow ? 34 : 40, PB = 26;
  const vals = series.flatMap(s => [s.median_abs_err_pct, s.baseline_median_abs_err_pct]);
  const hi = Math.max(...vals, 1) * 1.15;
  const x = i => P + (series.length === 1 ? 0.5 : i / (series.length - 1)) * (W - P - 14);
  const y = v => H - PB - v / hi * (H - PB - 16);
  const step = narrow && series.length > 5 ? 2 : 1;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="prediction error over time">
    <line class="axis" x1="${P}" y1="${H - PB}" x2="${W - 14}" y2="${H - PB}"/>
    ${[0.25, 0.5, 0.75, 1].map(f => `<text x="${P - 6}" y="${(y(hi * f) + 3).toFixed(1)}"
        text-anchor="end" class="faint">${Math.round(hi * f)}%</text>`).join('')}
    <polyline points="${series.map((s, i) => `${x(i).toFixed(1)},${y(s.baseline_median_abs_err_pct).toFixed(1)}`).join(' ')}"
      fill="none" stroke="var(--faint,#888)" stroke-width="1.5" stroke-dasharray="4 3"/>
    <polyline points="${series.map((s, i) => `${x(i).toFixed(1)},${y(s.median_abs_err_pct).toFixed(1)}`).join(' ')}"
      fill="none" stroke="var(--accent)" stroke-width="2"/>
    ${series.map((s, i) => `
      <circle cx="${x(i).toFixed(1)}" cy="${y(s.median_abs_err_pct).toFixed(1)}" r="3.5" fill="var(--accent)">
        <title>${trendDay(s.date)}: model ±${s.median_abs_err_pct}% vs baseline ±${s.baseline_median_abs_err_pct}% (n=${s.n})</title>
      </circle>
      ${i % step === 0 ? `<text x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle">${trendDay(s.date)}</text>` : ''}`).join('')}
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
    $('#trend').innerHTML = trend(d.over_time);

    $('#cat-tbl tbody').innerHTML = d.by_category.map(c => `
      <tr><td><span class="chip">${esc(c.category)}</span></td>
        <td class="num dimcell m-hide">${c.n}</td>
        <td class="num">±${c.median_abs_err_pct}%</td>
        <td class="num">${errCell(c.bias_pct)}</td>
        <td class="num dimcell m-hide">±${c.baseline_median_abs_err_pct}%</td>
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
        <td class="dimcell m-hide">${fmtDateTime(r.EndTime)}</td>
        <td><span class="chip">${esc(r.Category)}</span></td>
        <td><a href="/deal/${r.EbayID}" style="color:inherit">${esc(r.Model || '—')}</a></td>
        <td class="num dimcell m-hide">${fmtGBP(r.SurfacedPrice)}</td>
        <td class="num">${fmtGBP(r.PredictedFinal)}</td>
        <td class="num"><b>${fmtGBP(r.FinalPrice)}</b></td>
        <td class="num">${errCell(r.ErrPct)}</td>
      </tr>`).join('') || '<tr><td class="state" colspan="7">Nothing resolved yet.</td></tr>';
  } catch (e) {
    $('#stats').innerHTML = `<div class="state">Couldn’t load: ${esc(e.message)}</div>`;
  }
})();
