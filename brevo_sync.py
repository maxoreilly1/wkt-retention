"""
Push one STAGE per contact (plus two cross-sell flags) to Brevo. ServiceM8 is the source of
truth; Brevo is restamped every run.

STAGE (most urgent wins when two clients share an email):
    dnc, booked, overdue_24_plus, overdue_12_24, overdue_3_12, overdue_0_3, active,
    cold_never_converted
Customers who declined, have a callback pending, or are re-engagement targets get no STAGE
written (same as the old bot).

TERMITE_XSELL / TMS_REACTIVATION are separate Text attributes, always "eligible" or "not_eligible"
so they self-clear. Only contacts that have a STAGE are written. emailBlacklisted is never sent,
so unsubscribed contacts are never resubscribed.
"""
from __future__ import annotations

import logging
from datetime import date

import requests

import config
import customers as cust

log = logging.getLogger("brevo_sync")

STAGES = ["dnc", "booked", "overdue_24_plus", "overdue_12_24", "overdue_3_12",
          "overdue_0_3", "active", "cold_never_converted"]
RANK = {s: i for i, s in enumerate(STAGES)}
BREVO_BASE = "https://api.brevo.com/v3"
CHUNK = 1000


def stage_for(c: cust.Customer):
    if c.status == "paused":
        return None
    if c.status in ("dnc", "booked", "active"):
        return c.status
    m = c.overdue_months
    return ("overdue_24_plus" if m > 24 else "overdue_12_24" if m > 12
            else "overdue_3_12" if m > 3 else "overdue_0_3")


def build_payload(snap, customers, cold, today: date):
    by_client = {c.client_uuid: stage_for(c) for c in customers if stage_for(c)}
    for c in cold:
        if c.source == "Never converted" and c.client_uuid not in by_client \
                and c.months_since >= config.COLD_MIN_MONTHS:
            by_client[c.client_uuid] = "cold_never_converted"
    xsell = cust.termite_xsell(snap, customers, today)
    tms = cust.tms_reactivation(snap, today)

    by_email = {}
    for cu, st in by_client.items():
        email = snap.contacts.get(cu, ("", ""))[1]
        if not email:
            continue
        cur = by_email.get(email)
        if cur is None or RANK[st] < RANK[cur["STAGE"]]:
            by_email[email] = {"STAGE": st, "x": False, "t": False}
        by_email[email]["x"] |= cu in xsell
        by_email[email]["t"] |= cu in tms
    return [{"email": e, "attributes": {
                "STAGE": v["STAGE"],
                "TERMITE_XSELL": "eligible" if v["x"] else "not_eligible",
                "TMS_REACTIVATION": "eligible" if v["t"] else "not_eligible"}}
            for e, v in by_email.items()]


def sync(snap, customers, cold, today: date, dry_run: bool):
    contacts = build_payload(snap, customers, cold, today)
    counts = {s: 0 for s in STAGES}
    for c in contacts:
        counts[c["attributes"]["STAGE"]] += 1
    x = sum(c["attributes"]["TERMITE_XSELL"] == "eligible" for c in contacts)
    t = sum(c["attributes"]["TMS_REACTIVATION"] == "eligible" for c in contacts)
    log.info("Brevo stages: " + ", ".join(f"{s}={n}" for s, n in counts.items()))
    log.info(f"Brevo flags: TERMITE_XSELL eligible={x}, TMS_REACTIVATION eligible={t}, "
             f"contacts={len(contacts)}")
    if dry_run:
        log.info("  [DRY_RUN] nothing sent to Brevo")
        return counts
    if not config.BREVO_API_KEY:
        log.warning("  No BREVO_API_KEY in .env, skipping")
        return counts
    headers = {"api-key": config.BREVO_API_KEY, "content-type": "application/json",
               "accept": "application/json"}
    sent = 0
    for i in range(0, len(contacts), CHUNK):
        batch = contacts[i:i + CHUNK]
        r = requests.post(f"{BREVO_BASE}/contacts/import", headers=headers, timeout=120, json={
            "listIds": [config.BREVO_MASTER_LIST_ID], "updateExistingContacts": True,
            "emptyContactsAttributes": False, "jsonBody": batch})
        if r.status_code in (200, 201, 202):
            sent += len(batch)
        else:
            log.error(f"  Brevo batch failed: {r.status_code} {r.text[:200]}")
    log.info(f"  Brevo: {sent}/{len(contacts)} contacts submitted")
    return counts
