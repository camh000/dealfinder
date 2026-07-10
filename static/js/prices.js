/* Price guide: searchable market medians with 30-day trend, live-listing
   counts linking to model pages, and a persistent build basket. */

let guide = null, cat = 'all';
let sort = { col: 'AvgPrice', asc: false };
let basket = JSON.parse(localStorage.getItem('pcd-basket') || '[]');

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
  $$('#cat-pills .pill').forEach(x => x.classList.toggle('active', x === p));
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
