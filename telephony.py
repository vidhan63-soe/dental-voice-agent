"""
Twilio telephony wrapper.

Outbound calls use machine_detection="Enable" so the /voice/answered webhook
receives an `AnsweredBy` field. If it's not 'human' we hang up immediately —
this is the "don't pitch a dental appointment to voicemail" behavior.

TwiML builders return the XML strings Twilio expects as webhook responses.
"""
import os
import logging
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

import requests
from twilio.rest import Client as TwilioClient

log = logging.getLogger("telephony")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM_NUMBER", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Trial accounts can only call verified numbers. List them here comma-separated.
SANDBOX_NUMBERS = {n.strip() for n in os.getenv("SANDBOX_NUMBERS", "").split(",") if n.strip()}


def is_whitelisted(phone: str) -> bool:
    """True when the number is verified in the Twilio console (trial constraint)."""
    if not SANDBOX_NUMBERS:
        # Empty list → assume upgraded account, any number OK
        return True
    return phone in SANDBOX_NUMBERS


def twilio_client() -> TwilioClient:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def trigger_outbound_call(to_number: str, call_request_id: int) -> str:
    """Initiate an outbound call. Returns the Twilio CallSid."""
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL is not set")
    if not TWILIO_FROM:
        raise RuntimeError("TWILIO_FROM_NUMBER is not set")

    client = twilio_client()
    qs = f"?req_id={call_request_id}"

    call = client.calls.create(
        to=to_number,
        from_=TWILIO_FROM,
        url=f"{PUBLIC_BASE_URL}/voice/answered{qs}",
        method="POST",
        status_callback=f"{PUBLIC_BASE_URL}/voice/status{qs}",
        status_callback_method="POST",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        # AMD: Twilio passes the result as `AnsweredBy` to the answer webhook.
        # We call `Enable` (sync) so we know before executing TwiML.
        machine_detection="Enable",
        machine_detection_timeout=5,
    )
    log.info(f"Twilio call queued to {to_number} sid={call.sid}")
    return call.sid


# --------------------- TwiML builders ---------------------
def _xml(body: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>\n<Response>{body}</Response>"""


def twiml_hangup() -> str:
    return _xml("<Hangup/>")


def twiml_opening(opening_audio_url: str, turn_url: str) -> str:
    """Play pre-cached opening greeting, then Record the first utterance."""
    return _xml(f"""
  <Play>{xml_escape(opening_audio_url)}</Play>
  <Record action="{xml_escape(turn_url)}" method="POST"
          maxLength="15" timeout="3"
          playBeep="false" trim="trim-silence"
          finishOnKey=""/>
  <Redirect method="POST">{xml_escape(turn_url)}</Redirect>
""")


def twiml_turn_reply(audio_url: str, turn_url: str) -> str:
    """Play the agent's reply, then record the next utterance."""
    return _xml(f"""
  <Play>{xml_escape(audio_url)}</Play>
  <Record action="{xml_escape(turn_url)}" method="POST"
          maxLength="15" timeout="3"
          playBeep="false" trim="trim-silence"
          finishOnKey=""/>
  <Redirect method="POST">{xml_escape(turn_url)}</Redirect>
""")


def twiml_speak_reply(text: str, turn_url: str) -> str:
    """Fallback when TTS is unavailable — use Twilio's built-in <Say>."""
    return _xml(f"""
  <Say voice="Polly.Joanna">{xml_escape(text[:600])}</Say>
  <Record action="{xml_escape(turn_url)}" method="POST"
          maxLength="15" timeout="3"
          playBeep="false" trim="trim-silence"
          finishOnKey=""/>
  <Redirect method="POST">{xml_escape(turn_url)}</Redirect>
""")


def twiml_final_and_hangup(audio_url: Optional[str], text_fallback: str) -> str:
    if audio_url:
        voice = f"<Play>{xml_escape(audio_url)}</Play>"
    else:
        voice = f'<Say voice="Polly.Joanna">{xml_escape(text_fallback[:400])}</Say>'
    return _xml(f"""
  {voice}
  <Pause length="1"/>
  <Hangup/>
""")


def twiml_escalate_dial(audio_url: Optional[str], text_fallback: str, receptionist: str) -> str:
    """Announce handoff then <Dial> the receptionist."""
    if audio_url:
        voice = f"<Play>{xml_escape(audio_url)}</Play>"
    else:
        voice = f'<Say voice="Polly.Joanna">{xml_escape(text_fallback[:400])}</Say>'
    return _xml(f"""
  {voice}
  <Dial timeout="25" callerId="{xml_escape(TWILIO_FROM)}">
    <Number>{xml_escape(receptionist)}</Number>
  </Dial>
  <Hangup/>
""")


def download_twilio_recording(recording_url: str, retries: int = 4,
                              timeout: float = 15.0) -> bytes:
    """
    Twilio recording URLs need basic auth and Twilio has eventual consistency,
    so we retry a few times.
    """
    url = recording_url if recording_url.endswith(".wav") else recording_url + ".wav"
    last = None
    auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    import time
    for i in range(retries):
        try:
            r = requests.get(url, auth=auth, timeout=timeout)
            if r.status_code == 200:
                return r.content
            last = f"{r.status_code}: {r.text[:200]}"
        except Exception as e:
            last = str(e)
        time.sleep(0.6)
    raise RuntimeError(f"Could not fetch Twilio recording: {last}")
