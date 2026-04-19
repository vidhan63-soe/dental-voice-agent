from dotenv import load_dotenv
load_dotenv()
"""
Bright Smile Dental — Voice AI Agent (Flask + Twilio + Sarvam + Gemini).

Public routes:
  GET  /                             landing page
  POST /api/request-call             submit demo request, trigger outbound call
  GET  /status/<req_id>              status page with live transcript
  GET  /api/status/<req_id>          polling
  GET  /api/transcript/<call_sid>    polling
  GET  /api/vacant-dates             JSON list of dates with free slots
  GET  /api/vacant-slots?date=...    JSON list of free slots on a date

Twilio webhooks:
  POST /voice/answered   — after pickup (or AMD result)
  POST /voice/turn       — after each utterance recorded
  POST /voice/status     — lifecycle events
Audio:
  GET  /audio/<id>.wav
  GET  /opening.wav
Admin:
  GET  /dashboard
  GET  /dashboard/<call_sid>
  GET  /api/appointments.csv
"""
import datetime
from dotenv import load_dotenv
load_dotenv()
import logging
import os
import re
import uuid as uuidlib
from pathlib import Path

from flask import (
    Flask, request, jsonify, render_template, send_file, abort,
    make_response, Response,
)
from zoneinfo import ZoneInfo

import db
import agent as agent_mod
from sarvam import sarvam_stt, sarvam_tts
from telephony import (
    trigger_outbound_call, is_whitelisted,
    twiml_opening, twiml_turn_reply, twiml_speak_reply,
    twiml_final_and_hangup, twiml_escalate_dial, twiml_hangup,
    download_twilio_recording,
)
from prompts import OUTBOUND_OPENING_TEXT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("app")

HERE = Path(__file__).resolve().parent
AUDIO_DIR = Path(os.getenv("AUDIO_DIR", HERE / "audio"))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
OPENING_PATH = AUDIO_DIR / "opening.wav"

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
RECEPTIONIST_PHONE = os.getenv("RECEPTIONIST_PHONE", "")
CLINIC_TZ = ZoneInfo(os.getenv("CLINIC_TZ", "Asia/Kolkata"))

app = Flask(__name__, template_folder=str(HERE / "templates"), static_folder=str(HERE / "static"))

# Initialize + top up slots at import time (cheap if already populated)
db.init_db()
try:
    added = db.generate_slots()
    if added:
        log.info(f"Generated {added} new slots")
except Exception:
    log.exception("Slot generation failed at startup")


# ============================================================================
# Helpers
# ============================================================================
def save_tts(wav_bytes: bytes) -> str:
    aid = uuidlib.uuid4().hex
    p = AUDIO_DIR / f"{aid}.wav"
    p.write_bytes(wav_bytes)
    return f"{PUBLIC_BASE_URL}/audio/{aid}.wav"


def normalize_phone(raw: str) -> str:
    s = re.sub(r"[^\d+]", "", raw or "")
    if not s:
        return ""
    if not s.startswith("+"):
        s = "+" + s.lstrip("0")
    return s


def opening_audio_url() -> str:
    return f"{PUBLIC_BASE_URL}/opening.wav"


def ensure_opening_exists():
    if OPENING_PATH.exists() and OPENING_PATH.stat().st_size > 0:
        return
    try:
        log.info("Generating opening.wav (one-time)...")
        wav = sarvam_tts(OUTBOUND_OPENING_TEXT)
        OPENING_PATH.write_bytes(wav)
    except Exception:
        log.exception("Failed to pre-generate opening.wav")


def format_slot_for_display(iso: str) -> str:
    """Turn 2026-04-22T15:00:00+05:30 into '3:00 PM' for admin UI."""
    dt = datetime.datetime.fromisoformat(iso)
    return dt.strftime("%-I:%M %p") if os.name != "nt" else dt.strftime("%I:%M %p").lstrip("0")


def format_date_for_display(iso: str) -> str:
    dt = datetime.datetime.fromisoformat(iso)
    return dt.strftime("%A, %B %-d") if os.name != "nt" else dt.strftime("%A, %B %d")


# ============================================================================
# Public pages
# ============================================================================
@app.get("/")
def landing():
    return render_template("demo.html")


@app.post("/api/request-call")
def request_call():
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    phone = normalize_phone(data.get("phone") or "")
    slot_id = data.get("slot_id")
    service = (data.get("service") or "").strip() or None

    if not name or len(name) > 80:
        return jsonify(ok=False, error="Please enter your name."), 400
    if not phone or len(phone) < 8:
        return jsonify(ok=False, error="Please enter a valid phone number with country code."), 400

    preselected_slot_id = None
    if slot_id:
        try:
            preselected_slot_id = int(slot_id)
            slot = db.get_slot(preselected_slot_id)
            if not slot:
                return jsonify(ok=False, error="Selected slot no longer exists."), 400
            if slot["booked"]:
                return jsonify(ok=False, error="Selected slot was just taken. Please pick another."), 409
        except ValueError:
            return jsonify(ok=False, error="Invalid slot selection."), 400

    req_id = db.create_call_request(
        name, phone,
        preselected_slot_id=preselected_slot_id,
        preselected_service=service,
    )

    if not is_whitelisted(phone):
        db.update_call_request(req_id, status="failed", error="number_not_whitelisted")
        return jsonify(
            ok=False, request_id=req_id,
            error=(
                "This number isn't whitelisted in the telephony trial account. "
                "Please share it so it can be verified — the call will work within "
                "a couple of minutes after that."
            ),
        ), 403

    try:
        ensure_opening_exists()
        call_sid = trigger_outbound_call(phone, req_id)
        db.update_call_request(req_id, call_sid=call_sid, status="dialing")
        return jsonify(ok=True, request_id=req_id)
    except Exception as e:
        log.exception("Failed to trigger outbound call")
        db.update_call_request(req_id, status="failed", error=str(e)[:300])
        return jsonify(ok=False, request_id=req_id,
                       error="Telephony provider rejected the call. Check server logs."), 500


@app.get("/status/<int:req_id>")
def status_page(req_id: int):
    req = db.get_call_request(req_id)
    if not req:
        abort(404)
    return render_template("status.html", req=req)


@app.get("/api/status/<int:req_id>")
def api_status(req_id: int):
    req = db.get_call_request(req_id)
    if not req:
        return jsonify(ok=False), 404
    call = db.get_call(req["call_sid"]) if req.get("call_sid") else None
    return jsonify(ok=True, request=req, call=call)


@app.get("/api/transcript/<call_sid>")
def api_transcript(call_sid: str):
    turns = db.load_transcript(call_sid)
    return jsonify(ok=True, turns=turns)


# ============================================================================
# Slot picker APIs (for the demo form's cascading dropdowns)
# ============================================================================
@app.get("/api/vacant-dates")
def api_vacant_dates():
    """Distinct YYYY-MM-DD dates with at least one vacant future slot."""
    dates = db.list_vacant_dates()
    out = []
    for d in dates:
        dt = datetime.date.fromisoformat(d)
        out.append({
            "date": d,
            "label": dt.strftime("%A, %b %d"),
        })
    return jsonify(ok=True, dates=out)


@app.get("/api/vacant-slots")
def api_vacant_slots():
    date = request.args.get("date")
    if not date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return jsonify(ok=False, error="date param required (YYYY-MM-DD)"), 400
    slots = db.list_vacant_slots_by_date(date)
    out = []
    for s in slots:
        dt = datetime.datetime.fromisoformat(s["start_time"])
        label = dt.strftime("%I:%M %p").lstrip("0")
        out.append({"slot_id": s["id"], "start_time": s["start_time"], "label": label})
    return jsonify(ok=True, slots=out)


# ============================================================================
# Twilio webhooks
# ============================================================================
@app.post("/voice/answered")
def voice_answered():
    """Call picked up. AnsweredBy tells us if it was a human or a machine."""
    req_id = request.args.get("req_id", type=int)
    call_sid = request.form.get("CallSid") or ""
    answered_by = (request.form.get("AnsweredBy") or "").lower()
    log.info(f"[req={req_id}] answered sid={call_sid} AnsweredBy={answered_by!r}")

    if call_sid:
        db.upsert_call(
            call_sid,
            from_number=request.form.get("From"),
            to_number=request.form.get("To"),
            direction="outbound",
            status="connected" if answered_by == "human" else "machine_detected",
            answered_by=answered_by or None,
        )
    if req_id and call_sid:
        db.update_call_request(req_id, call_sid=call_sid)

    # Hang up on anything non-human
    if answered_by in ("machine_start", "fax", "machine_end_beep",
                       "machine_end_silence", "machine_end_other"):
        if req_id:
            db.update_call_request(req_id, status="ended", error="voicemail_detected")
        return Response(twiml_hangup(), mimetype="application/xml")

    # 'human' (or 'unknown' — treat as human; better than hanging up)
    if req_id:
        db.update_call_request(req_id, status="connected")

    db.add_transcript(call_sid, "assistant", OUTBOUND_OPENING_TEXT)

    qs = f"?req_id={req_id}" if req_id else ""
    xml = twiml_opening(
        opening_audio_url=opening_audio_url(),
        turn_url=f"{PUBLIC_BASE_URL}/voice/turn{qs}",
    )
    return Response(xml, mimetype="application/xml")


@app.post("/voice/turn")
def voice_turn():
    req_id = request.args.get("req_id", type=int)
    call_sid = request.form.get("CallSid") or ""
    recording_url = request.form.get("RecordingUrl") or ""
    caller_phone = request.form.get("To") or ""

    qs = f"?req_id={req_id}" if req_id else ""
    turn_url = f"{PUBLIC_BASE_URL}/voice/turn{qs}"

    # Immediately return a "thinking" response so Twilio doesn't timeout
    thinking_url = f"{PUBLIC_BASE_URL}/audio/thinking.wav"

    import threading
    def process_in_background():
        try:
            # Pull preselected context
            preselected_slot = None
            preselected_service = None
            if call_sid:
                cr = db.get_call_request_by_sid(call_sid)
                if cr:
                    if cr.get("preselected_slot_id"):
                        preselected_slot = db.get_slot(cr["preselected_slot_id"])
                    preselected_service = cr.get("preselected_service")

            # STT
            user_text = ""
            if recording_url:
                try:
                    audio = download_twilio_recording(recording_url)
                    user_text = sarvam_stt(audio, mime="audio/wav")
                except Exception:
                    log.exception("STT failed")

            log.info(f"[{call_sid}] user={user_text!r}")

            if not user_text.strip():
                reply_text = "Sorry, I didn't catch that. Could you please repeat?"
                action = "continue"
            else:
                db.add_transcript(call_sid, "user", user_text)
                try:
                    reply_text, action = agent_mod.respond(
                        call_sid=call_sid,
                        caller_phone=caller_phone,
                        user_text=user_text,
                        direction="outbound",
                        preselected_slot=preselected_slot,
                        preselected_service=preselected_service,
                    )
                except Exception:
                    log.exception("Agent failed")
                    reply_text = "Sorry, something went wrong. Could you repeat that?"
                    action = "continue"

            db.add_transcript(call_sid, "assistant", reply_text)
            log.info(f"[{call_sid}] agent={reply_text!r} action={action}")

            # TTS
            audio_url = None
            try:
                audio_url = save_tts(sarvam_tts(reply_text))
            except Exception:
                log.exception("TTS failed")

            # Build new TwiML and update the live call via Twilio REST API
            if action == "escalate":
                if RECEPTIONIST_PHONE:
                    new_twiml = twiml_escalate_dial(audio_url, reply_text, RECEPTIONIST_PHONE)
                else:
                    new_twiml = twiml_final_and_hangup(audio_url, reply_text)
            elif action == "hangup":
                new_twiml = twiml_final_and_hangup(audio_url, reply_text)
            else:
                if audio_url:
                    new_twiml = twiml_turn_reply(audio_url, turn_url)
                else:
                    new_twiml = twiml_speak_reply(reply_text, turn_url)

            # Update the live call with new TwiML
            from telephony import twilio_client
            client = twilio_client()
            try:
                call_status = client.calls(call_sid).fetch().status
                if call_status == "in-progress":
                    client.calls(call_sid).update(twiml=new_twiml)
                else:
                    log.warning(f"[{call_sid}] Call already ended ({call_status}), skipping update")
            except Exception:
                log.exception("Call update failed")

        except Exception:
            log.exception("Background processing failed")

    # Fire background thread
    t = threading.Thread(target=process_in_background, daemon=True)
    t.start()

    # Immediately return "thinking" TwiML — Twilio gets response in <1s
    immediate = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{thinking_url}</Play>
  <Pause length="25"/>
</Response>"""
    return Response(immediate, mimetype="application/xml")

@app.post("/voice/status")
def voice_status():
    """Twilio call-lifecycle status updates."""
    req_id = request.args.get("req_id", type=int)
    call_sid = request.form.get("CallSid") or ""
    call_status = request.form.get("CallStatus") or ""
    log.info(f"[{call_sid}] status={call_status}")

    # Map Twilio statuses to our UI stages
    status_map = {
        "queued": "dialing",
        "initiated": "dialing",
        "ringing": "ringing",
        "in-progress": "connected",
        "completed": "ended",
        "busy": "ended",
        "no-answer": "ended",
        "failed": "failed",
        "canceled": "ended",
    }
    ui_status = status_map.get(call_status, call_status)

    if call_sid:
        db.upsert_call(
            call_sid,
            status=ui_status,
            hangup_cause=call_status if call_status in ("completed", "busy", "no-answer", "failed", "canceled") else None,
        )
    if req_id:
        cur = db.get_call_request(req_id)
        if cur and cur.get("status") != "failed":  # don't overwrite explicit failure
            db.update_call_request(req_id, status=ui_status)

    return ("", 204)


# ============================================================================
# Audio
# ============================================================================
@app.get("/audio/<aid>.wav")
def audio(aid: str):
    if not re.fullmatch(r"[0-9a-zA-Z_-]{1,64}", aid or ""):
        abort(404)
    p = AUDIO_DIR / f"{aid}.wav"
    if not p.exists():
        abort(404)
    return send_file(str(p), mimetype="audio/wav")


@app.get("/opening.wav")
def opening():
    ensure_opening_exists()
    if not OPENING_PATH.exists():
        abort(503)
    return send_file(str(OPENING_PATH), mimetype="audio/wav")


# ============================================================================
# Admin dashboard
# ============================================================================
@app.get("/dashboard")
def dashboard():
    only = request.args.get("filter")
    if only not in ("booked", "vacant"):
        only = None
    slots = db.list_slots_with_appointments(only=only)

    # Group by date for the template
    grouped: dict[str, list[dict]] = {}
    for s in slots:
        date_key = s["start_time"][:10]
        s["time_label"] = format_slot_for_display(s["start_time"])
        grouped.setdefault(date_key, []).append(s)

    grouped_list = [
        {"date": k, "date_label": format_date_for_display(f"{k}T12:00:00"), "slots": v}
        for k, v in sorted(grouped.items())
    ]
    calls = db.list_calls(limit=25)
    total_booked = sum(1 for s in slots if s["booked"])
    total_vacant = sum(1 for s in slots if not s["booked"])
    return render_template(
        "dashboard.html",
        grouped=grouped_list,
        calls=calls,
        filter=only,
        total_booked=total_booked,
        total_vacant=total_vacant,
    )


@app.get("/dashboard/<call_sid>")
def dashboard_call(call_sid: str):
    call = db.get_call(call_sid)
    if not call:
        abort(404)
    turns = db.load_transcript(call_sid)
    return render_template("transcript.html", call=call, turns=turns)


@app.get("/api/appointments.csv")
def appointments_csv():
    appts = db.list_appointments(limit=10_000)
    fields = ["id", "slot_start", "patient_name", "phone", "service",
              "urgency", "notes", "status", "created_at", "call_sid"]
    lines = [",".join(fields)]
    for a in appts:
        row = []
        for f in fields:
            v = a.get(f) or ""
            v = str(v).replace('"', '""')
            if "," in v or "\n" in v:
                v = f'"{v}"'
            row.append(v)
        lines.append(",".join(row))
    resp = make_response("\n".join(lines))
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = 'attachment; filename="appointments.csv"'
    return resp


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, opening_audio=OPENING_PATH.exists())


@app.errorhandler(404)
def not_found(e):
    return jsonify(ok=False, error="not_found"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
