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
    conn = mysql.connector.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3305")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
    )
    cur = conn.cursor()
    cur.execute("SET time_zone = '+00:00'")   # match the stack's UTC frame
    cur.close()
    return conn


# ── queries ───────────────────────────────────────────────────────────────────

# IsActive is computed IN SQL so live/ended classification uses the database
# clock — a Python datetime.now() on a host in another timezone previously
# risked classing live auctions as ended (and --delete destroying them).
FIND_SUSPECT_SQL = """
    SELECT e.ID, e.Title, e.Price, e.EndTime, e.SoldDate,
           CASE WHEN g.ID IS NOT NULL THEN 'GPU'
                WHEN c.ID IS NOT NULL THEN 'CPU'
                WHEN h.ID IS NOT NULL THEN 'HDD'
                ELSE 'RAM' END AS Category,
           (e.SoldDate IS NULL AND e.EndTime IS NOT NULL AND e.EndTime > NOW()) AS IsActive
    FROM   EBAY e
    LEFT   JOIN GPU g ON g.ID = e.ID
    LEFT   JOIN CPU c ON c.ID = e.ID
    LEFT   JOIN HDD h ON h.ID = e.ID
    LEFT   JOIN RAM r ON r.ID = e.ID
    WHERE  (g.ID IS NOT NULL OR c.ID IS NOT NULL OR h.ID IS NOT NULL OR r.ID IS NOT NULL)
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
    cur.execute(f"DELETE FROM HDD WHERE ID IN ({placeholders})", ids)
    cur.execute(f"DELETE FROM RAM WHERE ID IN ({placeholders})", ids)

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

    # columns: ID=0, Title=1, Price=2, EndTime=3, SoldDate=4, Category=5, IsActive=6
    active   = [r for r in rows if r[6]]
    ended    = [r for r in rows if not r[6]]

    print(f"\n{'─'*70}")
    print(f"  SUSPECT RECORDS  (Price < £10.00 in GPU/CPU listings)")
    print(f"{'─'*70}")
    print(f"  {'ID':>14}  {'Cat':4}  {'Price':>8}  {'EndTime':>20}  Title")
    print(f"{'─'*70}")
    for r in rows:
        ebay_id, title, price_p, end_time, sold_date, category, _is_active = r
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
    print(f"Active records ({len(active)}) will be left for the scraper to correct.")
    print("NOTE: sub-£10 sales can be LEGITIMATE (old i3s, small RAM sticks) —")
    print("      review the list above before confirming.")
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
