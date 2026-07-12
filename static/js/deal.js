/* Deal detail: everything we know about one listing — stat strip, market
   position bar, the observed price/bid trajectory, listing facts,
   market context, and the outcome once resolved. */

const ID = window.DEAL_ID;

const factRow = (label, value) => value == null || value === '' ? '' :
  `<tr><td class="dimcell" style="width:40%">${label}</td><td>${value}</td></tr>`;

function statStrip(d) {
  const l = d.listing, s = d.stats, p = d.prediction;
  const eff = Number(l.ItemPrice || 0) + Number(l.Shipping || 0);
  const qty = Number(l.Quantity) || 1;
  const live = !l.SoldDate && l.EndTime && new Date(l.EndTime) > Date.now();
  const disc = s && s.median ? (1 - (eff / qty) / s.median) * 100 : null;
  const cells = [];
  cells.push(`<div class="stat"><b class="num">${fmtGBP(l.ItemPrice)}</b>
      <span>${Number(l.Shipping) > 0 ? '+' + fmtGBP(l.Shipping) + ' delivery' : 'free delivery'}</span></div>`);
  if (qty > 1)
    cells.push(`<div class="stat"><b class="num">${fmtGBP(eff / qty)}</b><span>per unit (×${qty})</span></div>`);
  if (s && s.median != null)
    cells.push(`<div class="stat"><b class="num">${fmtGBP(s.median)}</b><span>market median (n=${s.n})</span></div>`);
  if (disc != null)
    cells.push(`<div class="stat"><b class="num ${disc > 0 ? 'good' : 'bad'}">${disc.toFixed(1)}%</b><span>${disc > 0 ? 'below' : 'above'} market</span></div>`);
  if (p && live)
    cells.push(`<div class="stat" title="current × ${p.ratio} (from ${p.n} resolved outcomes)">
        <b class="num">${fmtGBP(p.final)}</b><span>predicted final</span></div>`);
  if (live)
    cells.push(`<div class="stat"><b class="countdown num" ${endsAttr(l.EndTime)}></b>
        <span>remaining${l.EndTimeExact ? ' (exact)' : ''}</span></div>`);
  if (l.ListingType === 'bin')
    cells.push(`<div class="stat"><b class="num">BIN</b><span>fixed price${
      l.HasBid === 1 ? ' · +bids' : ''}${l.HasBestOffer === 1 ? ' · offers' : ''}</span></div>`);
  else
    cells.push(`<div class="stat"><b class="num">${l.Bids || 0}</b><span>bids${
      l.HasBin === 1 ? ' · +Buy It Now' : ''}${l.HasBestOffer === 1 ? ' · offers' : ''}</span></div>`);
  if (l.SellerFeedbackPct != null && l.SellerFeedbackCount >= 3)
    cells.push(`<div class="stat"><b class="num">${l.SellerFeedbackPct}%</b>
        <span>seller (${l.SellerFeedbackCount})</span></div>`);
  $('#stats').innerHTML = cells.join('');
}

function bigPosbar(d) {
  const s = d.stats, l = d.listing;
  if (!s || !(s.max > s.min)) return;
  const qty = Number(l.Quantity) || 1;
  const eff = (Number(l.ItemPrice || 0) + Number(l.Shipping || 0)) / qty;
  const pred = d.prediction ? d.prediction.final / qty : null;
  const pct = v => Math.max(0, Math.min(100, (v - s.min) / (s.max - s.min) * 100)).toFixed(1);
  $('#posbar-card').style.display = '';
  $('#big-posbar').innerHTML = `
    <div class="posbar" style="height:26px">
      <div class="track" style="top:12px"></div>
      <span class="tick" style="left:${pct(s.median)}%;top:6px;height:16px" title="median ${fmtGBP(s.median)}"></span>
      ${pred != null ? `<span class="dot hollow" style="left:${pct(pred)}%;top:8px" title="predicted ${fmtGBP(pred)}"></span>` : ''}
      <span class="dot" style="left:${pct(eff)}%;top:8.5px" title="this listing ${fmtGBP(eff)}"></span>
    </div>
    <div class="posbar-legend"><span>${fmtGBP0(s.min)}</span><span>median ${fmtGBP0(s.median)}${
      pred != null ? ` · pred ~${fmtGBP0(pred)}` : ''}</span><span>${fmtGBP0(s.max)}</span></div>`;
}

/* Price advisor: what you can pay (before delivery) while the delivery-
   inclusive per-unit price stays under market by a given margin. A listing
   can offer several routes at once — bidding, a Buy-It-Now and Best Offers —
   so the card names and explains whichever apply (from item-page enrichment):
     auction only ............... "Max-bid advisor"  (bid up to X)
     auction + Best Offer ....... "Price advisor"    (bid OR offer up to X)
     Buy-It-Now + Best Offer .... "Offer advisor"    (offer up to X, or buy)
     fixed price, no offers ..... "Fixed price"      (no negotiation — just buy)
   HasBin / HasBestOffer are NULL until the listing is enriched; unknown-offer
   listings hedge rather than hide. */
function maxBidAdvisor(d) {
  const l = d.listing, s = d.stats;
  const isBinType = l.ListingType === 'bin';
  const live = !l.SoldDate && (isBinType || (l.EndTime && new Date(l.EndTime) > Date.now()));
  if (!live || !s || s.median == null) return;

  // A listing can offer several routes at once — trust the enriched flags,
  // fall back to ListingType when a flag is NULL (not yet enriched).
  const canBid   = l.HasBid === 1 || (l.HasBid == null && !isBinType);
  const canBuy   = l.HasBin === 1 || (l.HasBin == null && isBinType);
  const canOffer = l.HasBestOffer === 1;
  const offerKnown = l.HasBestOffer != null;

  const qty = Number(l.Quantity) || 1;
  const ship = Number(l.Shipping) || 0;
  const current = Number(l.ItemPrice) || 0;
  const cap = margin => s.median * qty * (1 - margin) - ship;

  $('#maxbid-card').style.display = '';

  // Fixed price you can only buy at (no bids, no offers) → verdict, no table.
  if (canBuy && !canBid && !canOffer && offerKnown) {
    const disc = (1 - (current + ship) / (s.median * qty)) * 100;
    $('#maxbid-title').textContent = 'Fixed price';
    $('#maxbid-body').innerHTML = `<p class="help" style="font-size:14px;margin:0">
      This listing is a fixed <b>${fmtGBP(current)}</b> with no offers accepted — the price is the price.
      That's <b class="${disc > 0 ? 'good' : 'bad'}">${Math.abs(disc).toFixed(1)}% ${disc > 0 ? 'under' : 'over'}</b>
      the ${fmtGBP(s.median)} market median${qty > 1 ? ` (×${qty})` : ''}${
      disc >= 15 ? ' — worth buying.' : disc > 0 ? '.' : ' — skip it.'}</p>`;
    return;
  }

  const rows = [
    { margin: 0.20, note: 'the surfacing bar — a definite deal' },
    { margin: 0.15, note: 'still a comfortable win' },
    { margin: 0.00, note: 'break-even with the market — walk away above this' },
  ].filter(r => cap(r.margin) > 0);
  if (!rows.length) return;

  // Name the routes in plain English for the closing note.
  const routeWords = [];
  if (canBid) routeWords.push('place a bid');
  if (canBuy) routeWords.push('use Buy It Now');
  if (canOffer) routeWords.push('send a Best Offer');
  const routesPhrase = routeWords.length === 1 ? routeWords[0]
    : routeWords.slice(0, -1).join(', ') + ' or ' + routeWords.slice(-1);

  // Title + column header. "Price advisor" whenever more than one route
  // exists (Cam: an auction that also takes offers is a price advisor);
  // single-route pages keep their specific name.
  const routeCount = (canBid ? 1 : 0) + (canBuy ? 1 : 0) + (canOffer ? 1 : 0);
  let title, colHead, note;
  if (routeCount > 1) {
    title = 'Price advisor';
    colHead = 'Pay up to';
    note = `This listing lets you <b>${routesPhrase}</b> — whichever route you take, don't pay above these figures to stay under market.`;
  } else if (canBid) {
    title = 'Max-bid advisor';
    colHead = 'Bid up to';
    note = `Bids are what you enter on eBay (delivery excluded); margins compare the delivery-inclusive total against market.`;
  } else if (canOffer) {
    title = 'Offer advisor';
    colHead = 'Offer';
    note = `This listing accepts <b>Best Offers</b> — asking is ${fmtGBP(current)}; open below the margin you want.`;
  } else {   // BIN, offer availability unknown (not yet enriched)
    title = 'Offer advisor';
    colHead = 'Offer';
    note = `Asking is ${fmtGBP(current)}. If this listing accepts Best Offers, open below the margin you want; otherwise just buy at or under these figures.`;
  }

  // "Dead" row: with bidding available a margin is spent once the current
  // price passes its cap; a buy/offer-only listing already clears it when
  // the asking price is under the cap.
  const deadFor = v => canBid ? current > v : current <= v;
  const deadNote = canBid ? 'already past this — ' : 'asking already clears this — just buy — ';

  $('#maxbid-title').textContent = title;
  $('#maxbid-body').innerHTML = `
    <div class="tbl-wrap" style="box-shadow:none;border:none">
      <table class="tbl"><thead>
        <tr><th>To stay under market by</th><th class="num">${colHead}</th><th>Meaning</th></tr>
      </thead><tbody>
        ${rows.map(r => {
          const v = cap(r.margin);
          const dead = deadFor(v);
          return `<tr${dead ? ' style="opacity:.45"' : ''}>
            <td>${(r.margin * 100).toFixed(0)}%</td>
            <td class="num"><b>${fmtGBP(v)}</b></td>
            <td class="dimcell">${dead ? deadNote : ''}${r.note}</td>
          </tr>`;
        }).join('')}
      </tbody></table>
    </div>
    <p class="help" style="margin:8px 0 0">
      Based on the ${fmtGBP(s.median)} market median${qty > 1 ? ` × ${qty} units` : ''}${ship > 0 ? `, minus ${fmtGBP(ship)} delivery` : ''}.
      ${note} Figures are item-price only (delivery excluded).
    </p>`;
}

function trajectory(d) {
  const snaps = d.snapshots || [];
  if (snaps.length < 2) return;
  $('#traj-card').style.display = '';
  $('#traj-sub').textContent = `(${snaps.length} observations)`;
  const narrow = isNarrowScreen();
  const W = narrow ? 380 : 720, H = narrow ? 210 : 190,
        P = narrow ? 34 : 40, PR = narrow ? 42 : 56;
  const t0 = new Date(snaps[0][0]).getTime();
  const tEnd = d.listing.EndTime ? new Date(d.listing.EndTime).getTime()
                                 : new Date(snaps[snaps.length - 1][0]).getTime();
  const prices = snaps.map(s => s[1]);
  const med = d.stats ? d.stats.median * (Number(d.listing.Quantity) || 1) : null;
  const pred = d.prediction ? d.prediction.final : null;
  const domain = prices.concat(med != null ? [med] : [], pred != null ? [pred] : []);
  const lo = Math.min(...domain) * 0.95, hi = Math.max(...domain) * 1.05;
  const x = t => P + Math.max(0, Math.min(1, (t - t0) / ((tEnd - t0) || 1))) * (W - P - PR);
  const y = v => H - 26 - (v - lo) / ((hi - lo) || 1) * (H - 52);

  const pts = snaps.map(s => `${x(new Date(s[0]).getTime()).toFixed(1)},${y(s[1]).toFixed(1)}`).join(' ');
  // bid-arrival markers: a filled dot wherever the bid count increased
  let bidMarks = '';
  for (let i = 1; i < snaps.length; i++) {
    if (snaps[i][2] > snaps[i - 1][2]) {
      bidMarks += `<circle class="pt" cx="${x(new Date(snaps[i][0]).getTime()).toFixed(1)}"
        cy="${y(snaps[i][1]).toFixed(1)}" r="3.5">
        <title>bid ${snaps[i - 1][2]} → ${snaps[i][2]} · ${fmtGBP(snaps[i][1])}</title></circle>`;
    }
  }
  const medLine = med != null ? `
    <line class="medline" x1="${P}" y1="${y(med).toFixed(1)}" x2="${W - PR + 6}" y2="${y(med).toFixed(1)}">
      <title>market median ${fmtGBP(med)}</title></line>` : '';
  const predMark = pred != null ? `
    <circle class="predpt" cx="${W - PR - 6}" cy="${y(pred).toFixed(1)}" r="4.5">
      <title>predicted final ${fmtGBP(pred)}</title></circle>` : '';

  // x labels: start / end times (few, no overlap risk)
  const fmtT = t => new Date(t).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  $('#traj-chart').innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="price trajectory">
    <line class="axis" x1="${P}" y1="${H - 26}" x2="${W - PR + 6}" y2="${H - 26}"/>
    ${medLine}
    <polyline class="line" points="${pts}"/>
    ${bidMarks}${predMark}
    <text x="${P}" y="${H - 10}">${fmtT(t0)}</text>
    <text x="${W - PR}" y="${H - 10}" text-anchor="end">${d.listing.EndTime ? 'ends ' + fmtT(tEnd) : fmtT(tEnd)}</text>
    <text x="${P - 4}" y="${(y(prices[0]) + 3).toFixed(1)}" text-anchor="end">${fmtGBP0(prices[0])}</text>
  </svg>`;
}

function facts(d) {
  const l = d.listing, o = d.outcome || {};
  const chips = [];
  if (d.category) chips.push(d.category.toUpperCase());
  // buying routes this listing offers (from item-page enrichment) — a listing
  // can be several at once, e.g. an auction that also has a Buy It Now.
  const canBid   = l.HasBid === 1 || (l.HasBid == null && l.ListingType !== 'bin');
  const canBuy   = l.HasBin === 1 || (l.HasBin == null && l.ListingType === 'bin');
  const canOffer = l.HasBestOffer === 1;
  const routes = [];
  if (canBid) routes.push('bidding');
  if (canBuy) routes.push('Buy It Now');
  if (canOffer) routes.push('Best Offer');
  $('#facts-tbl tbody').innerHTML = [
    factRow('Buying options', routes.length > 1 ? routes.join(' · ') : null),
    factRow('Condition', o.ItemCondition && esc(o.ItemCondition)),
    factRow('Location', o.ItemLocation && esc(o.ItemLocation)),
    factRow('eBay category', o.CategoryPath && esc(o.CategoryPath.split(' > ').slice(-2).join(' > '))),
    factRow('Quantity', Number(l.Quantity) > 1 ? `×${l.Quantity} lot` : null),
    factRow('Reserve', l.ReserveNotMet ? '<span class="verdict miss">reserve not met</span>' : null),
    factRow('Seller', l.SellerFeedbackPct != null ? `${l.SellerFeedbackPct}% positive (${l.SellerFeedbackCount} reviews)` : null),
    factRow('Last seen by scraper', timeAgo(l.LastSeenAt)),
    factRow('End time', `${fmtDateTime(l.EndTime)}${l.EndTimeExact ? ' <span class="chip new">exact</span>' : ' <span class="chip">±1 min</span>'}`),
    factRow('eBay item', `<a href="${safeUrl(l.URL)}" target="_blank" rel="noopener noreferrer">${l.ID}</a>`),
    factRow('ePID', o.Epid && esc(o.Epid)),
  ].join('') || '<tr><td class="state">Nothing recorded yet.</td></tr>';
}

function market(d) {
  const s = d.stats, o = d.outcome || {};
  $('#market-tbl tbody').innerHTML = [
    factRow('Market group', d.group_label
      ? `<a href="${modelHref(d.category, d.group || {})}">${esc(d.group_label)}</a>` : null),
    s ? factRow('Median (120d)', `<b>${fmtGBP(s.median)}</b> across ${s.n} sales`) : '',
    s ? factRow('Range', `${fmtGBP0(s.min)} – ${fmtGBP0(s.max)}`) : '',
    factRow('Spotted by dealfinder', o.SurfacedAt
      ? `${fmtDateTime(o.SurfacedAt)} at ${fmtGBP(o.SurfacedPrice)} (${o.DiscountPct}% off, ${o.BidsAtSurfacing} bids)` : null),
    factRow('Predicted at spotting', o.PredictedFinal && fmtGBP(o.PredictedFinal)),
    factRow('Cohort', o.NearMiss ? 'near-miss control (12–20% band)' : null),
    factRow('Enrichment note', o.EnrichNote && esc(o.EnrichNote)),
  ].join('') || '<tr><td class="state">No market group — not enough comparables.</td></tr>';
}

function outcome(d) {
  const l = d.listing, o = d.outcome;
  if (!l.SoldDate && !(o && (o.EndedUnsold || o.GaveUp))) return;
  $('#outcome-card').style.display = '';
  let body;
  if (o && o.FinalPrice != null && !o.EndedUnsold) {
    const win = Number(o.FinalPrice) < Number(o.AvgMarketPrice);
    body = `Sold ${fmtDate(l.SoldDate)} for <b>${fmtGBP(o.FinalPrice)}</b> against a
      ${fmtGBP(o.AvgMarketPrice)} market value —
      <span class="verdict ${win ? 'win' : 'miss'}">${win ? 'DEAL' : 'MISS'}</span>
      ${o.PredictedFinal != null ? ` · we predicted ${fmtGBP(o.PredictedFinal)}` : ''}`;
  } else if (o && o.EndedUnsold) {
    body = 'Ended without selling (or was cancelled/removed by the seller).';
  } else if (o && o.GaveUp) {
    body = 'Unresolvable — the listing left eBay search before we could verify the result.';
  } else {
    body = `Sold ${fmtDate(l.SoldDate)} for ${fmtGBP(Number(l.ItemPrice) + Number(l.Shipping || 0))} (delivery-inclusive).`;
  }
  $('#outcome-body').innerHTML = `<p class="help" style="font-size:14px;margin:0">${body}</p>`;
}

(async () => {
  try {
    const res = await fetch(`/api/deal/${ID}`);
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'error');
    $('#deal-title').textContent = data.listing.Title;
    if (data.group_label && data.category)
      $('#deal-sub').innerHTML =
        `<a href="${modelHref(data.category, data.group || {})}">${esc(data.group_label)}</a>`;
    else
      $('#deal-sub').textContent = (data.category || '').toUpperCase();
    $('#ebay-link').href = safeUrl(data.listing.URL);
    statStrip(data); bigPosbar(data); maxBidAdvisor(data); trajectory(data);
    facts(data); market(data); outcome(data);
  } catch (e) {
    $('#stats').innerHTML = `<div class="state">Couldn’t load this deal: ${esc(e.message)}</div>`;
  }
})();
