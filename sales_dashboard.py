"""
WKT Sales Dashboard
-------------------
Fills the "Sales Dashboard" tab of the WKT Demand Database with, per rep per day:
    Dials | Bookings | Revenue booked (inc GST)

Data sources
  - ServiceM8  -> bookings + revenue
  - RingCentral -> outbound dials

How a "booking" is counted
  ServiceM8 doesn't record who CREATED a booking, only who last edited it.
  So this script keeps a list of every job that has ever been scheduled
  (dashboard_state.json). A job counts as a booking ONLY the first time it
  ever gets a scheduled booking. It's credited to whoever scheduled it, on
  that day.
    - Reschedules never count (the job was already booked before).
    - Fix-up jobs never count (matched by item / description keywords below).
    - Every other job counts (inc. German cockroach Treatment 2, warranty
      inspections, etc.).

Run it every hour, 7am-7pm, with Windows Task Scheduler. Hourly runs make sure
the person who made the booking is credited before anyone else edits it.

First run:  python sales_dashboard.py --seed
            (marks every job ALREADY booked as "seen", so history isn't
             counted as new bookings. Run this once only.)
Every run:  python sales_dashboard.py
Check GST:  python sales_dashboard.py --check-job 12345
            (prints what the script reads as the total for job #12345, so
             you can compare with the Billing tab in ServiceM8)
"""

import argparse
import base64
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

# ============================================================
# SETTINGS - fill these in
# ============================================================

# ServiceM8 API key (same one your other scripts use)
SM8_API_KEY = os.environ.get("SM8_API_KEY", "PASTE_SM8_API_KEY")

# RingCentral app (see SETUP.md). JWT credential, client id + secret.
RC_CLIENT_ID = os.environ.get("RC_CLIENT_ID", "PASTE_CLIENT_ID")
RC_CLIENT_SECRET = os.environ.get("RC_CLIENT_SECRET", "PASTE_CLIENT_SECRET")
RC_JWT = os.environ.get("RC_JWT", "PASTE_JWT")
RC_SERVER = "https://platform.ringcentral.com"

# Google: service account JSON (same one the Demand Database bot uses)
GOOGLE_SA_FILE = os.environ.get("GOOGLE_SA_FILE", "service_account.json")
# WKT Demand Database + the tab the dashboard goes in
DASHBOARD_SHEET_ID = "1ZYlZqqL2fNN1--t282adwLe1vnUfaEUIOjsaQDViWjU"
DASHBOARD_TAB = "Sales Dashboard"

# Phone reps: name -> ServiceM8 staff UUID + RingCentral extension NUMBER
# (the short number, e.g. "104"). Add new reps here.
REPS = {
    # "Jesse Lyle": {"sm8_uuid": "xxxxxxxx-....", "rc_ext": "104"},
}

# Show bookings made by people NOT in REPS (office, techs) as "Other: <name>"?
INCLUDE_OTHER_STAFF = True

# Jobs whose item names or description contain any of these are fix-ups
FIXUP_KEYWORDS = ["fixup", "fix up", "fix-up"]

# If --check-job shows ServiceM8's total is EX GST, set this to True
TOTAL_IS_EX_GST = False

# How many past days of dials to refresh each run
DIAL_DAYS_TO_REFRESH = 3

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "dashboard_state.json")
BRISBANE = timezone(timedelta(hours=10))  # no daylight saving in QLD

# ============================================================
# ServiceM8
# ============================================================
SM8 = "https://api.servicem8.com/api_1.0"


def sm8_get(path, params=None):
    r = requests.get(f"{SM8}/{path}", params=params or {},
                     headers={"X-Api-Key": SM8_API_KEY, "Accept": "application/json"},
                     timeout=120)
    r.raise_for_status()
    return r.json()


def sm8_staff_names():
    return {s["uuid"]: f'{s.get("first", "")} {s.get("last", "")}'.strip()
            for s in sm8_get("staff.json")}


def sm8_job(job_uuid):
    return sm8_get(f"job/{job_uuid}.json")


def sm8_job_items(job_uuid):
    return sm8_get("jobmaterial.json",
                   {"$filter": f"job_uuid eq '{job_uuid}'"})


def is_fixup(job, items):
    text = (job.get("job_description") or "").lower()
    text += " " + " ".join((i.get("name") or "").lower() for i in items)
    return any(k in text for k in FIXUP_KEYWORDS)


def job_total_inc_gst(job):
    total = float(job.get("total_invoice_amount") or 0)
    return round(total * 1.1, 2) if TOTAL_IS_EX_GST else round(total, 2)


# ============================================================
# RingCentral
# ============================================================

def rc_token():
    basic = base64.b64encode(f"{RC_CLIENT_ID}:{RC_CLIENT_SECRET}".encode()).decode()
    r = requests.post(f"{RC_SERVER}/restapi/oauth/token",
                      headers={"Authorization": f"Basic {basic}"},
                      data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                            "assertion": RC_JWT}, timeout=60)
    r.raise_for_status()
    return r.json()["access_token"]


def rc_get(token, path, params=None):
    r = requests.get(f"{RC_SERVER}{path}", params=params or {},
                     headers={"Authorization": f"Bearer {token}"}, timeout=120)
    r.raise_for_status()
    return r.json()


def rc_extension_ids(token):
    """extension number ('104') -> extension id"""
    out, page = {}, 1
    while True:
        d = rc_get(token, "/restapi/v1.0/account/~/extension",
                   {"perPage": 1000, "page": page})
        for e in d.get("records", []):
            out[str(e.get("extensionNumber"))] = e["id"]
        if page >= d.get("paging", {}).get("totalPages", 1):
            return out
        page += 1


def rc_outbound_calls_by_day(token, ext_id, day_from, day_to):
    """Count outbound voice calls per local date for one extension."""
    start = datetime.combine(day_from, datetime.min.time(), BRISBANE)
    end = datetime.combine(day_to + timedelta(days=1), datetime.min.time(), BRISBANE)
    counts, page = defaultdict(int), 1
    while True:
        d = rc_get(token, f"/restapi/v1.0/account/~/extension/{ext_id}/call-log", {
            "direction": "Outbound", "type": "Voice", "view": "Simple",
            "dateFrom": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "dateTo": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "perPage": 1000, "page": page})
        for c in d.get("records", []):
            t = datetime.fromisoformat(c["startTime"].replace("Z", "+00:00"))
            counts[t.astimezone(BRISBANE).date().isoformat()] += 1
        if not d.get("navigation", {}).get("nextPage"):
            return counts
        page += 1


# ============================================================
# State
# ============================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# ============================================================
# Steps
# ============================================================

def seed():
    if load_state():
        sys.exit("Already seeded. Delete dashboard_state.json first if you really mean it.")
    print("Reading every booking in ServiceM8 (can take a minute)...")
    acts = sm8_get("jobactivity.json", {"$filter": "activity_was_scheduled eq 1"})
    seen = sorted({a["job_uuid"] for a in acts})
    now = datetime.now(BRISBANE).strftime("%Y-%m-%d %H:%M:%S")
    save_state({"seen_jobs": seen, "last_run": now, "bookings": [], "dials": {}})
    print(f"Seeded {len(seen)} already-booked jobs. From now on, new bookings count.")


def collect_new_bookings(state, staff_names):
    seen = set(state["seen_jobs"])
    # look back 2h past the last run so nothing slips through
    since = (datetime.strptime(state["last_run"], "%Y-%m-%d %H:%M:%S")
             - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    acts = [a for a in sm8_get("jobactivity.json",
                               {"$filter": f"edit_date gt '{since}'"})
            if str(a.get("activity_was_scheduled")) == "1"]
    rep_by_uuid = {v["sm8_uuid"]: k for k, v in REPS.items()}

    # first (earliest-edited) active scheduled booking per unseen job
    first = {}
    for a in acts:
        if str(a.get("active")) != "1" or a["job_uuid"] in seen:
            continue
        if a["job_uuid"] not in first or a["edit_date"] < first[a["job_uuid"]]["edit_date"]:
            first[a["job_uuid"]] = a

    added = 0
    for job_uuid, a in first.items():
        seen.add(job_uuid)  # never count this job again, whatever happens below
        job = sm8_job(job_uuid)
        if is_fixup(job, sm8_job_items(job_uuid)):
            continue
        who = a.get("edit_by_staff_uuid") or ""
        rep = rep_by_uuid.get(who)
        if rep is None:
            if not INCLUDE_OTHER_STAFF:
                continue
            rep = "Other: " + staff_names.get(who, "Unknown")
        state["bookings"].append({
            "date": a["edit_date"][:10],
            "time": a["edit_date"][11:16],
            "rep": rep,
            "job_no": job.get("generated_job_id", ""),
            "booked_for": (a.get("start_date") or "")[:16],
            "revenue": job_total_inc_gst(job),
        })
        added += 1
    state["seen_jobs"] = sorted(seen)
    return added


def collect_dials(state):
    if not REPS:
        return
    token = rc_token()
    ext_ids = rc_extension_ids(token)
    today = datetime.now(BRISBANE).date()
    day_from = today - timedelta(days=DIAL_DAYS_TO_REFRESH - 1)
    for rep, cfg in REPS.items():
        ext_id = ext_ids.get(str(cfg.get("rc_ext")))
        if not ext_id:
            print(f"  ! RingCentral extension {cfg.get('rc_ext')} not found for {rep}")
            continue
        counts = rc_outbound_calls_by_day(token, ext_id, day_from, today)
        for i in range(DIAL_DAYS_TO_REFRESH):
            d = (day_from + timedelta(days=i)).isoformat()
            state["dials"].setdefault(d, {})[rep] = counts.get(d, 0)


def write_sheet(state):
    creds = Credentials.from_service_account_file(
        GOOGLE_SA_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(DASHBOARD_SHEET_ID)

    # ---- totals per (date, rep)
    rows = defaultdict(lambda: {"dials": 0, "bookings": 0, "revenue": 0.0})
    for d, reps in state["dials"].items():
        for rep, n in reps.items():
            rows[(d, rep)]["dials"] = n
    for b in state["bookings"]:
        rows[(b["date"], b["rep"])]["bookings"] += 1
        rows[(b["date"], b["rep"])]["revenue"] += b["revenue"]

    daily = [["Date", "Rep", "Dials", "Bookings", "Revenue booked (inc GST)"]]
    for (d, rep) in sorted(rows, key=lambda k: (k[0], k[1]), reverse=True):
        r = rows[(d, rep)]
        daily.append([d, rep, r["dials"], r["bookings"], round(r["revenue"], 2)])

    # ---- weekly (Mon-Sun)
    wk = defaultdict(lambda: {"dials": 0, "bookings": 0, "revenue": 0.0, "days": set()})
    for (d, rep), r in rows.items():
        dt = datetime.strptime(d, "%Y-%m-%d").date()
        monday = (dt - timedelta(days=dt.weekday())).isoformat()
        w = wk[(monday, rep)]
        w["dials"] += r["dials"]; w["bookings"] += r["bookings"]; w["revenue"] += r["revenue"]
        if r["dials"] or r["bookings"]:
            w["days"].add(d)
    weekly = [["Week starting (Mon)", "Rep", "Days worked", "Dials", "Bookings",
               "Revenue booked (inc GST)", "Dials per day", "Bookings per day",
               "Revenue per day"]]
    for (m, rep) in sorted(wk, key=lambda k: (k[0], k[1]), reverse=True):
        w = wk[(m, rep)]
        n = len(w["days"]) or 1
        weekly.append([m, rep, len(w["days"]), w["dials"], w["bookings"],
                       round(w["revenue"], 2), round(w["dials"] / n, 1),
                       round(w["bookings"] / n, 1), round(w["revenue"] / n, 2)])

    # ---- booking log (so any number can be checked against ServiceM8)
    log = [["Date booked", "Time", "Rep", "Job #", "Booked for", "Revenue (inc GST)"]]
    for b in sorted(state["bookings"], key=lambda b: (b["date"], b["time"]), reverse=True):
        log.append([b["date"], b["time"], b["rep"], b["job_no"], b["booked_for"], b["revenue"]])

    updated = datetime.now(BRISBANE).strftime("%d %b %Y %I:%M %p")
    ws = sh.worksheet(DASHBOARD_TAB)
    # clear old data under the headers (rows 4+), keep rows 1-3
    ws.batch_clear(["A4:E", "G4:O", "Q4:V"])
    ws.update(values=[[f"SALES DASHBOARD (filled in automatically every hour, do not edit). Last updated: {updated}"]],
              range_name="A1")
    # headers live in row 3; data starts row 4
    updates = []
    for col, data in (("A", daily), ("G", weekly), ("Q", log)):
        if len(data) > 1:
            updates.append({"range": f"{col}4", "values": data[1:]})
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")


def check_job(job_no):
    jobs = sm8_get("job.json", {"$filter": f"generated_job_id eq '{job_no}'"})
    if not jobs:
        sys.exit(f"Job #{job_no} not found")
    j = jobs[0]
    print(f"Job #{job_no}: ServiceM8 total_invoice_amount = ${float(j.get('total_invoice_amount') or 0):,.2f}")
    print("Compare with the Billing tab total in ServiceM8.")
    print("If that matches the INC GST total, leave TOTAL_IS_EX_GST = False.")
    print("If it matches the EX GST subtotal, set TOTAL_IS_EX_GST = True.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--seed", action="store_true")
    p.add_argument("--check-job")
    p.add_argument("--list-staff", action="store_true")
    args = p.parse_args()
    if args.list_staff:
        for uuid, name in sorted(sm8_staff_names().items(), key=lambda x: x[1]):
            print(f"{name:30} {uuid}")
        return
    if args.seed:
        return seed()
    if args.check_job:
        return check_job(args.check_job)

    state = load_state()
    if not state:
        sys.exit("Run once with --seed first.")
    run_started = datetime.now(BRISBANE).strftime("%Y-%m-%d %H:%M:%S")
    added = collect_new_bookings(state, sm8_staff_names())
    collect_dials(state)
    state["last_run"] = run_started
    save_state(state)
    write_sheet(state)
    print(f"Done. {added} new booking(s).")


if __name__ == "__main__":
    main()
