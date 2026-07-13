/* Prediction-surfacing experiment: do the deals the model predicts will close
   >= margin under median actually win clearly more than the deals it predicts
   below that margin? If so, predicted margin alone can drive surfacing. */

const overlaps = (a, b) => a.wr_ci[0] <= b.wr_ci[1] && b.wr_ci[0] <= a.wr_ci[1];

function verdictText(pred, skipped, main, target, margin) {
  if (!pred.resolved) return `No deals predicted ≥${margin}% under median have resolved yet — nothing to judge.`;
  const line = c => c.win_rate != null
    ? `<b>${c.win_rate}%</b> (95% CI ${c.wr_ci[0]}–${c.wr_ci[1]}%, n=${c.resolved})`
    : `— (n=${c.resolved})`;
  let call;
  if (pred.resolved < target) {
    call = `<b>Too early to call.</b> Judgement day is ~${target} resolved predicted-margin deals
      (${target - pred.resolved} to go).`;
  } else {
    const beats = skipped.win_rate == null || skipped.resolved < 10
      || (!overlaps(pred, skipped) && pred.win_rate > skipped.win_rate);
    if (beats) {
      call = `<b>The prediction separates winners from losers.</b> Deals predicted ≥${margin}% under
        median win clearly more than the ones the model predicted <em>below</em> that — so
        "surface anything predicted ≥${margin}% under median" is a usable rule. Consider dropping the
        current-discount floor to a low recording level and gating the feed on predicted margin.`;
    } else {
      call = `<b>Not discriminating yet.</b> Deals predicted ≥${margin}% under median win about the
        same as the ones predicted below it, so the predicted-margin flag isn't adding signal — the
        model can't yet be trusted as the surfacing rule.`;
    }
  }
  return `<p class="help" style="font-size:14px;margin:0">
    Predicted ≥${margin}% under median (the rule surfaces): ${line(pred)}.<br>
    Predicted below that (the rule skips): ${line(skipped)}.<br>
    Live feed, same window (reference): ${line(main)}.<br><br>${call}</p>`;
}

(async () => {
  try {
    const d = await fetch('/api/insights/predsurface').then(r => r.json());
    if (d.status !== 'ok') throw new Error(d.message || 'error');
    const { pred, main, skipped, margin, target_n } = d;

    const cell = (v, label, cls = '', title = '') =>
      `<div class="stat" ${title ? `title="${esc(title)}"` : ''}><b class="num ${cls}">${v}</b><span>${label}</span></div>`;
    $('#stats').innerHTML = [
      cell(pred.tracked, `predicted ≥${margin}% tracked`),
      cell(pred.resolved, 'resolved'),
      pred.win_rate != null ? cell(`${pred.win_rate}%`, `predicted ≥${margin}% WR`, 'good',
        `95% CI ${pred.wr_ci[0]}–${pred.wr_ci[1]}%`) : '',
      skipped.win_rate != null ? cell(`${skipped.win_rate}%`, `predicted <${margin}% WR`, 'warn',
        `Deals the model predicted below the margin — the set the rule skips (n=${skipped.resolved})`) : '',
      main.win_rate != null ? cell(`${main.win_rate}%`, 'live feed WR (same period)', '',
        `The current feed over the same window, for reference (n=${main.resolved})`) : '',
      pred.median_actual_discount != null
        ? cell(`${pred.median_actual_discount}%`, 'median close vs market') : '',
    ].join('');

    $('#verdict-card').style.display = '';
    $('#verdict').innerHTML = verdictText(pred, skipped, main, target_n, margin);
    const pct = Math.min(100, pred.resolved / target_n * 100);
    $('#progress').innerHTML = `
      <div class="posbar" style="height:10px"><div class="track" style="top:4px"></div>
        <div style="position:absolute;left:0;top:4px;height:3px;width:${pct}%;background:var(--accent);border-radius:2px"></div>
      </div>
      <div class="posbar-legend"><span>0</span><span>${pred.resolved} of ${target_n} resolved</span><span>${target_n}</span></div>`;

    $('#recent-tbl tbody').innerHTML = d.rows.slice(0, 40).map(r => `
      <tr>
        <td class="dimcell m-hide">${fmtDateTime(r.EndTime)}</td>
        <td><span class="chip">${esc(r.Category)}</span></td>
        <td>${esc(r.Model || '—')}</td>
        <td class="num">${r.DiscountPct}%</td>
        <td class="num dimcell m-hide">${fmtGBP(r.AvgMarketPrice)}</td>
        <td class="num">${r.FinalPrice != null && r.result !== 'pending' ? `<b>${fmtGBP(r.FinalPrice)}</b>` : '—'}</td>
        <td>${r.result === 'win' ? '<span class="verdict win">WIN</span>'
             : r.result === 'miss' ? '<span class="verdict miss">MISS</span>'
             : `<span class="dimcell">${esc(r.result)}</span>`}</td>
      </tr>`).join('') || '<tr><td class="state" colspan="7">No model-flagged deals recorded yet.</td></tr>';
  } catch (e) {
    $('#stats').innerHTML = `<div class="state">Couldn’t load: ${esc(e.message)}</div>`;
  }
})();
