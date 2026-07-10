# PC Deal Finder — TODO

## Frontend

- [x] **Sortable columns** — click any header to sort by price, discount %, or time remaining
- [x] **Outcomes surfaced timestamp** — the OUTCOMES tab "Surfaced" column currently shows only a date (e.g. "27 Feb"); include the time of day so items surfaced on the same day can be distinguished
- [x] **Components pricing tab** — new tab with a searchable component browser showing average market price per model; allow selecting multiple components to sum their combined value (useful for valuing a parts bundle or full build)
- [x] **Prices tab grouping simplification** — GPUs should be grouped by Model only (drop VRAM/Brand from the GROUP BY); CPUs grouped by Model only (drop Brand/Socket/Cores); HDDs grouped by Interface + CapacityGB only (drop FormFactor/Brand); reduces fragmentation so each model has a single representative average price
- [x] **Last scraped timestamp** — top-right of the dashboard should display the datetime the last scrape run completed
- [x] **Prices tab sortable columns** — click any column header (Cat, Model/Specs, Avg Market, Sales) to sort the price-guide table ascending/descending, consistent with the sort behaviour on the deal tables
- [x] **Outcomes resolved panel: hide Ended items + fixed-height scroll** — filter out EndedUnsold rows from the resolved table (they clutter the outcome history without useful price data); cap the panel at 7 rows tall with overflow-y scroll so it doesn't push pending items off screen
- [x] **Outcomes resolved: show ended-at timestamp** — replace the "Surfaced" column in the resolved table with (or add alongside it) the auction end time (`EndTime`); the ended-at date is more useful for history — "when did this sell?" — than when the scraper first spotted it
- [x] **Outcomes £ saving column** — in the resolved outcomes table, add a column (or sub-line on the Final Sale cell) showing the absolute £ difference between FinalPrice and AvgMarketPrice (e.g. "−£47 vs market"); positive = saved, negative = overpaid; complements the existing % label
- [x] **Bid count on deal panels** — show the current bid count on each deal row in the GPU / CPU / HDD / RAM panels; requires scraping `bidCount` from the eBay listing and storing it; surface as a small muted sub-line below the price
- [x] **Filter panel** — filter by brand, minimum discount %, minimum £ saving
- [x] **Widen time window** — add a "coming up" section for auctions ending in 2–6 hours
- [x] **Align OUTCOMES panel columns** — stat cards in the top panel are slightly offset from the resolved/pending table columns below
- [x] **Light / dark theme toggle** — light theme CSS vars + toggle button persisted to `localStorage`
- [x] **Move build basket to right side** — in the PRICES tab, position the build basket panel on the right side so it stays visible when scrolling through a long component list
- [x] **PWA / mobile install** — add `manifest.json` and service worker for home screen install

## Scraper / Data

- [x] 🔴 **Find Oxylabs alternative** — replaced with Zyte API (pay-per-use, no subscription)
- [x] **Zyte 520 retry** — on HTTP 520 (unknown web server error), back off and retry up to N times before failing over
- [x] **Adaptive scheduler** — replace fixed 30-min interval with dynamic logic: default to hourly full scrape; when active deals are approaching their end time, launch targeted scrapes (by item title) at increasing frequency as the clock runs down (e.g. 15 min → 5 min → 1 min out)
- [x] **Scrape run summary log** — at end of each category scrape, log how many items were inserted vs updated (new vs already-seen listings)
- [x] **Bid count scraping** — scrape and persist `bidCount` from each eBay listing to support the bid count display on deal panels and the future bid-count filter / deal-score features
- [x] ~~**Bid count filter**~~ — superseded by DealScore, which already damps contested auctions by 1/(1+bids)
- [ ] **Reserve price detection** — BLOCKED: eBay search results don't expose reserve status in any current markup (2026-07 audit of sold+active pages); it only appears on the item detail page, which we don't fetch
- [x] **Seller feedback filter** — parsed from the result card ("100% positive (290)"), stored on EBAY, deals hidden below `MIN_SELLER_FEEDBACK_PCT` (default 90); only trusted once a seller has 3+ feedback so new accounts aren't penalised
- [x] **Job lots (HDD)** — multi-drive lots ("5 x 4TB") priced per unit vs single-item medians, excluded from market stats, untested/spares lots skipped, ×N LOT badge in UI
- [x] **SSD category** — capacity + NVMe/SATA interface grouping, portable/external split via the shared DriveType classifier, PCIe gen display-only (grouping on it would fragment — most titles omit it), flash-media/SSHD/system-listing skips, job-lot support inherited
- [ ] **Motherboard category** — group by chipset (B450/B550/Z690… regex-extractable) with a chipset→socket map (B550→AM4, Z690→LGA1700); null-safe DDR4-vs-DDR5 split for dual-gen chipsets; must skip mobo+CPU combos (mirror of the CPU-side combo filter); accept wide in-group spread — board tier (Prime vs ROG) doubles prices within one chipset, so medians are a valuation aid more than a deal signal. PSU deliberately NOT planned: brand tier dominates within wattage/rating groups and surfacing used PSUs as “deals” is anti-advice
- [ ] **Monitor curl_cffi stability in Docker/Linux** — `chrome120` appears to be working across recent full scrape runs; keep an eye on whether it holds or regresses intermittently (Zyte still covers any failures)

## Ranking & Scoring

- [x] **Price distribution** — show min / max / spread alongside average so you can judge reliability; σ-filtered (±2 SD) to exclude outlier sales; applied to both PRICES tab and deal tables
- [x] **Deal score** — composite ranking: `discount% × (1 / hours_remaining) × (1 / bid_count)`
- [x] **Market stats recency window** — medians only trust sales within `MARKET_STATS_DAYS` (default 120); year-old prices no longer blend into today's market value
- [x] **DealSnapshots price trajectories** — every observation of a live in-window deal is appended (hourly surfacing pass + 1–15 min targeted refreshes); the training dataset for time-aware premiums below
- [ ] **Predicted-margin-first surfacing** — end-state design once the near-miss cohort validates the premium model below 20%: demote `SURFACE_MIN_DISCOUNT` to a low *recording floor* (~10–12%, volume control + training data only) and filter the feed/notifications purely on predicted margin (predicted final ≥ X% below median). Prerequisites: (1) near-miss cohort shows the model predicts well in the 12–20% band, (2) premium coverage exists for the newer categories (SSD/Arc/Xeon buckets need ≥5 resolved outcomes each — until then the prediction gate degenerates to "any discount" and the current-discount floor is the only real filter there), (3) ideally time-aware premiums land first (see below) so 6/24h views aren't filtered on optimistic ratios. The machinery already exists — the change itself is small; the evidence is what's pending
- [ ] **Time-to-end-aware snipe premiums** — the current final/surfaced ratios are all trained at ≤2h-to-end (the surfacing window) but applied to 6h/24h UI views, where more bidding remains → predictions there are optimistic. Once DealSnapshots has a few weeks of data, condition the premium on hours-remaining buckets (ratio = final vs price-at-N-hours-out). Until then, consider hiding/downweighting predictions outside the ~2h window they were trained on (`queries.annotate_predictions`)
- [x] **Near-miss control cohort** — listings in the `[SURFACE_NEARMISS_DISCOUNT, SURFACE_MIN_DISCOUNT)` band (default 12–20%) are recorded flagged `NearMiss=1`: never notified, excluded from the outcomes scoreboard and premium training; their resolved win rate shows as a "Near-miss WR" stat card on the OUTCOMES All view — if it rivals the main win rate, lower the threshold
- [ ] **Surface after targeted scrapes** — `SurfaceDeals` only runs on the hourly tick, so a listing that first drops below the threshold in its final ~40 min can end unrecorded; run a cheap surfacing pass after each targeted-scrape batch to close the gap (`scheduler.run_targeted_scrapes`)
- [ ] **Market trend indicator** — flag if avg sold prices for a model are rising or falling over 30 days

## Notifications & Tracking

- [x] **Deal outcome tracking** — record surfaced deals and what they actually sold for to validate the algorithm
- [x] **Outcome verification scrape** — a configurable number of hours after a tracked deal's end time, search eBay sold listings by the item title to confirm the final sale price is captured in the resolved panel (handles cases where the scheduler misses the sold listing)
- [x] **Fix outcome verification + give-up threshold** — `VerifyPendingOutcomes` is not resolving items as expected; investigate why (wrong search params? eBay not returning sold results for that title?); also add a configurable give-up threshold (e.g. 7 days after EndTime) after which an item is marked as permanently unresolvable rather than retried forever
- [x] ~~**Ntfy / Pushover notifications**~~ — superseded by Home Assistant push notifications with per-recipient category settings (SETTINGS tab)
- [ ] **Auto-bid button** — one-click to place a max bid on a deal listing as the auction nears its end (requires eBay OAuth integration)

## Security

- [x] **Sanitise 500 error responses** — all five route error handlers return `str(e)` in the JSON body, which can expose internal detail (DB host, file paths); replace with a generic `"internal error"` message and log the real exception server-side (`App.py`)
- [x] **HTTP Basic Auth gate** — configurable Basic Auth via `HTTP_USER` + `HTTP_PASS` env vars in a `before_request` hook; enabled only when both are set (`App.py`)
- [ ] **API rate limiting** — no per-IP throttling on any endpoint; add Flask-Limiter with a sensible default (e.g. 60 req/min) configurable via a `RATE_LIMIT` env var; most critical for `/api/deals` which inserts rows on each call (`App.py`, `requirements.txt`)

## Bugs

- [x] **ScrapeTargeted fails on RAM items** — fixed during the audit refactor: `__ParseItems` now always includes the `'ram-type'` key for every category (`EbayScraper.py`)
- [x] 🔴 **Price parsing drops thousands separator** — `__ParseRawPrice` does `replace(',', '.')` so `£1,740.70` → `£1.740.70`; regex then matches `1.740` = £1.74. Fix: `replace(',', '')` (`EbayScraper.py: __ParseRawPrice`); after fixing, run a backfill query to find and correct suspicious prices already in the DB (any active/sold GPU or CPU listing under £10 is a candidate)
- [x] **Suppress zero active-deals log** — `GetActiveDeals()` logs "Active deals: 0 item(s) currently tracked" every scheduler tick when there are no tracked deals; only log when count > 0 (`EbayScraper.py: GetActiveDeals`)
- [x] **Complete PC builds classified as CPU** — titles like "HIGH END GAMING PC RYZEN 7 9800x3d, AMD Radeon RX 9070 XT" pass the system-listing filter; add `'gaming pc'`, `'custom pc'`, `'full pc'`, `'complete pc'` to `_is_system` keyword list (`EbayScraper.py: __ParseItems CPU branch`)
