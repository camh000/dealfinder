# PC Deal Finder — TODO

## Frontend

- [ ] **Light / dark theme toggle** — light theme CSS vars + toggle button persisted to `localStorage`
- [ ] **Last scraped timestamp** — top-right of the dashboard should display the datetime the last scrape run completed
- [ ] **Components pricing tab** — new tab with a searchable component browser showing average market price per model; allow selecting multiple components to sum their combined value (useful for valuing a parts bundle or full build)
- [ ] **Sortable columns** — click any header to sort by price, discount %, or time remaining
- [ ] **Filter panel** — filter by brand, minimum discount %, minimum £ saving
- [ ] **Widen time window** — add a "coming up" section for auctions ending in 2–6 hours
- [ ] **PWA / mobile install** — add `manifest.json` and service worker for home screen install
- [ ] **Align OUTCOMES panel columns** — stat cards in the top panel are slightly offset from the resolved/pending table columns below

## Scraper / Data

- [x] 🔴 **Find Oxylabs alternative** — replaced with Zyte API (pay-per-use, no subscription)
- [ ] **Monitor curl_cffi stability in Docker/Linux** — `chrome120` appears to be working across recent full scrape runs; keep an eye on whether it holds or regresses intermittently (Zyte still covers any failures)
- [ ] **Zyte 520 retry** — on HTTP 520 (unknown web server error), back off and retry up to N times before failing over
- [ ] **Scrape run summary log** — at end of each category scrape, log how many items were inserted vs updated (new vs already-seen listings)
- [x] **Adaptive scheduler** — replace fixed 30-min interval with dynamic logic: default to hourly full scrape; when active deals are approaching their end time, launch targeted scrapes (by item title) at increasing frequency as the clock runs down (e.g. 15 min → 5 min → 1 min out)
- [ ] **Bid count filter** — deprioritise or hide items with 5+ bids (price likely already bid up)
- [ ] **Reserve price detection** — filter out "Reserve not met" listings
- [ ] **Seller feedback filter** — skip listings from sellers below a configurable feedback threshold
- [ ] **More categories** — RAM (DDR4/DDR5), SSDs, or motherboards

## Ranking & Scoring

- [ ] **Deal score** — composite ranking: `discount% × (1 / hours_remaining) × (1 / bid_count)`
- [ ] **Price distribution** — show min / max / spread alongside average so you can judge reliability
- [ ] **Market trend indicator** — flag if avg sold prices for a model are rising or falling over 30 days

## Notifications & Tracking

- [ ] **Ntfy / Pushover notifications** — notify once per item ID when a deal is first detected
- [ ] **Auto-bid button** — one-click to place a max bid on a deal listing as the auction nears its end (requires eBay OAuth integration)
- [x] **Deal outcome tracking** — record surfaced deals and what they actually sold for to validate the algorithm
- [x] **Outcome verification scrape** — a configurable number of hours after a tracked deal's end time, search eBay sold listings by the item title to confirm the final sale price is captured in the resolved panel (handles cases where the scheduler misses the sold listing)

## Security

- [ ] **Cloudflare Tunnel exposure review** — assess risks of making the Flask UI publicly accessible: add HTTP basic auth or token gate, review API endpoints for input validation, consider rate limiting

## Bugs

- [ ] 🔴 **Price parsing drops thousands separator** — `__ParseRawPrice` does `replace(',', '.')` so `£1,740.70` → `£1.740.70`; regex then matches `1.740` = £1.74. Fix: `replace(',', '')` (`EbayScraper.py: __ParseRawPrice`)
- [ ] **Complete PC builds classified as CPU** — titles like "HIGH END GAMING PC RYZEN 7 9800x3d, AMD Radeon RX 9070 XT" pass the system-listing filter; add `'gaming pc'`, `'custom pc'`, `'full pc'`, `'complete pc'` to `_is_system` keyword list (`EbayScraper.py: __ParseItems CPU branch`)
