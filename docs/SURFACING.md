# What decides whether an auction surfaces

The full pipeline from "listing exists on eBay" to "deal on your dashboard /
phone", in order. A listing must survive **every** stage. Defaults shown;
most are env-tunable (see `credentials.env.example`).

## Stage 1 — Being seen at all (scraper, hourly)

- The listing must appear on **page 1** (~60 results) of one of the hourly
  search queries (`scheduler.py` query lists — currently 12 GPU, 12 CPU,
  7 HDD, 4 SSD, 8 RAM), searched twice: sold and active (active sorted
  ending-soonest, which biases coverage toward auctions closing soon —
  deliberately, since that's the deal window).
- Auction listings only (`LH_Auction`), used condition, eBay UK.
- Must parse: title, price, a real `/itm/<id>` link. Promoted "Shop on eBay"
  tiles are skipped.

## Stage 2 — The junk gate (all categories)

Skipped entirely, both as live listings and as sold history:

- **Damaged/faulty wording**: untested, spares, repairs, faulty, not working,
  for parts, as-is, dead, damaged, broken, non-functional. A damaged item is
  a phantom deal live and median-poison sold.
- **Accessory listings**: "no GPU", "not included", "heatsink & box",
  "box only" etc. — boxes sold under the component's name.

## Stage 3 — Category parsing

Each category has its own extraction and its own skips:

- **GPU**: model regex (RTX/GTX/RX/TITAN/Arc); dual-VRAM models (1060, 2060,
  3050, 3060, 3080, 4060 Ti, 5060 Ti, RX 570/580, 7600, 9060 XT, A770) get
  VRAM baked into the model ("RTX 3060 12GB") so each variant prices against
  its own market. Unknown-VRAM listings of those models keep the bare name
  and usually fall below the stats floor — excluded beats mispriced.
- **CPU**: Core-i and all Xeon families; whole systems (gaming PC, PowerEdge,
  ProLiant, Supermicro, mini PC…) and CPU+motherboard+RAM combos skipped;
  "2x Xeon" matched pairs become quantity-2 lots.
- **HDD**: capacity + SATA/SAS + Internal/External; flash media (USB sticks,
  SD cards) skipped.
- **SSD**: capacity (60GB–8TB) + NVMe/SATA/USB(portable) + PCIe gen
  (display-only); flash media, SSHD hybrids, whole systems skipped.
- **RAM**: DDR3/4/5 + total kit capacity + DIMM/SODIMM; kit notation (2x8GB)
  is ONE kit, not a lot; whole machines skipped.
- **Job lots** ("5 x 4TB", "job lot of 10"): quantity parsed conservatively
  (must sit next to a capacity token or lot phrase, cap 30) — everything
  downstream is priced **per unit**.

## Stage 4 — Having a market to be judged against

A listing can only be a deal relative to its **market group**:

| Category | Group |
|---|---|
| GPU / CPU | Model (VRAM-qualified where relevant) |
| HDD / SSD | Capacity + Interface + Internal/External |
| RAM | DDR type + kit capacity + DIMM/SODIMM |

Market value = **median** of that group's sold effective prices
(item + postage), **single units only** (lots excluded — bulk discounts are
structural), sold within the last **120 days** (`MARKET_STATS_DAYS`), and
the group needs **≥ 5 sales** in that window. No qualifying group → the
listing is invisible to deal detection, full stop. The displayed min–max
range is sanity-banded (0.4×–2.5× median) so outliers can't stretch it.

## Stage 5 — The deal test

Evaluated hourly by `SurfaceDeals` (server-side, drives recording +
notifications, window 2h / floor 20%) and live by the dashboard (window and
floor user-adjustable):

1. Auction still unsold, ending **within the window**.
2. Effective **per-unit** price < median × 0.8 (**≥ 20% discount**,
   `SURFACE_MIN_DISCOUNT`).
3. **Seller gate**: ≥ 90% positive feedback once the seller has 3+ reviews
   (`MIN_SELLER_FEEDBACK_PCT`); new/unknown sellers pass.
4. **Freshness gate**: a scrape actually saw the listing within 90 minutes
   (`STALE_DEAL_MINUTES`) — kills seller-cancelled phantoms.

Passing this stage records the listing in `DealOutcomes` (once, ever — the
snapshot the outcomes scoreboard grades). Listings in the 12–20% band are
also recorded as the hidden **near-miss control cohort**.

## Stage 6 — The prediction gate (feed + notifications)

From resolved outcomes we learn **snipe premiums**: median final÷spotted
price per category × bid bucket (0 / 1–3 / 4+ bids, ≥ 5 samples, near-miss
cohort excluded). Each candidate's **predicted final = current × ratio**.

- **Feed + badges**: only shown if the predicted final is still **below**
  the (lot-scaled) median. No premium history → prediction equals current
  price → passes untouched.
- **Notifications**: stricter — predicted discount ≥ 10%
  (`SURFACE_MIN_PREDICTED_DISCOUNT`), plus the recipient has that category
  ticked. One push per listing, ever.

This gate bites unevenly **by design**. GPUs close ~1.21× their spotted
price, so a 20%-off GPU usually survives. HDDs close ~1.45–1.55× — so an
HDD effectively needs **>31% off** (>35% with 4+ bids) to surface. That's
the main reason the HDD tab looks quiet relative to the others; the other
reason is inventory (used drives skew Buy-It-Now, and we only scrape
auctions).

## Ranking

`DealScore = predicted discount ÷ hours-remaining (floor 0.25) ÷ (1 + bids)`
— urgency-weighted, competition-damped, computed on the *predicted* not the
current discount.

## What removes a surfaced deal

Sold, ended, bid past the threshold, prediction flipped, or simply not seen
by any scrape for 90 minutes (cancelled/removed listings age out
automatically).

## Known gaps (in TODO.md)

- `SurfaceDeals` runs on the hourly tick — a listing that first becomes a
  deal in its final ~40 minutes can end unrecorded (targeted scrapes refresh
  *already-tracked* deals every 1–15 min, but don't surface new ones).
- Premium ratios are trained at ≤ 2h-to-end and are optimistic when applied
  to the 6h/24h views; time-aware premiums await DealSnapshots data.
- Page-1-only search visibility: a deal can exist on eBay without ever being
  seen if its query's page 1 is crowded.
