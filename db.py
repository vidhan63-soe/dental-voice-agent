"""
SQLite persistence.

Key tables (voice-agent-specific):
  slots                 — pre-generated 1-hour appointment slots
  appointments          — bookings (one slot per appointment)
  call_requests         — web form submissions (may preselect a slot)
  calls                 — Twilio calls (keyed by CallSid)
  transcripts           — dialogue for replay + live UI
  agent_state           — serialized Gemini contents history for worker-safe rehydration
  reschedule_requests   — reschedule asks
  escalations           — handoffs to human

All timestamps stored as ISO strings in the clinic's local timezone (see CLINIC_TZ env).
"""
import json
import os
import sqlite3
import datetime
from typing import Optional
from zoneinfo import ZoneInfo

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "clinic.db"))
CLINIC_TZ = ZoneInfo(os.getenv("CLINIC_TZ", "Asia/Kolkata"))

# Clinic working hours (local time)
OPEN_HOUR = int(os.getenv("CLINIC_OPEN_HOUR", "9"))
CLOSE_HOUR = int(os.getenv("CLINIC_CLOSE_HOUR", "19"))
CLOSED_WEEKDAYS = {6}                                   # Sunday = 6 (Mon=0)
SLOT_HORIZON_DAYS = int(os.getenv("SLOT_HORIZON_DAYS", "14"))
SLOT_DURATION_MINUTES = int(os.getenv("SLOT_DURATION_MINUTES", "60"))


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA busy_timeout=5000;")
    return c


def _now() -> str:
    return datetime.datetime.now(tz=CLINIC_TZ).isoformat(timespec="seconds")


def _now_dt() -> datetime.datetime:
    return datetime.datetime.now(tz=CLINIC_TZ)


def init_db():
    c = _conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_time TEXT UNIQUE,
        end_time TEXT,
        booked INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_slots_start ON slots(start_time);
    CREATE INDEX IF NOT EXISTS idx_slots_booked ON slots(booked, start_time);

    CREATE TABLE IF NOT EXISTS call_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        phone TEXT,
        status TEXT DEFAULT 'queued',
        call_sid TEXT,
        preselected_slot_id INTEGER,
        preselected_service TEXT,
        error TEXT,
        created_at TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS calls (
        call_sid TEXT PRIMARY KEY,
        from_number TEXT,
        to_number TEXT,
        direction TEXT,
        status TEXT DEFAULT 'in_progress',
        hangup_cause TEXT,
        summary TEXT,
        answered_by TEXT,
        started_at TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS transcripts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        call_sid TEXT,
        role TEXT,
        content TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS agent_state (
        call_sid TEXT PRIMARY KEY,
        contents_json TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slot_id INTEGER UNIQUE,
        call_sid TEXT,
        patient_name TEXT,
        phone TEXT,
        service TEXT,
        urgency TEXT,
        notes TEXT,
        status TEXT DEFAULT 'confirmed',
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS reschedule_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        call_sid TEXT,
        patient_name TEXT,
        phone TEXT,
        original_hint TEXT,
        new_slot_id INTEGER,
        notes TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS escalations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        call_sid TEXT,
        reason TEXT,
        created_at TEXT
    );
    """)
    c.close()


# =============================================================================
# Slot generation + queries
# =============================================================================
def generate_slots(days: int = SLOT_HORIZON_DAYS) -> int:
    """Ensure slots exist for the next `days` days. Idempotent."""
    now = _now_dt()
    end_date = now.date() + datetime.timedelta(days=days)

    c = _conn()
    inserted = 0
    date_cursor = now.date()
    while date_cursor < end_date:
        if date_cursor.weekday() in CLOSED_WEEKDAYS:
            date_cursor += datetime.timedelta(days=1)
            continue

        for hour in range(OPEN_HOUR, CLOSE_HOUR):
            start_dt = datetime.datetime(
                date_cursor.year, date_cursor.month, date_cursor.day,
                hour, 0, 0, tzinfo=CLINIC_TZ
            )
            end_dt = start_dt + datetime.timedelta(minutes=SLOT_DURATION_MINUTES)
            if start_dt <= now:
                continue
            try:
                cur = c.execute(
                    "INSERT OR IGNORE INTO slots(start_time, end_time, booked, created_at)"
                    " VALUES (?,?,0,?)",
                    (start_dt.isoformat(timespec="seconds"),
                     end_dt.isoformat(timespec="seconds"),
                     _now()),
                )
                if cur.rowcount > 0:
                    inserted += 1
            except sqlite3.IntegrityError:
                pass
        date_cursor += datetime.timedelta(days=1)

    c.close()
    return inserted


def get_slot(slot_id: int) -> Optional[dict]:
    c = _conn()
    row = c.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def list_vacant_slots_by_date(date_iso: str) -> list[dict]:
    """Vacant slots on a given date (YYYY-MM-DD) in clinic TZ."""
    c = _conn()
    start_of_day = f"{date_iso}T00:00:00"
    end_of_day = f"{date_iso}T23:59:59"
    rows = c.execute(
        "SELECT * FROM slots WHERE booked=0 AND start_time BETWEEN ? AND ? AND start_time > ?"
        " ORDER BY start_time",
        (start_of_day, end_of_day, _now()),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def list_vacant_dates(horizon_days: int = SLOT_HORIZON_DAYS) -> list[str]:
    """Distinct YYYY-MM-DD dates that have at least one vacant future slot."""
    c = _conn()
    horizon = (_now_dt() + datetime.timedelta(days=horizon_days)).isoformat(timespec="seconds")
    rows = c.execute(
        "SELECT DISTINCT substr(start_time, 1, 10) AS date FROM slots"
        " WHERE booked=0 AND start_time > ? AND start_time < ? ORDER BY date",
        (_now(), horizon),
    ).fetchall()
    c.close()
    return [r["date"] for r in rows]


def book_slot(slot_id: int) -> bool:
    """Mark slot booked. Returns False if not found / already booked."""
    c = _conn()
    cur = c.execute("UPDATE slots SET booked=1 WHERE id=? AND booked=0", (slot_id,))
    ok = cur.rowcount > 0
    c.close()
    return ok


def release_slot(slot_id: int):
    c = _conn()
    c.execute("UPDATE slots SET booked=0 WHERE id=?", (slot_id,))
    c.close()


# =============================================================================
# call_requests
# =============================================================================
def create_call_request(name: str, phone: str,
                        preselected_slot_id: Optional[int] = None,
                        preselected_service: Optional[str] = None) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO call_requests(name, phone, status, preselected_slot_id, preselected_service,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (name, phone, "queued", preselected_slot_id, preselected_service, _now(), _now()),
    )
    rid = cur.lastrowid
    c.close()
    return rid


def update_call_request(req_id: int, **fields):
    allowed = {"status", "call_sid", "error"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?"); vals.append(_now()); vals.append(req_id)
    c = _conn()
    c.execute(f"UPDATE call_requests SET {', '.join(sets)} WHERE id=?", vals)
    c.close()


def get_call_request(req_id: int) -> Optional[dict]:
    c = _conn()
    row = c.execute("SELECT * FROM call_requests WHERE id=?", (req_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def get_call_request_by_sid(call_sid: str) -> Optional[dict]:
    c = _conn()
    row = c.execute("SELECT * FROM call_requests WHERE call_sid=?", (call_sid,)).fetchone()
    c.close()
    return dict(row) if row else None


# =============================================================================
# calls
# =============================================================================
def upsert_call(call_sid: str, **fields):
    c = _conn()
    row = c.execute("SELECT call_sid FROM calls WHERE call_sid=?", (call_sid,)).fetchone()
    if row:
        allowed = {"from_number", "to_number", "direction", "status",
                   "hangup_cause", "summary", "answered_by"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?"); vals.append(v)
        if sets:
            sets.append("updated_at=?"); vals.append(_now()); vals.append(call_sid)
            c.execute(f"UPDATE calls SET {', '.join(sets)} WHERE call_sid=?", vals)
    else:
        c.execute(
            "INSERT INTO calls(call_sid, from_number, to_number, direction, status,"
            " answered_by, started_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (call_sid,
             fields.get("from_number"),
             fields.get("to_number"),
             fields.get("direction", "outbound"),
             fields.get("status", "in_progress"),
             fields.get("answered_by"),
             _now(), _now()),
        )
    c.close()


def get_call(call_sid: str) -> Optional[dict]:
    c = _conn()
    row = c.execute("SELECT * FROM calls WHERE call_sid=?", (call_sid,)).fetchone()
    c.close()
    return dict(row) if row else None


def list_calls(limit: int = 100) -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT * FROM calls ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


# =============================================================================
# transcripts
# =============================================================================
def add_transcript(call_sid: str, role: str, content: str):
    c = _conn()
    c.execute(
        "INSERT INTO transcripts(call_sid, role, content, created_at) VALUES (?,?,?,?)",
        (call_sid, role, content, _now()),
    )
    c.close()


def load_transcript(call_sid: str) -> list[dict]:
    c = _conn()
    rows = c.execute(
        "SELECT role, content, created_at FROM transcripts WHERE call_sid=? ORDER BY id",
        (call_sid,),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


# =============================================================================
# agent_state
# =============================================================================
def save_agent_state(call_sid: str, contents_json: str):
    c = _conn()
    c.execute(
        "INSERT INTO agent_state(call_sid, contents_json, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(call_sid) DO UPDATE SET contents_json=excluded.contents_json, updated_at=excluded.updated_at",
        (call_sid, contents_json, _now()),
    )
    c.close()


def load_agent_state(call_sid: str) -> Optional[list]:
    c = _conn()
    row = c.execute("SELECT contents_json FROM agent_state WHERE call_sid=?", (call_sid,)).fetchone()
    c.close()
    if not row:
        return None
    try:
        return json.loads(row["contents_json"])
    except Exception:
        return None


# =============================================================================
# appointments
# =============================================================================
def insert_appointment(**f) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO appointments(slot_id, call_sid, patient_name, phone, service,"
        " urgency, notes, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (f["slot_id"], f.get("call_sid"), f["patient_name"], f["phone"],
         f["service"], f["urgency"], f.get("notes"), "confirmed", _now()),
    )
    aid = cur.lastrowid
    c.close()
    return aid


def list_appointments(limit: int = 200) -> list[dict]:
    c = _conn()
    rows = c.execute("""
        SELECT a.*, s.start_time AS slot_start, s.end_time AS slot_end
        FROM appointments a
        LEFT JOIN slots s ON s.id = a.slot_id
        ORDER BY s.start_time DESC LIMIT ?
    """, (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def list_slots_with_appointments(only: Optional[str] = None, limit: int = 500) -> list[dict]:
    """Every future slot joined with appointment info if booked. only ∈ {'vacant','booked',None}"""
    where = []
    if only == "vacant":
        where.append("s.booked = 0")
    elif only == "booked":
        where.append("s.booked = 1")
    where.append("s.start_time > ?")
    args = [_now()]

    sql = """
        SELECT s.id AS slot_id, s.start_time, s.end_time, s.booked,
               a.id AS appointment_id, a.patient_name, a.phone, a.service,
               a.urgency, a.notes, a.status AS appt_status, a.created_at AS booked_at,
               a.call_sid
        FROM slots s
        LEFT JOIN appointments a ON a.slot_id = s.id
        WHERE """ + " AND ".join(where) + """
        ORDER BY s.start_time ASC LIMIT ?
    """
    args.append(limit)

    c = _conn()
    rows = c.execute(sql, args).fetchall()
    c.close()
    return [dict(r) for r in rows]


def find_appointment_by_name(name_substring: str) -> list[dict]:
    """Fuzzy lookup for reschedule."""
    c = _conn()
    rows = c.execute("""
        SELECT a.*, s.start_time AS slot_start
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        WHERE a.status='confirmed' AND LOWER(a.patient_name) LIKE ?
        ORDER BY s.start_time
    """, (f"%{name_substring.lower()}%",)).fetchall()
    c.close()
    return [dict(r) for r in rows]


# =============================================================================
# reschedule + escalations
# =============================================================================
def insert_reschedule(**f) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO reschedule_requests(call_sid, patient_name, phone, original_hint,"
        " new_slot_id, notes, created_at) VALUES (?,?,?,?,?,?,?)",
        (f.get("call_sid"), f["patient_name"], f.get("phone"),
         f.get("original_hint", ""), f.get("new_slot_id"), f.get("notes"), _now()),
    )
    rid = cur.lastrowid
    c.close()
    return rid


def insert_escalation(call_sid: str, reason: str):
    c = _conn()
    c.execute(
        "INSERT INTO escalations(call_sid, reason, created_at) VALUES (?,?,?)",
        (call_sid, reason, _now()),
    )
    c.close()
