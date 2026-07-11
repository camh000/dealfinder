/* Outcomes page: stat strip (incl. prediction accuracy, lifetime, near-miss
   control cohort), category filter, resolved + pending tables. */

let resolved = [], pending = [], summary = null;
let cat = 'all';
let rSort = { col: 'EndTime', asc: false }, pSort = { col: 'EndTime', asc: true };

const inCat = r => cat === 'all' || (r.Category || '').toLowerCase() === cat;

function statStrip() {
  const res = resolved.filter(inCat);
  const pen = pending.filter(inCat);
  const priced = res.filter(r => !r.EndedUnsold && r.FinalPrice != null);
  const beat = priced.filter(r => Number(r.FinalPrice) < Number(r.AvgMarketPrice)).length;
  const wr = priced.length ? (beat / priced.length * 100).toFixed(1) : '0';
  const all = cat === 'all';
  const gaveUp = all ? (summary?.gave_up || 0) : 0;
  const nm = all ? summary?.near_miss : null;
  const pred = all ? summary?.prediction : null;
  const life = all ? summary?.lifetime : null;
  const cell = (v, label, cls = '', title = '', href = '') => {
    const body = `<b class="${cls} num">${v}</b><span>${label}${href ? ' ↗' : ''}</span>`;
    return `<div class="stat" ${title ? `title="${esc(title)}"` : ''}>${
      href ? `<a href="${href}">${body}</a>` : body}</div>`;
  };
  $('#stats').innerHTML = [
    cell(priced.length + pen.length + gaveUp, 'tracked'),
    cell(priced.length, 'resolved'),
    cell(`${wr}%`, 'win rate', wr >= 50 ? 'good' : 'warn',
         `${beat} of ${priced.length} sold below their market median`),
    cell(pen.length, 'pending'),
    gaveUp ? cell(gaveUp, 'gave up', 'warn',
      'Unresolvable — the listing left eBay search before it could be verified') : '',
    nm && nm.resolved > 0 ? cell(`${nm.win_rate}%`, 'near-miss WR', '',
      `Control cohort: 12–20%-off listings, recorded but never surfaced (n=${nm.resolved}). Click for the full experiment.`,
      '/insights/nearmiss') : '',
    pred && pred.n >= 5 ? cell(`±${pred.median_abs_err_pct}%`, 'prediction error', '',
      `Median gap between the predicted final price and reality, over ${pred.n} resolved deals. Click for the full model report.`,
      '/insights/predictions') : '',
    life && life.market_value_tracked ? cell(fmtGBP0(life.market_value_tracked), 'value tracked', '',
      life.median_actual_discount != null
        ? `Lifetime market value of tracked deals — median closed ${life.median_actual_discount}% under market`
        : '') : '',
  ].join('');
}

const th = (label, col, sort, fn, num = false) =>
  `<th class="sortable ${num ? 'num' : ''}" onclick="${fn}('${col}')">${label}${
    sort.col === col ? `<span class="arrow">${sort.asc ? '▲' : '▼'}</span>` : ''}</th>`;

function renderResolved() {
  const rows = resolved.filter(r => !r.EndedUnsold && inCat(r));
  rows.forEach(r => {
    r.SavingGbp = (r.FinalPrice != null && r.AvgMarketPrice != null)
      ? Number(r.AvgMarketPrice) - Number(r.FinalPrice) : null;
  });
  $('#resolved-tbl thead').innerHTML = `<tr>
    ${th('Ended', 'EndTime', rSort, 'sortR')}
    <th>Cat</th><th>Model</th>
    ${th('Surfaced @', 'SurfacedPrice', rSort, 'sortR', true)}
    ${th('Market', 'AvgMarketPrice', rSort, 'sortR', true)}
    ${th('Predicted', 'PredictedFinal', rSort, 'sortR', true)}
    ${th('Final', 'FinalPrice', rSort, 'sortR', true)}
    ${th('Saving', 'SavingGbp', rSort, 'sortR', true)}
    <th></th><th></th></tr>`;
  const body = $('#resolved-tbl tbody');
  if (!rows.length) {
    body.innerHTML = `<tr><td class="state" colspan="10">No resolved ${cat === 'all' ? '' : cat.toUpperCase() + ' '}deals yet.</td></tr>`;
    return;
  }
  body.innerHTML = sortRows(rows, rSort.col, rSort.asc).map(r => {
    const win = Number(r.FinalPrice) < Number(r.AvgMarketPrice);
    const s = r.SavingGbp ?? 0;
    const predCell = r.PredictedFinal != null
      ? `${fmtGBP(r.PredictedFinal)}<div class="dimcell">${
          Math.abs(r.FinalPrice - r.PredictedFinal) / r.PredictedFinal <= 0.10 ? '✓ close' :
          r.FinalPrice > r.PredictedFinal ? 'under-called' : 'over-called'}</div>`
      : '<span class="dimcell">—</span>';
    return `<tr>
      <td class="dimcell">${fmtDateTime(r.EndTime)}</td>
      <td><span class="chip">${esc(r.Category)}</span></td>
      <td><a href="/deal/${r.EbayID}" style="color:inherit">${esc(r.Model || '—')}</a></td>
      <td class="num">${fmtGBP(r.SurfacedPrice)}<div class="dimcell">${r.SurfacedDiscountPct}% off</div></td>
      <td class="num dimcell">${fmtGBP(r.AvgMarketPrice)}</td>
      <td class="num">${predCell}</td>
      <td class="num"><b>${fmtGBP(r.FinalPrice)}</b><div class="dimcell">${
        r.ActualDiscountPct > 0 ? r.ActualDiscountPct + '% off mkt' : Math.abs(r.ActualDiscountPct).toFixed(1) + '% over'}</div></td>
      <td class="num" style="color:${s > 0 ? 'var(--gain)' : 'var(--loss)'}">${s >= 0 ? '+' : '−'}${fmtGBP(Math.abs(s))}</td>
      <td><span class="verdict ${win ? 'win' : 'miss'}">${win ? 'DEAL' : 'MISS'}</span></td>
      <td><a href="${safeUrl(r.URL)}" target="_blank" rel="noopener noreferrer">view</a></td>
    </tr>`;
  }).join('');
}

function renderPending() {
  const rows = pending.filter(inCat);
  $('#pending-section').style.display = rows.length ? '' : 'none';
  if (!rows.length) return;
  $('#pending-tbl thead').innerHTML = `<tr>
    ${th('Spotted', 'SurfacedAt', pSort, 'sortP')}
    <th>Cat</th><th>Model</th>
    ${th('Surfaced @', 'SurfacedPrice', pSort, 'sortP', true)}
    ${th('Market', 'AvgMarketPrice', pSort, 'sortP', true)}
    ${th('Current', 'CurrentPrice', pSort, 'sortP', true)}
    ${th('Ends', 'EndTime', pSort, 'sortP')}
    <th></th></tr>`;
  $('#pending-tbl tbody').innerHTML = sortRows(rows, pSort.col, pSort.asc).map(r => {
    const ended = !r.EndTime || new Date(r.EndTime) <= Date.now();
    return `<tr>
      <td class="dimcell">${fmtDateTime(r.SurfacedAt)}</td>
      <td><span class="chip">${esc(r.Category)}</span></td>
      <td><a href="/deal/${r.EbayID}" style="color:inherit">${esc(r.Model || '—')}</a></td>
      <td class="num">${fmtGBP(r.SurfacedPrice)}<div class="dimcell">${r.SurfacedDiscountPct}% off</div></td>
      <td class="num dimcell">${fmtGBP(r.AvgMarketPrice)}</td>
      <td class="num">${fmtGBP(r.CurrentPrice)}<div class="dimcell">${r.CurrentBids || 0} bids</div></td>
      <td>${ended ? '<span class="dimcell">awaiting result</span>' : `<span class="countdown" ${endsAttr(r.EndTime)}></span>`}</td>
      <td><a href="${safeUrl(r.URL)}" target="_blank" rel="noopener noreferrer">view</a></td>
    </tr>`;
  }).join('');
}

window.sortR = col => { rSort = rSort.col === col ? { col, asc: !rSort.asc } : { col, asc: false }; renderResolved(); };
window.sortP = col => { pSort = pSort.col === col ? { col, asc: !pSort.asc } : { col, asc: col === 'EndTime' }; renderPending(); };

$$('#cat-pills .pill').forEach(p => p.addEventListener('click', () => {
  cat = p.dataset.cat;
  $$('#cat-pills .pill').forEach(x => x.classList.toggle('active', x === p));
  statStrip(); renderResolved(); renderPending();
}));

(async () => {
  try {
    const res = await fetch('/api/outcomes');
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'error');
    ({ resolved, pending } = data);
    summary = data.summary;
    statStrip(); renderResolved(); renderPending();
  } catch (e) {
    $('#resolved-tbl tbody').innerHTML =
      `<tr><td class="state" colspan="10">Couldn’t load outcomes: ${esc(e.message)}</td></tr>`;
  }
})();
