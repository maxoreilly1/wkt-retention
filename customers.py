"""
One place that decides where every customer stands today. The call lists, the dashboard
and Brevo all read from this, so they can never disagree.

Status per customer (first match wins):
    dnc        client marked Do Not Contact
    booked     admin marked Booked in the last 90 days, OR a Work Order with a visit scheduled
               on the calendar from today onwards
    paused     declined in the last 6 months, a callback date not reached yet, or a
               re-engagement target (declined at 24+ months, now handled on Cold Outreach).
               Off the call lists and given no Brevo STAGE (same as the old bot).
    active     not due yet (due = last completed service + 12 months)
    overdue    past due; overdue_months = days past due // 30
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import config
import notes_block
import sm8

log = logging.getLogger("customers")


# ── helpers ──────────────────────────────────────────────────────────────────
def add_months(d, n):
    import calendar
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def months_between(earlier, later):
    m = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    return m - 1 if later.day < earlier.day else m


def _d(s):
    s = (s or "")[:10]
    if not s or s.startswith("0000"):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def service_date(job):
    """Completion date if set (when the tech actually finished), else the scheduled date."""
    return _d(job.get("completion_date")) or _d(job.get("date"))


def is_fixup(job):
    return "".join((job.get("job_description") or "").lower().split()).startswith("fix")


def is_active_rec(rec):
    return str(rec.get("active", "1")) != "0"


def bucket_for(overdue_months):
    if overdue_months <= 12:
        return "0-12"
    if overdue_months <= 24:
        return "12-24"
    return "24+"


# Service classification (same keyword tiers as the report)
_OTHER = ("surcharge", "cancellation fee", "key collection", "service location fee", "weekend callout",
          "card 2%", "gst", "tax 10%", "discount", "credit", "referral", "tip")
_TREAT = ("termite barrier", "termite management system", "termite protection", "termite bait",
          "termite reticulation", "reticulation", "trelona", "atbs", "sentricon", "live termite",
          "termite part treatment", "termite treatment", "termite foaming", "termidor",
          "chemical treated zone", "chemical barrier", "termite monitoring", "dry wood termite")
_INSPECT = ("termite inspection", "termite check", "timber pest inspection", "timber pest",
            "visual pest inspection")
_PEST = ("pest control", "general pest", "premium pest", "rodent", "cockroach", "german", "flea",
         "spider", "ant treatment", "wasp", "silverfish", "pest treatment")


def classify(text):
    n = re.sub(r"\s+", " ", (text or "").lower())
    if not n or any(k in n for k in _OTHER):
        return set()
    cats = set()
    if any(k in n for k in _TREAT):
        cats.add("TT")
    if any(k in n for k in _INSPECT):
        cats.add("TI")
    if any(k in n for k in _PEST):
        cats.add("PC")
    return cats


# ── snapshot: everything fetched once per run ────────────────────────────────
@dataclass
class ClientNote:
    record: Optional[dict]          # existing ServiceM8 Note record, or None
    parsed: notes_block.ParsedNotes
    dirty: bool = False


@dataclass
class Snapshot:
    jobs: list
    names: dict                     # client_uuid -> name
    contacts: dict                  # client_uuid -> (phone, email)
    notes: dict                     # client_uuid -> ClientNote (only clients that have a bot note)
    materials: dict                 # job_uuid -> [billing item names]
    scheduled: dict = None          # client_uuid -> [upcoming scheduled visit dates on Work Orders]

    def note_for(self, cuid):
        if cuid not in self.notes:
            self.notes[cuid] = ClientNote(None, notes_block.parse(""))
        return self.notes[cuid]

    def block(self, cuid):
        cn = self.notes.get(cuid)
        return cn.parsed.block if cn else notes_block.BotBlock()


def load_snapshot(with_materials=True) -> Snapshot:
    log.info("Fetching ServiceM8 (jobs, clients, contacts, notes%s)..." %
             (", billing items" if with_materials else ""))
    jobs = sm8.get_all("job")
    names = {c["uuid"]: (c.get("name") or "").strip() for c in sm8.get_all("company") if c.get("uuid")}
    contacts = {}
    for c in sm8.get_all("companycontact"):
        cu = c.get("company_uuid")
        if not cu or not is_active_rec(c):
            continue
        ph, em = contacts.get(cu, ("", ""))
        contacts[cu] = (ph or (c.get("mobile") or c.get("phone") or "").strip(),
                        em or (c.get("email") or "").strip().lower())
    notes = {}
    for n in sm8.get_all("note"):
        if (n.get("related_object") or "").lower() != "company" or str(n.get("active")) != "1":
            continue
        if notes_block.BEGIN_FENCE not in (n.get("note") or ""):
            continue
        cu = n.get("related_object_uuid")
        prev = notes.get(cu)
        if cu and (prev is None or (n.get("edit_date") or "") > (prev.record.get("edit_date") or "")):
            notes[cu] = ClientNote(n, notes_block.parse(n.get("note")))
    materials = defaultdict(list)
    if with_materials:
        for m in sm8.get_all("jobmaterial"):
            if m.get("job_uuid") and is_active_rec(m):
                materials[m["job_uuid"]].append(m.get("name") or "")
    wo_client = {j["uuid"]: j.get("company_uuid") for j in jobs
                 if is_active_rec(j) and (j.get("status") or "").lower() == "work order" and j.get("uuid")}
    scheduled = defaultdict(list)
    for a in sm8.get_all("jobactivity"):
        cu = wo_client.get(a.get("job_uuid"))
        d = _d(a.get("start_date"))
        if cu and d and is_active_rec(a):
            scheduled[cu].append(d)
    log.info(f"  {len(jobs)} jobs, {len(names)} clients, {len(notes)} bot notes")
    return Snapshot(jobs, names, contacts, notes, materials, scheduled)


def save_notes(snap: Snapshot, dry_run: bool):
    """Write every changed note back to ServiceM8. Returns the set of clients that failed."""
    failed = set()
    for cu, cn in snap.notes.items():
        if not cn.dirty:
            continue
        text = notes_block.serialise(cn.parsed)
        if dry_run:
            log.info(f"  [DRY_RUN] would {'update' if cn.record else 'create'} note for {cu[:8]}")
            continue
        try:
            if cn.record:
                sm8.update_note(cn.record["uuid"], text)
            else:
                sm8.create_note(cu, text)
            cn.dirty = False
        except Exception as e:                                   # noqa: BLE001
            log.error(f"  Note write failed for {cu[:8]}: {e}")
            failed.add(cu)
    return failed


# ── customer status ──────────────────────────────────────────────────────────
@dataclass
class Customer:
    client_uuid: str
    name: str
    phone: str
    email: str
    address: str
    last_service: date
    last_service_type: str
    due: date
    status: str                       # dnc | booked | paused | active | overdue
    overdue_months: int = 0
    call_count: int = 0
    reengage: bool = False


def build_customers(snap: Snapshot, today: date) -> list[Customer]:
    last = {}
    for j in snap.jobs:
        if (j.get("status") or "").lower() != "completed" or not is_active_rec(j) or is_fixup(j):
            continue
        cu, d = j.get("company_uuid"), service_date(j)
        if cu and d and (cu not in last or d > last[cu][0]):
            last[cu] = (d, j)
    scheduled = snap.scheduled or {}

    out = []
    for cu, (d, job) in last.items():
        blk = snap.block(cu)
        due = add_months(d, config.DUE_MONTHS)
        latest = blk.latest_decline()
        reengage = bool(latest and latest.last_bucket == "24+" and not blk.suppressed(today))
        if blk.is_dnc():
            status = "dnc"
        elif blk.booked(today) or any(s >= today for s in scheduled.get(cu, [])):
            status = "booked"
        elif blk.suppressed(today) or blk.callback_pending(today) or reengage:
            status = "paused"
        elif today < due:
            status = "active"
        else:
            status = "overdue"
        overdue = max(0, (today - due).days // 30)
        phone, email = snap.contacts.get(cu, ("", ""))
        out.append(Customer(
            client_uuid=cu, name=snap.names.get(cu, ""), phone=phone, email=email,
            address=(job.get("job_address") or "").strip(), last_service=d,
            last_service_type=(job.get("job_description") or "").strip()[:80], due=due, status=status,
            overdue_months=overdue, call_count=blk.attempts_for(bucket_for(overdue)), reengage=reengage))
    return out


@dataclass
class ColdLead:
    client_uuid: str
    name: str
    phone: str
    email: str
    address: str
    last_date: date
    months_since: int
    job_type: str
    source: str                       # "Never converted" | "Re-engagement target"


def build_cold(snap: Snapshot, customers: list[Customer], today: date) -> list[ColdLead]:
    """Never converted (enquired/quoted, never had a completed job) + re-engagement targets.
    Only DNC is left off (same as the old bot)."""
    completed = {c.client_uuid for c in customers}
    latest_open = {}
    for j in snap.jobs:
        cu = j.get("company_uuid")
        if not cu or cu in completed or not is_active_rec(j):
            continue
        d = service_date(j)
        if d and (cu not in latest_open or d > latest_open[cu][0]):
            latest_open[cu] = (d, j)
    out = []
    for cu, (d, j) in latest_open.items():
        blk = snap.block(cu)
        if blk.is_dnc():
            continue
        phone, email = snap.contacts.get(cu, ("", ""))
        out.append(ColdLead(cu, snap.names.get(cu, ""), phone, email, (j.get("job_address") or "").strip(),
                            d, max(0, months_between(d, today)),
                            (j.get("job_description") or "").strip()[:80], "Never converted"))
    for c in customers:
        if c.reengage and c.status == "paused":
            out.append(ColdLead(c.client_uuid, c.name, c.phone, c.email, c.address, c.last_service,
                                max(0, months_between(c.last_service, today)), c.last_service_type,
                                "Re-engagement target"))
    return out


# ── cross-sell pools for Brevo (original July definitions) ───────────────────
def _job_cats(snap, j):
    cats = classify(j.get("job_description"))
    for name in snap.materials.get(j.get("uuid"), []):
        cats |= classify(name)
    return cats


def _commercial_clients(snap):
    out = set()
    for j in snap.jobs:
        texts = [j.get("job_description") or ""] + snap.materials.get(j.get("uuid"), [])
        if j.get("company_uuid") and any("commercial" in t.lower() for t in texts):
            out.add(j["company_uuid"])
    return out


def termite_xsell(snap: Snapshot, customers: list[Customer], today: date) -> set[str]:
    """Residential pest-control customers with no termite history of any kind (inspection,
    treatment, combo or TMS quote, any job status), still under warranty with 30+ days before
    retreatment, whose pest job completed 30+ days ago."""
    termite, pest = set(), set()
    for j in snap.jobs:
        cu = j.get("company_uuid")
        if not cu:
            continue
        cats = _job_cats(snap, j)
        if cats & {"TI", "TT"}:
            termite.add(cu)
        if "PC" in cats and (j.get("status") or "").lower() == "completed":
            pest.add(cu)
    commercial = _commercial_clients(snap)
    return {c.client_uuid for c in customers
            if c.client_uuid in pest and c.client_uuid not in termite and c.client_uuid not in commercial
            and c.status == "active" and (today - c.last_service).days >= 30 and (c.due - today).days >= 30}


def tms_reactivation(snap: Snapshot, today: date) -> set[str]:
    """Residential customers quoted a termite treatment 180+ days ago (Quote or Unsuccessful) who
    have never had a termite treatment Work Order or Completed job, and are not DNC."""
    quotes, accepted = defaultdict(list), set()
    for j in snap.jobs:
        cu = j.get("company_uuid")
        if not cu or not is_active_rec(j) or "TT" not in _job_cats(snap, j):
            continue
        st = (j.get("status") or "").lower()
        if st in ("work order", "completed"):
            accepted.add(cu)
        elif st in ("quote", "unsuccessful"):
            d = service_date(j)
            if d:
                quotes[cu].append(d)
    commercial = _commercial_clients(snap)
    cutoff = today - timedelta(days=180)
    return {cu for cu, qs in quotes.items()
            if max(qs) <= cutoff and cu not in accepted and cu not in commercial
            and not snap.block(cu).is_dnc()}
