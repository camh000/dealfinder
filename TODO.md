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
- [ ] **Move build basket to right side** — in the PRICES tab, position the build basket panel on the right side so it stays visible when scrolling through a long component list
- [x] **PWA / mobile install** — add `manifest.json` and service worker for home screen install

## Scraper / Data

- [x] 🔴 **Find Oxylabs alternative** — replaced with Zyte API (pay-per-use, no subscription)
- [x] **Zyte 520 retry** — on HTTP 520 (unknown web server error), back off and retry up to N times before failing over
- [x] **Adaptive scheduler** — replace fixed 30-min interval with dynamic logic: default to hourly full scrape; when active deals are approaching their end time, launch targeted scrapes (by item title) at increasing frequency as the clock runs down (e.g. 15 min → 5 min → 1 min out)
- [x] **Scrape run summary log** — at end of each category scrape, log how many items were inserted vs updated (new vs already-seen listings)
- [ ] **Bid count scraping** — scrape and persist `bidCount` from each eBay listing to support the bid count display on deal panels and the future bid-count filter / deal-score features
- [ ] **Bid count filter** — deprioritise or hide items with 5+ bids (price likely already bid up)
- [ ] **Reserve price detection** — filter out "Reserve not met" listings
- [ ] **Seller feedback filter** — skip listings from sellers below a configurable feedback threshold
- [ ] **More categories** — RAM (DDR4/DDR5), SSDs, or motherboards
- [ ] **Monitor curl_cffi stability in Docker/Linux** — `chrome120` appears to be working across recent full scrape runs; keep an eye on whether it holds or regresses intermittently (Zyte still covers any failures)

## Ranking & Scoring

- [x] **Price distribution** — show min / max / spread alongside average so you can judge reliability; σ-filtered (±2 SD) to exclude outlier sales; applied to both PRICES tab and deal tables
- [ ] **Deal score** — composite ranking: `discount% × (1 / hours_remaining) × (1 / bid_count)`
- [ ] **Market trend indicator** — flag if avg sold prices for a model are rising or falling over 30 days

## Notifications & Tracking

- [x] **Deal outcome tracking** — record surfaced deals and what they actually sold for to validate the algorithm
- [x] **Outcome verification scrape** — a configurable number of hours after a tracked deal's end time, search eBay sold listings by the item title to confirm the final sale price is captured in the resolved panel (handles cases where the scheduler misses the sold listing)
- [x] **Fix outcome verification + give-up threshold** — `VerifyPendingOutcomes` is not resolving items as expected; investigate why (wrong search params? eBay not returning sold results for that title?); also add a configurable give-up threshold (e.g. 7 days after EndTime) after which an item is marked as permanently unresolvable rather than retried forever
- [ ] **Ntfy / Pushover notifications** — notify once per item ID when a deal is first detected
- [ ] **Auto-bid button** — one-click to place a max bid on a deal listing as the auction nears its end (requires eBay OAuth integration)

## Security

- [x] **Sanitise 500 error responses** — all five route error handlers return `str(e)` in the JSON body, which can expose internal detail (DB host, file paths); replace with a generic `"internal error"` message and log the real exception server-side (`App.py`)
- [ ] **HTTP Basic Auth gate** — all 5 Flask routes are unauthenticated (including `/api/deals` which writes to `DealOutcomes` on every page load); add configurable Basic Auth via `HTTP_USER` + `HTTP_PASS` env vars enforced in a `before_request` hook — no extra library needed (`App.py`)
- [ ] **API rate limiting** — no per-IP throttling on any endpoint; add Flask-Limiter with a sensible default (e.g. 60 req/min) configurable via a `RATE_LIMIT` env var; most critical for `/api/deals` which inserts rows on each call (`App.py`, `requirements.txt`)

## Bugs

- [ ] **ScrapeTargeted fails on RAM items** — `ScrapeTargeted()` fails with KeyError: 'ram-type' when processing RAM items; the Product dataclass expects 'ram_type' but the scraper returns 'ram-type' with a hyphen; affects targeted scrapes when tracked RAM deals are ending (`EbayScraper.py: ScrapeTargeted`)
- [x] 🔴 **Price parsing drops thousands separator** — `__ParseRawPrice` does `replace(',', '.')` so `£1,740.70` → `£1.740.70`; regex then matches `1.740` = £1.74. Fix: `replace(',', '')` (`EbayScraper.py: __ParseRawPrice`); after fixing, run a backfill query to find and correct suspicious prices already in the DB (any active/sold GPU or CPU listing under £10 is a candidate)
- [x] **Suppress zero active-deals log** — `GetActiveDeals()` logs "Active deals: 0 item(s) currently tracked" every scheduler tick when there are no tracked deals; only log when count > 0 (`EbayScraper.py: GetActiveDeals`)
- [x] **Complete PC builds classified as CPU** — titles like "HIGH END GAMING PC RYZEN 7 9800x3d, AMD Radeon RX 9070 XT" pass the system-listing filter; add `'gaming pc'`, `'custom pc'`, `'full pc'`, `'complete pc'` to `_is_system` keyword list (`EbayScraper.py: __ParseItems CPU branch`)
