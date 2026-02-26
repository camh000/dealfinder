# PC Deal Finder — TODO

## Frontend

- [ ] **Light / dark theme toggle** — light theme CSS vars + toggle button persisted to `localStorage`
- [ ] **Sortable columns** — click any header to sort by price, discount %, or time remaining
- [ ] **Filter panel** — filter by brand, minimum discount %, minimum £ saving
- [ ] **Widen time window** — add a "coming up" section for auctions ending in 2–6 hours
- [ ] **PWA / mobile install** — add `manifest.json` and service worker for home screen install

## Scraper / Data

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
- [ ] **Deal outcome tracking** — record surfaced deals and what they actually sold for to validate the algorithm
