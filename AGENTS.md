# AGENTS.md — PC Deal Finder

Guidelines for AI coding assistants working on this repository.

## Project Overview

Python Flask web app + scraper that finds eBay deals on GPUs, CPUs, and HDDs by comparing live auction prices against historical sold data. Uses MariaDB for storage.

## Build / Run / Test Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run web server (dev)
python App.py

# Run scraper/scheduler (dev)
python scheduler.py

# Run tests
pytest tests/                    # All tests
pytest tests/ -v                 # Verbose output
pytest tests/ -m "not live"      # Unit tests only (no network)
pytest tests/ -m live            # Live integration tests only
pytest tests/test_scraper.py::TestParseRawPrice -v   # Single test class
pytest tests/test_scraper.py::TestParseRawPrice::test_basic_gbp -v  # Single test

# Docker
docker compose up -d --build     # Build and start both containers
docker compose logs -f scraper   # Tail scraper logs
docker compose logs -f web       # Tail web logs
```

## Project Structure

```
├── App.py               # Flask web server + REST API
├── EbayScraper.py       # Core scraper, parser, DB operations
├── scheduler.py         # Adaptive scheduling for scrapes
├── backfill_prices.py   # Historical price backfill utility
├── templates/
│   └── Index.html       # Single-page dashboard (vanilla JS/CSS)
├── tests/
│   └── test_scraper.py  # pytest test suite
├── requirements.txt     # Python dependencies
├── pytest.ini          # pytest configuration with markers
├── docker-compose.yml   # Two services: web + scraper
├── Dockerfile.web       # Gunicorn container
├── Dockerfile.scraper   # Scheduler container
└── credentials.env      # Environment vars (not in git)
```

## Code Style Guidelines

### General

- **Python 3.11+** with type hints where helpful
- Line length: ~100 characters (be pragmatic, not rigid)
- Use double quotes for strings unless single quotes reduce escaping

### Imports

Order: stdlib → third-party → local. Separate groups with blank line.

```python
import re
import time
from datetime import datetime, timedelta

import mariadb
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import EbayScraper
```

### Naming Conventions

| Type | Convention | Example |
|------|-----------|---------|
| Functions | snake_case | `get_connection()`, `parse_ebay_endtime()` |
| Private functions | _single_leading_underscore | `_fetch_direct()`, `_upload()` |
| Internal helpers | __double_underscore (module level) | `__ParseRawPrice()`, `__GetHTML()` |
| Classes | PascalCase | `Product`, `TestParseRawPrice` |
| Constants | UPPER_SNAKE_CASE | `GPU_QUERY_LIST`, `OUTCOME_VERIFY_HOURS` |
| Global state | _leading_underscore | `_direct_session`, `_DIRECT_HEADERS_BASE` |
| Variables | snake_case | `avg_price`, `item_count` |
| Test classes | Test + PascalCase | `TestParseRawPrice`, `TestFetchDirect` |
| Test methods | test_ + snake_case | `test_basic_gbp()`, `test_removes_outliers()` |

### Type Hints

Use for function signatures and complex return types:

```python
def parse_ebay_endtime(endtime_str: str, reference_date: datetime = None) -> datetime | None:
def GetActiveDeals() -> list:
def _upload(cur, p: Product, product_type: str) -> int:
```

### Error Handling

Use `try/except` with specific exceptions. Log errors with context.

```python
try:
    result = some_operation()
except mariadb.Error as e:
    log.error(f"DB error in {context}: {e}")
    return None
except Exception as e:
    log.exception(f"Unexpected error: {e}")
    raise
```

For fallbacks (e.g., direct → Zyte API), catch and log, then proceed to fallback.

### Logging

Use module-level logger:

```python
import logging
log = logging.getLogger(__name__)

log.info("Starting scrape for %s", product_type)
log.warning("Rate limit hit, backing off")
log.error("DB connection failed: %s", e)
```

### Database

- Prices stored in **pence** (integers) to avoid float issues
- Display prices converted with `/ 100`
- Connection helper: `get_connection()` in App.py, `_get_connection()` in EbayScraper.py
- Tables created automatically where possible; category tables (GPU, CPU, HDD) must be created manually

### Testing

- Tests in `tests/` directory
- Use pytest markers: `@pytest.mark.live` for network tests
- Mock external dependencies (requests, DB) for unit tests
- Access module-private helpers via `vars(Module)["__Name"]`
- Test classes group related functionality

### Documentation

- Docstrings for non-obvious functions (Google style)
- Comments for complex logic or workarounds
- Section headers in large files using `# ── Section ─────────────────`

### Environment Variables

Load once at module level:

```python
from dotenv import load_dotenv
load_dotenv("credentials.env")

OUTCOME_VERIFY_HOURS = int(os.environ.get('OUTCOME_VERIFY_HOURS', '6'))
```

### SQL Style

- UPPERCASE for SQL keywords
- snake_case for table/column names
- Indent subqueries consistently
- Use CTEs (WITH clauses) for complex queries

### Frontend (Index.html)

- Vanilla JS, no frameworks
- CSS variables for theming
- Mobile-first responsive design
- PWA manifest and service worker support

## Important Notes

- **Never commit `credentials.env`** — it contains secrets
- Scraper uses `curl-cffi` with Chrome TLS fingerprint to bypass bot detection
- Zyte API is pay-per-use fallback (only charged when curl-cffi fails)
- The `_direct_session` global must be reset via `reset_direct_session()` before each scrape run
- Always run tests with `-m "not live"` unless specifically testing live scraping
