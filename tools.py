"""
Tools exposed to Gemini.

The new booking model: agent first calls check_availability() with a date hint
like "Tuesday afternoon" or "next Wednesday morning" or "earliest available".
The tool resolves that to a list of concrete vacant slots and returns them.
The agent then reads the options aloud and, once the caller picks one, calls
book_appointment(slot_id, ...) to commit the booking.
"""
import datetime
import re
from typing import Any, Optional
from zoneinfo import ZoneInfo

import db


CLINIC_TZ = db.CLINIC_TZ


# ===========================================================================
# Natural-language date parsing (simple, good enough for the common cases)
# ===========================================================================
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _resolve_date_hint(hint: str) -> list[str]:
    """
    Parse 'tomorrow', 'Tuesday', 'next Wednesday', 'Apr 22', '2026-04-22', etc.
    Returns a list of candidate YYYY-MM-DD dates to search. May return multiple
    candidates if the hint is ambiguous (e.g., "this week" → 5 weekdays).

    Rules (best-effort, not bulletproof):
    - "today" / empty → today
    - "tomorrow" → today + 1
    - weekday names → next occurrence of that weekday
    - "next <weekday>" → that weekday at least 7 days out
    - "this week" → next 5 weekdays
    - "earliest" / "asap" / "soon" → empty list (caller means "anything ASAP")
    - "next week" → the 7 days starting next Monday
    - Explicit ISO date (YYYY-MM-DD) → that exact date
    - "apr 22", "april 22" → Apr 22 of current year (bump year if in past)
    """
    if not hint:
        return [_today().isoformat()]

    h = hint.lower().strip()
    today = _today()

    if any(w in h for w in ("earliest", "asap", "as soon as", "any time", "anytime")):
        return []  # caller means any vacant slot

    if "tomorrow" in h:
        return [(today + datetime.timedelta(days=1)).isoformat()]
    if "today" in h and "tomorrow" not in h:
        return [today.isoformat()]

    # ISO date
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", h)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return [datetime.date(y, mo, d).isoformat()]
        except ValueError:
            pass

    # "next week" → Monday of next week → 5 days
    if "next week" in h:
        days_until_monday = (7 - today.weekday()) % 7 or 7
        start = today + datetime.timedelta(days=days_until_monday)
        return [(start + datetime.timedelta(days=i)).isoformat() for i in range(6)]

    # "this week"
    if "this week" in h:
        return [(today + datetime.timedelta(days=i)).isoformat()
                for i in range(6) if (today + datetime.timedelta(days=i)).weekday() != 6]

    # "next <weekday>"
    for name, wd in _WEEKDAYS.items():
        if f"next {name}" in h:
            days_ahead = (wd - today.weekday()) % 7
            if days_ahead < 7:
                days_ahead += 7
            return [(today + datetime.timedelta(days=days_ahead)).isoformat()]

    # Bare weekday name → next occurrence (including today if today matches and future slots exist)
    for name, wd in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", h):
            days_ahead = (wd - today.weekday()) % 7
            if days_ahead == 0:
                # today itself — only if there are still future slots left
                return [today.isoformat(), (today + datetime.timedelta(days=7)).isoformat()]
            return [(today + datetime.timedelta(days=days_ahead)).isoformat()]

    # "apr 22" / "april 22"
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
        "jul": 7, "july": 7, "aug": 8, "august": 8,
        "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
        "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    for name, mo in months.items():
        m = re.search(rf"\b{name}\b\s*(\d{{1,2}})", h)
        if m:
            day = int(m.group(1))
            year = today.year
            try:
                d = datetime.date(year, mo, day)
                if d < today:
                    d = datetime.date(year + 1, mo, day)
                return [d.isoformat()]
            except ValueError:
                pass

    # Give up — return today + next 3 days as candidates
    return [(today + datetime.timedelta(days=i)).isoformat() for i in range(4)]


def _today() -> datetime.date:
    return datetime.datetime.now(tz=CLINIC_TZ).date()


def _resolve_time_hint(hint: str) -> Optional[tuple[int, int]]:
    """Return (min_hour, max_hour) if hint has a time-of-day indicator."""
    if not hint:
        return None
    h = hint.lower()
    if "morning" in h:
        return (9, 12)
    if "afternoon" in h:
        return (12, 17)
    if "evening" in h:
        return (17, 19)
    # explicit hour like "3pm" / "3 pm" / "15:00"
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", h)
    if m:
        hh = int(m.group(1))
        ap = m.group(3)
        if ap == "pm" and hh < 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
        return (hh, hh + 1)
    m = re.search(r"\b(\d{1,2}):00\b", h)
    if m:
        hh = int(m.group(1))
        return (hh, hh + 1)
    return None


def _format_slot_for_speech(slot: dict) -> str:
    """Turn 2026-04-22T15:00:00+05:30 into 'Wednesday, April 22 at 3 PM'."""
    dt = datetime.datetime.fromisoformat(slot["start_time"])
    day_name = dt.strftime("%A")
    month = dt.strftime("%B")
    day = dt.day
    hour_12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{day_name}, {month} {day} at {hour_12} {ampm}"


# ===========================================================================
# Tool schemas (Gemini function declarations)
# ===========================================================================
TOOL_SCHEMAS = [
    {
        "name": "check_availability",
        "description": (
            "Look up vacant appointment slots matching a date hint. Use this BEFORE "
            "offering a specific time to a caller — never invent times. The hint can "
            "be vague ('Tuesday afternoon', 'earliest available', 'next week') or "
            "specific ('April 22'). Returns a list of vacant slots with slot IDs that "
            "you will later pass to book_appointment."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date_hint": {
                    "type": "STRING",
                    "description": "Date part of caller's preference. E.g. 'Tuesday', 'next Wednesday', 'tomorrow', 'earliest available'.",
                },
                "time_hint": {
                    "type": "STRING",
                    "description": "Optional time-of-day. E.g. 'morning', 'afternoon', 'evening', '3pm'.",
                },
                "max_results": {
                    "type": "INTEGER",
                    "description": "Cap on slots returned. Default 5.",
                },
            },
            "required": ["date_hint"],
        },
    },
    {
        "name": "book_appointment",
        "description": (
            "Commit a booking. Only call after you've confirmed the slot_id with the "
            "caller (from a prior check_availability response). Do not invent slot_ids."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "slot_id": {"type": "INTEGER", "description": "The exact slot_id from check_availability."},
                "patient_name": {"type": "STRING"},
                "phone": {"type": "STRING"},
                "service": {
                    "type": "STRING",
                    "description": "One of: checkup, cleaning, tooth_pain, cavity, root_canal_consult, crown, emergency, other",
                },
                "urgency": {"type": "STRING", "enum": ["routine", "soon", "emergency"]},
                "notes": {"type": "STRING"},
            },
            "required": ["slot_id", "patient_name", "phone", "service", "urgency"],
        },
    },
    {
        "name": "reschedule_appointment",
        "description": (
            "Reschedule an existing appointment. Use check_availability to find a new "
            "vacant slot, then call this with the new slot_id and the caller's name."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "patient_name": {"type": "STRING"},
                "phone": {"type": "STRING"},
                "original_hint": {"type": "STRING"},
                "new_slot_id": {"type": "INTEGER"},
                "notes": {"type": "STRING"},
            },
            "required": ["patient_name", "new_slot_id"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "Transfer the call to a human receptionist. Use for explicit human requests, "
            "repeated misunderstandings, complaints, or emergencies needing triage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "reason": {"type": "STRING"},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "end_call",
        "description": "End the call politely. Only use after the caller has said goodbye or the task is done.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "summary": {"type": "STRING"},
            },
            "required": ["summary"],
        },
    },
]


# ===========================================================================
# Tool execution
# ===========================================================================
def _slots_matching(date_hint: str, time_hint: str = "",
                    max_results: int = 5) -> list[dict]:
    candidates = _resolve_date_hint(date_hint)
    time_range = _resolve_time_hint(time_hint) if time_hint else None

    results: list[dict] = []
    if not candidates:
        # "earliest available" — grab the next vacant slots regardless of date
        c = db._conn()
        rows = c.execute(
            "SELECT * FROM slots WHERE booked=0 AND start_time > ? ORDER BY start_time LIMIT ?",
            (db._now(), max_results),
        ).fetchall()
        c.close()
        results = [dict(r) for r in rows]
    else:
        for date_iso in candidates:
            day_slots = db.list_vacant_slots_by_date(date_iso)
            if time_range:
                lo, hi = time_range
                day_slots = [s for s in day_slots
                             if lo <= datetime.datetime.fromisoformat(s["start_time"]).hour < hi]
            results.extend(day_slots)
            if len(results) >= max_results:
                break
        results = results[:max_results]

    return results


def run_tool(name: str, args: dict,
             caller_phone: str = "", call_sid: str = "") -> dict[str, Any]:
    try:
        if name == "check_availability":
            date_hint = args.get("date_hint", "")
            time_hint = args.get("time_hint", "")
            max_results = int(args.get("max_results", 5))
            slots = _slots_matching(date_hint, time_hint, max_results)
            # Return a compact, speech-friendly summary
            return {
                "ok": True,
                "count": len(slots),
                "slots": [
                    {
                        "slot_id": s["id"],
                        "start_time": s["start_time"],
                        "spoken_form": _format_slot_for_speech(s),
                    }
                    for s in slots
                ],
                "message": (
                    f"Found {len(slots)} vacant slot(s) matching '{date_hint}'."
                    if slots else f"No vacant slots matched '{date_hint}'. Try another day or 'earliest available'."
                ),
            }

        if name == "book_appointment":
            slot_id = int(args["slot_id"])
            slot = db.get_slot(slot_id)
            if not slot:
                return {"ok": False, "error": f"No slot with id {slot_id}. Re-run check_availability."}
            if slot["booked"]:
                return {"ok": False, "error": "That slot was just taken. Please check availability again."}
            # Flip the slot and write the appointment
            if not db.book_slot(slot_id):
                return {"ok": False, "error": "Slot couldn't be locked. Please try another time."}
            try:
                aid = db.insert_appointment(
                    slot_id=slot_id,
                    call_sid=call_sid,
                    patient_name=args["patient_name"],
                    phone=args.get("phone") or caller_phone,
                    service=args["service"],
                    urgency=args["urgency"],
                    notes=args.get("notes"),
                )
            except Exception as e:
                # If we fail to insert, release the slot
                db.release_slot(slot_id)
                return {"ok": False, "error": f"Booking failed: {e}"}
            return {
                "ok": True,
                "appointment_id": aid,
                "slot_spoken_form": _format_slot_for_speech(slot),
                "message": f"Booked appointment #{aid} for {_format_slot_for_speech(slot)}.",
            }

        if name == "reschedule_appointment":
            new_slot_id = int(args["new_slot_id"])
            slot = db.get_slot(new_slot_id)
            if not slot:
                return {"ok": False, "error": f"No slot with id {new_slot_id}."}
            if slot["booked"]:
                return {"ok": False, "error": "That time was just taken."}
            rid = db.insert_reschedule(
                call_sid=call_sid,
                patient_name=args["patient_name"],
                phone=args.get("phone") or caller_phone,
                original_hint=args.get("original_hint", ""),
                new_slot_id=new_slot_id,
                notes=args.get("notes"),
            )
            return {
                "ok": True,
                "request_id": rid,
                "message": f"Reschedule request #{rid} saved for {_format_slot_for_speech(slot)}. Staff will confirm.",
            }

        if name == "escalate_to_human":
            db.insert_escalation(call_sid=call_sid, reason=args.get("reason", ""))
            return {"ok": True, "escalate": True, "message": "Transferring to receptionist."}

        if name == "end_call":
            db.upsert_call(call_sid, summary=args.get("summary", ""))
            return {"ok": True, "end_call": True}

        return {"ok": False, "error": f"unknown tool {name}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
