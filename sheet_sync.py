"""
Google Sheet: read the admins' call outcomes, apply them to ServiceM8 notes, rewrite the lists.

Tabs (same as before): Settings, Dashboard, Call List (0-12 / 12-24 / 24+ Months Overdue),
Cold Outreach List. Call-list columns: Name, Phone, Email, Address, Last Service, Warranty Expiry
(now = due date), Overdue Months, Last Service Type, Client UUID, Call Count, Outcome,
Callback Date, Notes.

Outcomes: Booked | Not interested | Callback requested | No answer | Do not contact
(anything else, e.g. Wrong number, is logged and ignored, as before)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import config
import notes_block
from customers import Snapshot, Customer, ColdLead, bucket_for

log = logging.getLogger("sheet_sync")

CALL_TABS = {
    "0-12":  "Call List (0-12 Months Overdue)",
    "12-24": "Call List (12-24 Months Overdue)",
    "24+":   "Call List (24+ Months Overdue)",
}
COLD_TAB = "Cold Outreach List"
OUTCOME_TABS = list(CALL_TABS.values()) + [COLD_TAB]
TRUTHY = ("TRUE", "YES", "Y", "1", "ON")

_sheet = None


def sheet():
    global _sheet
    if _sheet is None:
        import gspread
        _sheet = gspread.service_account(filename=config.GSHEET_CREDS).open_by_key(config.SHEET_ID)
    return _sheet


def tab(name):
    return sheet().worksheet(name)


# ── settings ─────────────────────────────────────────────────────────────────
@dataclass
class Settings:
    kill_switch: bool = True                 # fail-safe: paused unless the sheet says OFF
    dnc_overrides: list = field(default_factory=list)


def read_settings() -> Settings:
    s = Settings()
    try:
        rows = tab("Settings").get_all_values()
    except Exception as e:                                       # noqa: BLE001
        log.error(f"Settings unreadable, staying paused: {e}")
        return s
    for r in rows[1:]:
        if len(r) < 2:
            continue
        k, v = r[0].strip(), r[1].strip()
        if k == "Kill Switch":
            s.kill_switch = v.upper() != "OFF"
        elif k.startswith("DNC Override") and v:
            s.dnc_overrides.append(v)
    log.info(f"Settings: kill_switch={'ON' if s.kill_switch else 'OFF'}, dnc_overrides={len(s.dnc_overrides)}")
    return s


def write_last_run(text):
    try:
        t = tab("Settings")
        for i, r in enumerate(t.get_all_values()):
            if r and r[0].strip() == "Last Run Timestamp":
                t.update_cell(i + 1, 2, text)
                return
    except Exception as e:                                       # noqa: BLE001
        log.error(f"Could not write Last Run Timestamp: {e}")


# ── outcomes ─────────────────────────────────────────────────────────────────
@dataclass
class Outcome:
    tab: str
    client_uuid: str
    outcome: str
    callback_date: Optional[date]


def _parse_date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def read_outcomes():
    """Returns (outcomes, tabs_read_ok). Each tab is read exactly once."""
    outcomes, ok = [], set()
    for name in OUTCOME_TABS:
        try:
            rows = tab(name).get_all_values()
        except Exception as e:                                   # noqa: BLE001
            log.error(f"Could not read {name}: {e} (tab will not be rewritten)")
            continue
        ok.add(name)
        if not rows:
            continue
        h = [c.strip() for c in rows[0]]
        try:
            iu, io, ic = h.index("Client UUID"), h.index("Outcome"), h.index("Callback Date")
        except ValueError:
            log.error(f"{name}: missing Client UUID / Outcome / Callback Date headers")
            ok.discard(name)
            continue
        for r in rows[1:]:
            if len(r) > io and r[io].strip() and len(r) > iu and r[iu].strip():
                outcomes.append(Outcome(name, r[iu].strip(), r[io].strip(),
                                        _parse_date(r[ic]) if len(r) > ic else None))
    log.info(f"Read {len(outcomes)} outcome(s) from the call lists")
    return outcomes, ok


def _bucket_of_tab(name):
    for b, t in CALL_TABS.items():
        if t == name:
            return b
    return "cold-outreach"


def apply_outcomes(snap: Snapshot, outcomes, today: date):
    """Apply outcomes to the in-memory notes (saved later by customers.save_notes).
    Returns {client_uuid: tab} for every client touched."""
    touched = {}
    for o in outcomes:
        cn = snap.note_for(o.client_uuid)
        blk = cn.parsed.block
        b = _bucket_of_tab(o.tab)
        v = o.outcome.lower()
        if v == "booked":
            blk.bookings.append(notes_block.Booking(today, b))
        elif v == "not interested":
            blk.declines.append(notes_block.Decline(today, b, today + timedelta(days=config.SUPPRESS_DAYS)))
        elif v == "callback requested":
            if not o.callback_date:
                log.warning(f"  {o.client_uuid[:8]}: callback requested with no date, ignored")
                continue
            blk.callbacks.append(notes_block.Callback(today, o.callback_date, b))
        elif v == "no answer":
            if blk.add_no_answer(b, today) >= config.NO_ANSWER_LIMIT:
                blk.declines.append(notes_block.Decline(today, b, today + timedelta(days=config.SUPPRESS_DAYS)))
                blk.reset_attempts(b)
        elif v == "do not contact":
            blk.contact_status = "Do Not Contact"
        else:
            log.warning(f"  {o.client_uuid[:8]}: unknown outcome {o.outcome!r}, ignored")
            continue
        cn.dirty = True
        touched[o.client_uuid] = o.tab
    return touched


def apply_dnc_overrides(snap: Snapshot, uuids):
    for cu in uuids:
        cn = snap.note_for(cu)
        if not cn.parsed.block.is_dnc():
            cn.parsed.block.contact_status = "Do Not Contact"
            cn.dirty = True


# ── writing the lists ────────────────────────────────────────────────────────
def _rewrite(name, rows, ncols):
    import gspread
    t = tab(name)
    if t.row_count > 1:
        t.batch_clear([f"A2:{gspread.utils.rowcol_to_a1(t.row_count, ncols)}"])
    if rows:
        if t.row_count < len(rows) + 1:
            t.add_rows(len(rows) + 1 - t.row_count)
        t.update(values=rows, range_name="A2")
    log.info(f"{name}: wrote {len(rows)} row(s)")


def call_list_rows(customers: list[Customer]):
    out = {b: [] for b in CALL_TABS}
    for c in sorted((c for c in customers if c.status == "overdue"), key=lambda c: c.due, reverse=True):
        out[bucket_for(c.overdue_months)].append([
            c.name, c.phone, c.email, c.address, c.last_service.isoformat(), c.due.isoformat(),
            str(c.overdue_months), c.last_service_type, c.client_uuid, str(c.call_count), "", "", ""])
    return out


def cold_rows(cold: list[ColdLead]):
    return [[c.name, c.phone, c.email, c.address, c.last_date.isoformat(), str(c.months_since),
             c.job_type, c.source, c.client_uuid, "", "", ""]
            for c in sorted(cold, key=lambda c: c.last_date, reverse=True)]


def dashboard_rows(customers: list[Customer], cold: list[ColdLead], today: date):
    n = lambda f: sum(1 for c in customers if f(c))                       # noqa: E731
    ov = lambda lo, hi: n(lambda c: c.status == "overdue" and lo <= c.overdue_months <= hi)  # noqa: E731
    return [
        ["CUSTOMERS BY STATUS", "", "", f"as of {today.isoformat()}"],
        ["Not due yet", str(n(lambda c: c.status == "active")), "", "due = last service + 12 months"],
        ["0-3 months overdue", str(ov(0, 3)), "", ""],
        ["4-12 months overdue", str(ov(4, 12)), "", ""],
        ["13-24 months overdue", str(ov(13, 24)), "", ""],
        ["24+ months overdue", str(ov(25, 10 ** 6)), "", ""],
        ["Booked", str(n(lambda c: c.status == "booked")), "", "admin-marked or open work order"],
        ["Paused", str(n(lambda c: c.status == "paused")), "", "declined, callback pending or re-engagement"],
        ["Do Not Contact", str(n(lambda c: c.status == "dnc")), "", ""],
        ["Cold outreach", str(len(cold)), "", "never converted + re-engagement"],
    ]


def write_lists(customers, cold, today, skip_tabs):
    for b, rows in call_list_rows(customers).items():
        if CALL_TABS[b] in skip_tabs:
            log.warning(f"{CALL_TABS[b]}: NOT rewritten (keeps admin outcomes that failed to save)")
        else:
            _rewrite(CALL_TABS[b], rows, 13)
    if COLD_TAB in skip_tabs:
        log.warning(f"{COLD_TAB}: NOT rewritten (keeps admin outcomes that failed to save)")
    else:
        _rewrite(COLD_TAB, cold_rows(cold), 12)
    try:
        _rewrite("Dashboard", dashboard_rows(customers, cold, today), 4)
    except Exception as e:                                       # noqa: BLE001
        log.error(f"Dashboard write failed: {e}")
