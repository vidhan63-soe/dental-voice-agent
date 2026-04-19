import json, logging, os, requests
from prompts import INBOUND_SYSTEM_PROMPT, OUTBOUND_SYSTEM_PROMPT, check_emergency_keywords, build_preselected_context
import db

log = logging.getLogger("agent")
GROQ_KEY = os.getenv("GROQ_API_KEY", "")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL = os.getenv("GROQ_MODEL", os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b"))

def _load_messages(call_sid):
    raw = db.load_agent_state(call_sid)
    if not raw: return []
    try: return json.loads(raw) if isinstance(raw, str) else raw
    except: return []

def _save_messages(call_sid, messages):
    db.save_agent_state(call_sid, json.dumps(messages))

def respond(call_sid, caller_phone, user_text, direction="outbound", preselected_slot=None, preselected_service=None):
    if check_emergency_keywords(user_text):
        db.insert_escalation(call_sid, reason=f"keyword_prefilter: {user_text[:120]}")
        return ("That sounds serious. Let me connect you with a staff member right now.", "escalate")

    if direction == "outbound":
        system_prompt = OUTBOUND_SYSTEM_PROMPT.format(
            caller_phone=caller_phone or "unknown",
            call_sid=call_sid,
            preselected_context=build_preselected_context(preselected_slot, preselected_service)
        )
    else:
        system_prompt = INBOUND_SYSTEM_PROMPT.format(
            caller_phone=caller_phone or "unknown",
            call_sid=call_sid
        )

    system_prompt += "\n\nEnd every reply with ONE of these tags on a new line:\n[ACTION:CONTINUE]\n[ACTION:ESCALATE]\n[ACTION:HANGUP]\n[ACTION:BOOKED:name|phone|service|time|urgency]\n\nOnly use BOOKED when you have confirmed ALL details. Use CONTINUE for normal conversation."

    messages = _load_messages(call_sid)
    messages.append({"role": "user", "content": user_text})

    all_messages = [{"role": "system", "content": system_prompt}] + messages

    try:
        if GROQ_KEY:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                json={"model": MODEL, "messages": all_messages, "max_tokens": 300},
                timeout=12
            )
        else:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                json={"model": MODEL, "messages": all_messages, "max_tokens": 300},
                timeout=12
            )
        r.raise_for_status()
        reply = r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        log.exception("LLM call failed")
        _save_messages(call_sid, messages)
        return ("Sorry, I had a brief issue. Could you repeat that?", "continue")

    action = "continue"
    spoken = reply

    if "[ACTION:ESCALATE]" in reply:
        action = "escalate"
        db.insert_escalation(call_sid, reason="agent_decision")
        spoken = reply.replace("[ACTION:ESCALATE]", "").strip()
    elif "[ACTION:HANGUP]" in reply:
        action = "hangup"
        spoken = reply.replace("[ACTION:HANGUP]", "").strip()
    elif "[ACTION:BOOKED:" in reply:
        try:
            tag_start = reply.index("[ACTION:BOOKED:")
            tag_end = reply.index("]", tag_start)
            parts = reply[tag_start+15:tag_end].split("|")
            if len(parts) >= 4:
                vacant_dates = db.list_vacant_dates()
                if vacant_dates:
                    slots = db.list_vacant_slots_by_date(vacant_dates[0])
                    if slots:
                        slot = slots[0]
                        db.book_slot(slot["id"])
                        db.insert_appointment(
                            slot_id=slot["id"], call_sid=call_sid,
                            patient_name=parts[0].strip(),
                            phone=parts[1].strip() or caller_phone,
                            service=parts[2].strip(),
                            urgency=parts[4].strip() if len(parts) > 4 else "routine",
                            notes=f"Preferred: {parts[3].strip()}"
                        )
            spoken = reply[:reply.index("[ACTION:BOOKED:")].strip()
        except Exception:
            log.exception("Booking parse failed")
            spoken = reply
        action = "continue"
    elif "[ACTION:CONTINUE]" in reply:
        spoken = reply.replace("[ACTION:CONTINUE]", "").strip()

    messages.append({"role": "assistant", "content": reply})
    _save_messages(call_sid, messages)
    return spoken, action
