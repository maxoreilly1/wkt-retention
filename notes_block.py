"""
The bot's fenced block inside each client's ServiceM8 note.

    <admin's own notes, never touched>

    === WKT BOT <em dash> DO NOT EDIT BELOW ===
    client_contact_status=Active
    [JOB:...] / [COMMERCIAL:...] / client_type_override=...   (old lines: kept exactly as they are)
    [DECLINE] outcome_date=YYYY-MM-DD last_bucket=0-12 suppress_until=YYYY-MM-DD
    [CALLBACK] requested_date=YYYY-MM-DD callback_date=YYYY-MM-DD source_bucket=0-12
    [CALL_ATTEMPTS] count=2 last_attempt=YYYY-MM-DD bucket=0-12
    [BOOKED] booked_date=YYYY-MM-DD source_bucket=0-12
    === END WKT BOT ===

Same format as the old bot, so every existing note in ServiceM8 still reads correctly.
Lines this version doesn't use are preserved untouched. Pure string handling, no API calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import config

# The fence text already in ServiceM8 contains an em dash; it must match exactly.
BEGIN_FENCE = "=== WKT BOT \u2014 DO NOT EDIT BELOW ==="
END_FENCE = "=== END WKT BOT ==="


def _fields(rest):
    out = {}
    for pair in rest.split():
        k, sep, v = pair.partition("=")
        if sep:
            out[k] = v
    return out


@dataclass
class Decline:
    outcome_date: date
    last_bucket: str
    suppress_until: date

    def active(self, today):
        return today < self.suppress_until

    def line(self):
        return (f"[DECLINE] outcome_date={self.outcome_date} last_bucket={self.last_bucket} "
                f"suppress_until={self.suppress_until}")


@dataclass
class Callback:
    requested_date: date
    callback_date: date
    source_bucket: str

    def active(self, today):
        return today < self.callback_date

    def line(self):
        return (f"[CALLBACK] requested_date={self.requested_date} callback_date={self.callback_date} "
                f"source_bucket={self.source_bucket}")


@dataclass
class CallAttempts:
    count: int
    last_attempt: date
    bucket: str

    def line(self):
        return f"[CALL_ATTEMPTS] count={self.count} last_attempt={self.last_attempt} bucket={self.bucket}"


@dataclass
class Booking:
    booked_date: date
    source_bucket: str

    def active(self, today):
        return today < self.booked_date + timedelta(days=config.BOOKED_SUPPRESS_DAYS)

    def line(self):
        return f"[BOOKED] booked_date={self.booked_date} source_bucket={self.source_bucket}"


@dataclass
class BotBlock:
    contact_status: str = "Active"
    declines: list = field(default_factory=list)
    callbacks: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    bookings: list = field(default_factory=list)
    other_lines: list = field(default_factory=list)   # old [JOB:], [COMMERCIAL:], anything unknown

    # -- state checks
    def is_dnc(self):
        return self.contact_status == "Do Not Contact"

    def suppressed(self, today):
        return any(d.active(today) for d in self.declines)

    def callback_pending(self, today):
        return any(c.active(today) for c in self.callbacks)

    def booked(self, today):
        return any(b.active(today) for b in self.bookings)

    def latest_decline(self):
        return max(self.declines, key=lambda d: d.outcome_date) if self.declines else None

    def attempts_for(self, bucket):
        return next((a.count for a in self.attempts if a.bucket == bucket), 0)

    # -- changes
    def add_no_answer(self, bucket, today):
        a = next((a for a in self.attempts if a.bucket == bucket), None)
        if a is None:
            a = CallAttempts(0, today, bucket)
            self.attempts.append(a)
        a.count += 1
        a.last_attempt = today
        return a.count

    def reset_attempts(self, bucket):
        self.attempts = [a for a in self.attempts if a.bucket != bucket]


@dataclass
class ParsedNotes:
    admin_text: str
    block: BotBlock
    trailing_text: str = ""
    had_block: bool = False


def _parse_line(line, block):
    try:
        if line.startswith("client_contact_status="):
            block.contact_status = line.split("=", 1)[1].strip()
            return True
        if line.startswith("[DECLINE]"):
            f = _fields(line[9:])
            block.declines.append(Decline(date.fromisoformat(f["outcome_date"]), f["last_bucket"],
                                          date.fromisoformat(f["suppress_until"])))
            return True
        if line.startswith("[CALLBACK]"):
            f = _fields(line[10:])
            block.callbacks.append(Callback(date.fromisoformat(f["requested_date"]),
                                            date.fromisoformat(f["callback_date"]), f["source_bucket"]))
            return True
        if line.startswith("[CALL_ATTEMPTS]"):
            f = _fields(line[15:])
            block.attempts.append(CallAttempts(int(f["count"]), date.fromisoformat(f["last_attempt"]),
                                               f["bucket"]))
            return True
        if line.startswith("[BOOKED]"):
            f = _fields(line[8:])
            block.bookings.append(Booking(date.fromisoformat(f["booked_date"]), f["source_bucket"]))
            return True
    except (KeyError, ValueError):
        return False
    return False


def parse(notes: Optional[str]) -> ParsedNotes:
    notes = notes or ""
    b = notes.find(BEGIN_FENCE)
    e = notes.find(END_FENCE, b + len(BEGIN_FENCE)) if b >= 0 else -1
    if b < 0 or e < 0:
        return ParsedNotes(admin_text=notes, block=BotBlock())
    block = BotBlock()
    for raw in notes[b + len(BEGIN_FENCE):e].splitlines():
        line = raw.strip()
        if line and not _parse_line(line, block):
            block.other_lines.append(line)
    return ParsedNotes(admin_text=notes[:b].rstrip("\n"), block=block,
                       trailing_text=notes[e + len(END_FENCE):].lstrip("\n"), had_block=True)


def serialise(p: ParsedNotes) -> str:
    out = []
    if p.admin_text:
        out += [p.admin_text.rstrip("\n"), ""]
    out.append(BEGIN_FENCE)
    out.append(f"client_contact_status={p.block.contact_status}")
    out += p.block.other_lines
    out += [x.line() for x in p.block.declines + p.block.callbacks + p.block.attempts + p.block.bookings]
    out.append(END_FENCE)
    if p.trailing_text:
        out += ["", p.trailing_text.rstrip("\n")]
    return "\n".join(out)
