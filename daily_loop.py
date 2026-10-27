"""
WKT retention bot: run once a day.

    1. Settings tab: stop if Kill Switch is ON
    2. Read admin outcomes from the call lists and apply them to ServiceM8 notes
    3. Rebuild every customer's status (due = last service + 12 months)
    4. Rewrite the call lists, cold outreach list and dashboard
    5. Push STAGE + cross-sell flags to Brevo

DRY_RUN (in .env, default true) reads everything and writes nothing: no ServiceM8 notes,
no sheet changes, no Brevo. Run it that way first and check the log.

    python daily_loop.py
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime

import config
import customers as cust
import sheet_sync
import brevo_sync

config.LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
                    handlers=[logging.FileHandler(config.LOG_DIR / "daily_loop.log", encoding="utf-8"),
                              logging.StreamHandler(sys.stdout)])
log = logging.getLogger("daily_loop")


def main():
    today = date.today()
    dry = config.DRY_RUN
    log.info(f"=== Retention bot {today} | DRY_RUN={dry} ===")

    settings = sheet_sync.read_settings()
    if settings.kill_switch:
        log.warning("Kill Switch is ON in the Settings tab, nothing done")
        if not dry:
            sheet_sync.write_last_run(f"{datetime.now():%Y-%m-%d %H:%M} (kill switch ON)")
        return

    snap = cust.load_snapshot()

    outcomes, read_ok = sheet_sync.read_outcomes()
    touched = sheet_sync.apply_outcomes(snap, outcomes, today)
    sheet_sync.apply_dnc_overrides(snap, settings.dnc_overrides)
    failed = cust.save_notes(snap, dry)
    skip_tabs = {t for t in sheet_sync.OUTCOME_TABS if t not in read_ok}
    skip_tabs |= {touched[c] for c in failed if c in touched}

    customers = cust.build_customers(snap, today)
    cold = cust.build_cold(snap, customers, today)
    by_status = {}
    for c in customers:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    log.info(f"Customers: {by_status}; cold leads: {len(cold)}")

    if dry:
        rows = sheet_sync.call_list_rows(customers)
        log.info("[DRY_RUN] sheet not changed. Would write: " +
                 ", ".join(f"{sheet_sync.CALL_TABS[b]}={len(r)}" for b, r in rows.items()) +
                 f", {sheet_sync.COLD_TAB}={len(cold)}")
    else:
        sheet_sync.write_lists(customers, cold, today, skip_tabs)

    try:
        brevo_sync.sync(snap, customers, cold, today, dry)
    except Exception as e:                                       # noqa: BLE001
        log.error(f"Brevo sync failed: {e}")

    if not dry:
        sheet_sync.write_last_run(f"{datetime.now():%Y-%m-%d %H:%M}")
    log.info("Done.")


if __name__ == "__main__":
    main()
