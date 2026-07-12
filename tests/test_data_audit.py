"""Live data-quality audit — proactively catches classification / lot
pollution in the DEPLOYED database before it skews a median (the RTX-3050
laptops, the Xeon-lot-as-single sales, cross-classified drives, etc.).

Marked `live`: it needs the Tower DB, so it SKIPS without one — the offline
suite (`-m "not live"`) never runs it. Run it on demand against Tower:

    pytest tests/test_data_audit.py -m live -s

`-s` shows the printed report even when the asserts pass, so you can eyeball
the worst outliers each time. It's the same engine the scheduler runs hourly
as a canary (EbayScraper.audit_data_quality)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _has_db():
    if not os.environ.get('DB_HOST'):
        return False
    try:
        import EbayScraper
        conn = EbayScraper._get_connection()
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.live
needs_db = pytest.mark.skipif(not _has_db(), reason="no reachable DB")


@pytest.fixture(scope="module")
def audit():
    import EbayScraper
    return EbayScraper.audit_data_quality()


def _report(a):
    lines = ["", "=== DATA-QUALITY AUDIT ===",
             f"rows per category: {a['counts']}"]
    for key, label in [
        ('reparse_rejects', "rows the current parser would now REJECT (pollution)"),
        ('lot_mismatch', "rows whose stored quantity != re-parsed quantity"),
        ('gpu_lots', "GPU rows with quantity>1 (GPU has no lots)"),
        ('price_outliers', "single sold rows priced >2.5x their group median"),
    ]:
        items = a[key]
        lines.append(f"\n-- {label}: {len(items)}"
                     + (" (capped)" if len(items) >= 25 else ""))
        for it in items[:15]:
            lines.append("   " + str(it))
    return "\n".join(lines)


@needs_db
def test_audit_report(audit, capsys):
    with capsys.disabled():
        print(_report(audit))


@needs_db
def test_no_gpu_lots(audit):
    # a GPU is never a multi-unit lot — "Ventus 2X"/"x16" misreads must not exist
    assert not audit['gpu_lots'], \
        f"GPU rows wrongly marked as lots: {audit['gpu_lots'][:5]}"


@needs_db
def test_pollution_rate_low(audit):
    # some churn is normal (rows scraped seconds ago, mid-enrichment), but a
    # SPIKE in parser-rejections means a new leak the gates don't catch yet
    total = sum(audit['counts'].values()) or 1
    rejects = len(audit['reparse_rejects'])
    rate = rejects / total
    assert rate < 0.01, (
        f"{rejects}/{total} ({rate:.1%}) stored rows would be rejected by the "
        f"current parser — a classification gate is leaking. Examples: "
        f"{audit['reparse_rejects'][:8]}")


@needs_db
def test_lots_labelled_consistently(audit):
    # the parser and the stored quantity must agree (a "10x Xeon" counted as 1
    # pollutes the single-unit median)
    total = sum(audit['counts'].values()) or 1
    assert len(audit['lot_mismatch']) / total < 0.01, (
        f"{len(audit['lot_mismatch'])} rows have a mislabelled lot quantity: "
        f"{audit['lot_mismatch'][:8]}")
