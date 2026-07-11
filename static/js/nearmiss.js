/* Near-miss experiment page: cohort comparison with confidence intervals,
   win rate by discount band, verdict + progress, recent near-misses. */

function bandChart(bands) {
  const drawn = bands.filter(b => b.resolved > 0);
  if (!drawn.length) return '<div class="state">No resolved outcomes in any band yet.</div>';
  const W = 640, H = 300, P = 40;
  const bw = (W - P - 16) / bands.length;
  const y = pct => H - 40 - (pct / 100) * (H - 84);
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="win rate by discount band">
    <line class="axis" x1="${P}" y1="${H - 40}" x2="${W - 16}" y2="${H - 40}"/>
    ${[0, 25, 50, 75, 100].map(t => `
      <text x="${P - 6}" y="${(y(t) + 3).toFixed(1)}" text-anchor="end">${t}%</text>`).join('')}
    ${bands.map((b, i) => {
      const cx = P + i * bw;
      const mid = cx + bw / 2;
      if (!b.resolved) return `
        <text x="${mid.toFixed(1)}" y="${(y(0) - 6).toFixed(1)}" text-anchor="middle">n=0</text>
        <text x="${mid.toFixed(1)}" y="${H - 22}" text-anchor="middle">${b.label}</text>`;
      const h = (b.win_rate / 100) * (H - 84);
      return `
        <rect class="bar ${b.near_miss_band ? 'hot' : ''}" x="${(cx + 6).toFixed(1)}"
          y="${y(b.win_rate).toFixed(1)}" width="${(bw - 12).toFixed(1)}" height="${h.toFixed(1)}">
          <title>${b.label}: ${b.win_rate}% win rate (${b.wins}/${b.resolved}), 95% CI ${b.wr_ci[0]}–${b.wr_ci[1]}%</title></rect>
        <line class="ci" x1="${mid.toFixed(1)}" y1="${y(b.wr_ci[0]).toFixed(1)}" x2="${mid.toFixed(1)}" y2="${y(b.wr_ci[1]).toFixed(1)}"/>
        <line class="ci" x1="${(mid - 5).toFixed(1)}" y1="${y(b.wr_ci[0]).toFixed(1)}" x2="${(mid + 5).toFixed(1)}" y2="${y(b.wr_ci[0]).toFixed(1)}"/>
        <line class="ci" x1="${(mid - 5).toFixed(1)}" y1="${y(b.wr_ci[1]).toFixed(1)}" x2="${(mid + 5).toFixed(1)}" y2="${y(b.wr_ci[1]).toFixed(1)}"/>
        <text x="${mid.toFixed(1)}" y="${(y(b.wr_ci[1]) - 6).toFixed(1)}" text-anchor="middle">${b.win_rate}%</text>
        <text x="${mid.toFixed(1)}" y="${H - 22}" text-anchor="middle">${b.label}</text>
        <text x="${mid.toFixed(1)}" y="${H - 8}" text-anchor="middle">n=${b.resolved}</text>`;
    }).join('')}
  </svg>`;
}

function verdictText(nm, main, target) {
  if (!nm.resolved) return 'No near-misses resolved yet — nothing to judge.';
  const overlap = nm.wr_ci[0] <= main.wr_ci[1] && main.wr_ci[0] <= nm.wr_ci[1];
  const gap = main.win_rate != null && nm.win_rate != null
    ? (main.win_rate - nm.win_rate).toFixed(1) : null;
  let call;
  if (nm.resolved < target) {
    call = `<b>Too early to call.</b> The intervals ${overlap ? 'overlap' : 'are already separating'} —
      judgement day is ~${target} resolved near-misses (${target - nm.resolved} to go).`;
  } else if (!overlap && nm.win_rate < main.win_rate) {
    call = `<b>The 20% bar is earning its keep</b> — near-misses genuinely win less, and the
      confidence intervals no longer overlap. Keep the threshold.`;
  } else if (overlap) {
    call = `<b>The bar may be too strict</b> — at n=${nm.resolved} the near-miss win rate is
      statistically indistinguishable from the main feed. Consider lowering
      SURFACE_MIN_DISCOUNT and letting the experiment re-run.`;
  } else {
    call = `<b>Surprising:</b> near-misses are winning MORE than the main feed — worth a closer look
      at what's different about them.`;
  }
  return `<p class="help" style="font-size:14px;margin:0">
    Near-misses win <b>${nm.win_rate}%</b> of the time (95% CI ${nm.wr_ci[0]}–${nm.wr_ci[1]}%, n=${nm.resolved})
    against the main feed's <b>${main.win_rate}%</b> (CI ${main.wr_ci[0]}–${main.wr_ci[1]}%, n=${main.resolved})${
    gap != null ? ` — a ${gap}-point gap` : ''}.<br><br>${call}</p>`;
}

(async () => {
  try {
    const res = await fetch('/api/insights/nearmiss');
    const d = await res.json();
    if (d.status !== 'ok') throw new Error(d.message || 'error');
    const nm = d.near_miss, main = d.main;

    const cell = (v, label, cls = '', title = '') =>
      `<div class="stat" ${title ? `title="${esc(title)}"` : ''}><b class="num ${cls}">${v}</b><span>${label}</span></div>`;
    $('#stats').innerHTML = [
      cell(nm.tracked, 'near-misses tracked'),
      cell(nm.resolved, 'resolved'),
      nm.win_rate != null ? cell(`${nm.win_rate}%`, 'near-miss win rate', '',
        `95% CI ${nm.wr_ci[0]}–${nm.wr_ci[1]}%`) : '',
      main.win_rate != null ? cell(`${main.win_rate}%`, 'main feed win rate', 'good',
        `95% CI ${main.wr_ci[0]}–${main.wr_ci[1]}% (n=${main.resolved})`) : '',
      cell(nm.pending, 'pending'),
      nm.median_actual_discount != null
        ? cell(`${nm.median_actual_discount}%`, 'median close vs market', '',
          'How far under market the resolved near-misses actually closed') : '',
    ].join('');

    $('#verdict-card').style.display = '';
    $('#verdict').innerHTML = verdictText(nm, main, d.target_n);
    const pct = Math.min(100, nm.resolved / d.target_n * 100);
    $('#progress').innerHTML = `
      <div class="posbar" style="height:10px"><div class="track" style="top:4px"></div>
        <div style="position:absolute;left:0;top:4px;height:3px;width:${pct}%;background:var(--accent);border-radius:2px"></div>
      </div>
      <div class="posbar-legend"><span>0</span><span>${nm.resolved} of ${d.target_n} resolved (judgement threshold)</span><span>${d.target_n}</span></div>`;

    $('#bands').innerHTML = bandChart(d.bands);

    $('#recent-tbl tbody').innerHTML = d.rows.slice(0, 40).map(r => `
      <tr>
        <td class="dimcell">${fmtDateTime(r.EndTime)}</td>
        <td><span class="chip">${esc(r.Category)}</span></td>
        <td><a href="/deal/${r.EbayID}" style="color:inherit">${esc(r.Model || '—')}</a></td>
        <td class="num">${r.DiscountPct}%</td>
        <td class="num dimcell">${fmtGBP(r.AvgMarketPrice)}</td>
        <td class="num">${r.FinalPrice != null && r.result !== 'pending' ? `<b>${fmtGBP(r.FinalPrice)}</b>` : '—'}</td>
        <td>${r.result === 'win' ? '<span class="verdict win">WIN</span>'
             : r.result === 'miss' ? '<span class="verdict miss">MISS</span>'
             : `<span class="dimcell">${esc(r.result)}</span>`}</td>
      </tr>`).join('') || '<tr><td class="state" colspan="7">No near-misses recorded yet.</td></tr>';
  } catch (e) {
    $('#stats').innerHTML = `<div class="state">Couldn’t load: ${esc(e.message)}</div>`;
  }
})();
