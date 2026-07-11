/* Price guide: searchable market medians with 30-day trend, live-listing
   counts linking to model pages, and a persistent build basket. */

let guide = null, cat = 'all';
let sort = { col: 'AvgPrice', asc: false };
let basket = JSON.parse(localStorage.getItem('pcd-basket') || '[]');

/* ── context-aware filters: appear under the pills once a type is picked ──
   Each entry: dropdown options + a row predicate. Options are [value, label]
   pairs; '' = any. State lives in `ctx` and resets on category switch. */
const CTX_FILTERS = {
  gpu: [
    { key: 'series', label: 'Series',
      opts: [['RTX', 'RTX'], ['GTX', 'GTX'], ['RX', 'Radeon RX'], ['ARC', 'Intel Arc']],
      match: (r, v) => (r.Model || '').toUpperCase().startsWith(v) },
  ],
  cpu: [
    { key: 'family', label: 'Family',
      opts: [['i3', 'Core i3'], ['i5', 'Core i5'], ['i7', 'Core i7'], ['i9', 'Core i9'],
             ['ryzen 3', 'Ryzen 3'], ['ryzen 5', 'Ryzen 5'], ['ryzen 7', 'Ryzen 7'],
             ['ryzen 9', 'Ryzen 9'], ['xeon', 'Xeon']],
      match: (r, v) => (r.Model || '').toLowerCase().includes(v) },
  ],
  hdd: [
    { key: 'iface', label: 'Interface', opts: [['SATA', 'SATA'], ['SAS', 'SAS']],
      match: (r, v) => (r.Interface || 'SATA') === v },
    { key: 'type', label: 'Type', opts: [['Internal', 'Internal'], ['External', 'External']],
      match: (r, v) => (r.DriveType || 'Internal') === v },
    { key: 'mincap', label: 'Min size',
      opts: [['1000', '1TB+'], ['4000', '4TB+'], ['8000', '8TB+'], ['12000', '12TB+']],
      match: (r, v) => Number(r.CapacityGB) >= Number(v) },
  ],
  ssd: [
    { key: 'iface', label: 'Interface', opts: [['NVMe', 'NVMe'], ['SATA', 'SATA'], ['USB', 'USB / portable']],
      match: (r, v) => (r.Interface || '') === v },
    { key: 'mincap', label: 'Min size',
      opts: [['500', '500GB+'], ['1000', '1TB+'], ['2000', '2TB+'], ['4000', '4TB+']],
      match: (r, v) => Number(r.CapacityGB) >= Number(v) },
  ],
  ram: [
    { key: 'type', label: 'Type', opts: [['DDR3', 'DDR3'], ['DDR4', 'DDR4'], ['DDR5', 'DDR5']],
      match: (r, v) => (r.Type || '') === v },
    { key: 'ff', label: 'Form', opts: [['DIMM', 'Desktop (DIMM)'], ['SODIMM', 'Laptop (SODIMM)']],
      match: (r, v) => (r.FormFactor || 'DIMM') === v },
    { key: 'kit', label: 'Kit',
      opts: [['1x', 'single stick'], ['2x', '2-stick kit'], ['4x', '4-stick kit'], ['?', 'unstated']],
      match: (r, v) => v === '?' ? !r.KitConfig : (r.KitConfig || '').toLowerCase().startsWith(v) },
    { key: 'mincap', label: 'Min size',
      opts: [['8', '8GB+'], ['16', '16GB+'], ['32', '32GB+'], ['64', '64GB+']],
      match: (r, v) => Number(r.CapacityGB) >= Number(v) },
  ],
};
let ctx = {};

function renderCtxFilters() {
  const defs = CTX_FILTERS[cat];
  const bar = $('#ctx-filters');
  if (!defs) { bar.style.display = 'none'; bar.innerHTML = ''; return; }
  bar.style.display = '';
  bar.innerHTML = defs.map(f => `
    <label>${f.label}
      <select data-ctx="${f.key}">
        <option value="">any</option>
        ${f.opts.map(([v, l]) => `<option value="${esc(v)}"${ctx[f.key] === v ? ' selected' : ''}>${esc(l)}</option>`).join('')}
      </select>
    </label>`).join('');
  $$('#ctx-filters select').forEach(sel => sel.addEventListener('change', () => {
    if (sel.value) ctx[sel.dataset.ctx] = sel.value; else delete ctx[sel.dataset.ctx];
    render();
  }));
}

function rowsFlat() {
  const cats = cat === 'all' ? ['gpu', 'cpu', 'hdd', 'ssd', 'ram'] : [cat];
  let rows = [];
  for (const c of cats)
    for (const r of (guide[c] || []))
      rows.push({ ...r, _cat: c, _label: groupLabel(c, r),
                  AvgPrice: Number(r.AvgPrice), MinPrice: Number(r.MinPrice),
                  MaxPrice: Number(r.MaxPrice) });
  const q = $('#q').value.trim().toLowerCase();
  if (q) rows = rows.filter(r => r._label.toLowerCase().includes(q));
  const defs = CTX_FILTERS[cat] || [];
  for (const f of defs)
    if (ctx[f.key]) rows = rows.filter(r => f.match(r, ctx[f.key]));
  return sortRows(rows, sort.col, sort.asc);
}

const th = (label, col, num = false) =>
  `<th class="sortable ${num ? 'num' : ''}" data-sort="${col}">${label}${
    sort.col === col ? `<span class="arrow">${sort.asc ? '▲' : '▼'}</span>` : ''}</th>`;

function trendCell(r) {
  if (r.Trend30dPct == null) return '<span class="trend-flat">—</span>';
  const v = Number(r.Trend30dPct);
  const cls = v > 3 ? 'trend-up' : v < -3 ? 'trend-down' : 'trend-flat';
  const sym = v > 3 ? '↑' : v < -3 ? '↓' : '→';
  return `<span class="${cls}" title="median last 30d vs prior 60d (n=${r.TrendSamples})">${sym} ${v > 0 ? '+' : ''}${v}%</span>`;
}

function render() {
  $('#guide-tbl thead').innerHTML = `<tr>
    <th>Cat</th>${th('Model / spec', '_label')}
    ${th('Median', 'AvgPrice', true)}${th('Range', 'MinPrice', true)}
    ${th('30d trend', 'Trend30dPct', true)}${th('Sales', 'SoldCount', true)}
    ${th('Live', 'LiveCount', true)}<th></th></tr>`;
  $$('#guide-tbl th.sortable').forEach(el => el.onclick = () => {
    const col = el.dataset.sort;
    sort = sort.col === col ? { col, asc: !sort.asc } : { col, asc: col === '_label' };
    render();
  });
  const rows = rowsFlat();
  const body = $('#guide-tbl tbody');
  if (!rows.length) { body.innerHTML = '<tr><td class="state" colspan="8">No components match.</td></tr>'; return; }
  body.innerHTML = rows.map((r, i) => `
    <tr class="${r.SoldCount < 5 ? 'low-conf' : ''}">
      <td><span class="chip">${r._cat.toUpperCase()}</span></td>
      <td><a href="${modelHref(r._cat, r)}">${esc(r._label)}</a></td>
      <td class="num"><b>${fmtGBP(r.AvgPrice)}</b></td>
      <td class="num dimcell">${fmtGBP0(r.MinPrice)}–${fmtGBP0(r.MaxPrice)}</td>
      <td class="num">${trendCell(r)}</td>
      <td class="num dimcell">${r.SoldCount}</td>
      <td class="num">${r.LiveCount > 0 ? `<a href="${modelHref(r._cat, r)}#live">${r.LiveCount}</a>` : '<span class="dimcell">0</span>'}</td>
      <td><button class="add-basket" data-i="${i}" title="add to build basket">+</button></td>
    </tr>`).join('');
  $$('#guide-tbl .add-basket').forEach(b => b.onclick = () => {
    const r = rows[Number(b.dataset.i)];
    basket.push({ label: r._label, price: r.AvgPrice });
    saveBasket();
  });
}

function saveBasket() {
  localStorage.setItem('pcd-basket', JSON.stringify(basket));
  renderBasket();
}
function renderBasket() {
  const box = $('#basket-items');
  if (!basket.length) {
    box.innerHTML = '<div class="state" style="padding:14px;font-size:12.5px">Add components with +</div>';
  } else {
    box.innerHTML = basket.map((b, i) => `
      <div class="bi"><span>${esc(b.label)}</span>
        <span class="num">${fmtGBP(b.price)} <button data-i="${i}" title="remove">×</button></span></div>`).join('');
    $$('#basket-items button').forEach(btn => btn.onclick = () => {
      basket.splice(Number(btn.dataset.i), 1); saveBasket();
    });
  }
  $('#basket-total').textContent = fmtGBP(basket.reduce((s, b) => s + Number(b.price), 0));
}

$('#basket-clear').onclick = () => { basket = []; saveBasket(); };
$('#q').addEventListener('input', () => render());
$$('#cat-pills .pill').forEach(p => p.addEventListener('click', () => {
  cat = p.dataset.cat;
  ctx = {};                       // filters are per-type — reset on switch
  $$('#cat-pills .pill').forEach(x => x.classList.toggle('active', x === p));
  renderCtxFilters();
  render();
}));

(async () => {
  renderBasket();
  try {
    const res = await fetch('/api/price-guide');
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'error');
    guide = data.components;
    render();
  } catch (e) {
    $('#guide-tbl tbody').innerHTML =
      `<tr><td class="state" colspan="8">Couldn’t load the guide: ${esc(e.message)}</td></tr>`;
  }
})();
