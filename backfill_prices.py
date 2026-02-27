"""
backfill_prices.py — one-time cleanup for the thousands-separator parsing bug.

The bug: __ParseRawPrice did replace(',', '.') so £1,740.70 → £1.74 (174 pence).
This script finds GPU/CPU records where Price < 1000 pence (< £10) and removes
the corrupted rows so they can be re-scraped cleanly.

Active listings (EndTime > NOW) are left in place — the next scheduled full
scrape will overwrite them with the correctly-parsed price automatically.

Usage:
    python backfill_prices.py            # dry-run (no deletions)
    python backfill_prices.py --delete   # delete corrupted sold records after confirming
"""

import os
import sys
import argparse
from datetime import datetime

try:
    import dotenv
    dotenv.load_dotenv("credentials.env")
except ImportError:
    pass  # dotenv optional — env vars may already be set

try:
    import mysql.connector
except ImportError:
    print("ERROR: mysql-connector-python is not installed. Run: pip install mysql-connector-python")
    sys.exit(1)


# ── connection ────────────────────────────────────────────────────────────────

def _connect():
    return mysql.connector.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
    )


# ── queries ───────────────────────────────────────────────────────────────────

FIND_SUSPECT_SQL = """
    SELECT e.ID, e.Title, e.Price, e.EndTime, e.SoldDate,
           CASE WHEN g.ID IS NOT NULL THEN 'GPU' ELSE 'CPU' END AS Category
    FROM   EBAY e
    LEFT   JOIN GPU g ON g.ID = e.ID
    LEFT   JOIN CPU c ON c.ID = e.ID
    WHERE  (g.ID IS NOT NULL OR c.ID IS NOT NULL)
    AND    e.Price < 1000
    ORDER  BY Category, e.Price
"""


def find_suspect(cur):
    cur.execute(FIND_SUSPECT_SQL)
    return cur.fetchall()


def delete_suspect(cur, ids: list[int]):
    placeholders = ", ".join(["%s"] * len(ids))
    # DealOutcomes first (FK constraint)
    cur.execute(
        f"DELETE FROM DealOutcomes WHERE EbayID IN ({placeholders})",
        ids,
    )
    outcomes_deleted = cur.rowcount

    # Category tables
    cur.execute(f"DELETE FROM GPU WHERE ID IN ({placeholders})", ids)
    cur.execute(f"DELETE FROM CPU WHERE ID IN ({placeholders})", ids)

    # Main EBAY table
    cur.execute(f"DELETE FROM EBAY WHERE ID IN ({placeholders})", ids)
    ebay_deleted = cur.rowcount

    return ebay_deleted, outcomes_deleted


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backfill corrupted price records")
    parser.add_argument("--delete", action="store_true",
                        help="Delete corrupted ended/sold records after confirmation")
    args = parser.parse_args()

    print("Connecting to DB…")
    conn = _connect()
    cur = conn.cursor()

    rows = find_suspect(cur)

    if not rows:
        print("✓ No suspect records found — database is clean.")
        cur.close()
        conn.close()
        return

    now = datetime.now()

    active   = [r for r in rows if r[2+1] is not None and r[2+1] > now and r[3+1] is None]
    # columns: ID, Title, Price, EndTime, SoldDate, Category
    active   = [r for r in rows if r[3] is not None and r[3] > now and r[4] is None]
    ended    = [r for r in rows if r not in active]

    print(f"\n{'─'*70}")
    print(f"  SUSPECT RECORDS  (Price < £10.00 in GPU/CPU listings)")
    print(f"{'─'*70}")
    print(f"  {'ID':>14}  {'Cat':4}  {'Price':>8}  {'EndTime':>20}  Title")
    print(f"{'─'*70}")
    for r in rows:
        ebay_id, title, price_p, end_time, sold_date, category = r
        flag = " [ACTIVE]" if r in active else ""
        print(f"  {ebay_id:>14}  {category:4}  £{price_p/100:>6.2f}  {str(end_time):>20}  {title[:35]}{flag}")
    print(f"{'─'*70}")
    print(f"  Total suspect: {len(rows)}  |  Active (will self-heal): {len(active)}  |  Ended/sold: {len(ended)}")
    print(f"{'─'*70}\n")

    if not args.delete:
        print("DRY RUN — no changes made.")
        print("Re-run with --delete to remove the ended/sold records.")
        print("Active records will be corrected automatically on the next full scrape.")
        cur.close()
        conn.close()
        return

    if not ended:
        print("No ended/sold records to delete — nothing to do.")
        cur.close()
        conn.close()
        return

    print(f"About to permanently DELETE {len(ended)} ended/sold record(s).")
    print("Active records ({len(active)}) will be left for the scraper to correct.")
    confirm = input("Type YES to confirm: ").strip()
    if confirm != "YES":
        print("Aborted.")
        cur.close()
        conn.close()
        return

    ids_to_delete = [r[0] for r in ended]
    ebay_del, outcomes_del = delete_suspect(cur, ids_to_delete)
    conn.commit()

    print(f"\n✓ Deleted {ebay_del} EBAY record(s) and {outcomes_del} DealOutcomes record(s).")
    print("  Active records remain — they will be re-priced on the next scheduled scrape.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
