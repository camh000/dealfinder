/* Health page: last scrape, per-category data volumes, last-run coverage. */

(async () => {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'error');

    const ageMin = data.last_scrape_at
      ? Math.floor((Date.now() - new Date(data.last_scrape_at).getTime()) / 60000) : null;
    const o = data.outcomes;
    $('#stats').innerHTML = `
      <div class="stat"><b class="num ${ageMin != null && ageMin < 90 ? 'good' : 'warn'}">${
        ageMin != null ? timeAgo(data.last_scrape_at) : '—'}</b><span>last full scrape</span></div>
      <div class="stat"><b class="num">${o.pending}</b><span>deals pending</span></div>
      <div class="stat"><b class="num">${o.resolved}</b><span>resolved</span></div>
      <div class="stat"><b class="num">${o.near_miss}</b><span>near-miss cohort</span></div>
      <div class="stat"><b class="num">${o.gave_up}</b><span>gave up</span></div>
      <div class="stat"><b class="num">${data.snapshots.rows.toLocaleString('en-GB')}</b><span>price snapshots (${data.snapshots.deals} deals)</span></div>`;

    const cats = Object.entries(data.categories);
    const tot = cats.reduce((a, [, v]) => {
      a.live += v.live; a.sold_window += v.sold_window; a.sold_total += v.sold_total; return a;
    }, { live: 0, sold_window: 0, sold_total: 0 });
    const n = v => v.toLocaleString('en-GB');
    $('#cat-tbl tbody').innerHTML = cats.map(([c, v]) => `
      <tr><td><a href="/deals/${c}"><span class="chip" style="cursor:pointer">${c.toUpperCase()}</span></a></td>
        <td class="num">${n(v.live)}</td>
        <td class="num">${n(v.sold_window)}</td>
        <td class="num dimcell">${n(v.sold_total)}</td></tr>`).join('') + `
      <tr style="border-top:2px solid var(--border);font-weight:650">
        <td><b>Total</b></td>
        <td class="num"><b>${n(tot.live)}</b></td>
        <td class="num"><b>${n(tot.sold_window)}</b></td>
        <td class="num dimcell"><b>${n(tot.sold_total)}</b></td></tr>`;

    if (data.last_run) {
      const r = data.last_run;
      $('#run-card').style.display = '';
      const cov = r.coverage || {};
      const pct = (a, b) => b ? Math.round(a / b * 100) + '%' : '—';
      $('#run-body').innerHTML = `
        <p class="help">${r.rows?.toLocaleString('en-GB') ?? '?'} rows touched ·
          ${r.categories_ok ?? '?'}/5 categories succeeded</p>
        ${r.alerts?.length
          ? `<p class="help" style="color:var(--loss)">⚠ ${r.alerts.map(esc).join('<br>⚠ ')}</p>`
          : '<p class="help" style="color:var(--gain)">All field-coverage checks passed.</p>'}
        <div class="stats" style="margin:0">
          <div class="stat"><b class="num">${pct(cov.sold_date, cov.sold_items)}</b><span>sold dates</span></div>
          <div class="stat"><b class="num">${pct(cov.end_time, cov.active_items)}</b><span>end times</span></div>
          <div class="stat"><b class="num">${pct(cov.feedback, cov.items)}</b><span>seller feedback</span></div>
          <div class="stat"><b class="num">${pct(cov.shipping, cov.items)}</b><span>paid shipping</span></div>
          <div class="stat"><b class="num">${pct(cov.bids, cov.active_items)}</b><span>items with bids</span></div>
        </div>`;
    }
  } catch (e) {
    $('#stats').innerHTML = `<div class="state">Couldn’t load health: ${esc(e.message)}</div>`;
  }
})();
