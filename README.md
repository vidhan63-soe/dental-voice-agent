# Bright Smile Dental — Voice AI Agent

An outbound voice AI call agent for a dental clinic. A visitor submits their phone number on a web form, receives an outbound call, has a real spoken conversation, and gets an appointment booked — all automatically.

**Stack:** Flask · Twilio (telephony) · Sarvam AI (STT + TTS) · Groq/OpenRouter (LLM) · SQLite

---

## How it works

1. Visitor fills the demo form → Flask triggers a Twilio outbound call
2. Caller picks up → Sarvam STT transcribes speech each turn
3. Groq LLM runs the conversation (check availability, book, escalate, etc.)
4. Sarvam TTS synthesises the reply → Twilio plays it back
5. Appointment is saved to SQLite; live transcript streams to the browser

---

## Quick start (local)

**Prerequisites:** Python 3.10+, [ngrok](https://ngrok.com)

```bash
git clone https://github.com/YOUR_USERNAME/dental-voice-agent
cd dental-voice-agent

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in all keys — see "Environment variables" below

# Expose localhost to Twilio
ngrok http 8000
# Copy the https URL into .env as PUBLIC_BASE_URL

# One-time: generate appointment slots and opening audio
python scripts/generate_slots.py
python scripts/generate_opening.py

python app.py
```

Open `http://localhost:8000`, enter your verified Twilio number, and your phone will ring.

---

## Environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | Where to get it |
|---|---|
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | [console.twilio.com](https://console.twilio.com) → Account Info |
| `TWILIO_FROM_NUMBER` | A voice-enabled number you bought in Twilio |
| `SANDBOX_NUMBERS` | Comma-separated verified numbers (trial accounts only) |
| `RECEPTIONIST_PHONE` | Number to forward escalated calls to |
| `SARVAM_API_KEY` | [dashboard.sarvam.ai](https://dashboard.sarvam.ai) |
| `GEMINI_API_KEY` | [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) |
| `OPENROUTER_API_KEY` | [openrouter.ai](https://openrouter.ai) (fallback if no Groq key) |
| `PUBLIC_BASE_URL` | Your ngrok / production HTTPS URL |

> **Trial note:** Twilio trial accounts can only call numbers listed in `SANDBOX_NUMBERS`. Go to Twilio Console → Verified Caller IDs to add numbers.

---

## File layout

```
app.py                  Flask routes + Twilio webhooks
agent.py                LLM conversation loop (Groq / OpenRouter)
prompts.py              System prompts, emergency keyword filter
tools.py                Tool schemas dispatched to the LLM
sarvam.py               Sarvam STT + TTS wrappers
telephony.py            Twilio REST + TwiML builders
db.py                   SQLite schema + slot helpers
passenger_wsgi.py       cPanel/Passenger entry point
scripts/
  generate_slots.py     Pre-generate appointment slots (run once + via cron)
  generate_opening.py   Pre-generate opening greeting audio
  cleanup_audio.py      Delete old synthesised audio files
templates/
  demo.html             Landing page with call request form
  status.html           Live call stage + streaming transcript
  dashboard.html        Admin slot table (filter booked/vacant)
  transcript.html       Per-call full transcript
audio/                  Synthesised WAV files (runtime)
clinic.db               SQLite database (created on first run)
```

---

## Admin pages

| URL | Description |
|---|---|
| `/dashboard` | All slots grouped by day; filter booked / vacant |
| `/dashboard/<call_sid>` | Full transcript for a single call |
| `/api/appointments.csv` | CSV export of all appointments |
| `/healthz` | Health check — `{"ok": true}` |

---

## Deploying to cPanel (MilesWeb / shared hosting)

1. Upload the project to `/home/<USER>/dental_voice_agent/`
2. cPanel → **Setup Python App** → Application root: `dental_voice_agent`, startup file: `passenger_wsgi.py`, entry point: `application`
3. Install deps: `pip install -r requirements.txt`
4. Set all env vars in cPanel → Python App → Environment Variables
5. Run one-time scripts via cPanel Terminal:
   ```bash
   python scripts/generate_slots.py
   python scripts/generate_opening.py
   ```
6. Add cron jobs:
   ```
   0 3 * * *    python scripts/generate_slots.py    # top up slots daily
   0 */6 * * *  python scripts/cleanup_audio.py     # clean old audio
   ```

---

## Key agent capabilities

- Books 1-hour appointment slots from a real SQLite calendar
- Handles rescheduling, clinic FAQs, urgent pain cases
- Escalates to a human receptionist via Twilio `<Dial>` on request or keyword
- Emergency keyword pre-filter (no LLM call) — instant escalation for phrases like "can't breathe", "bleeding won't stop"
- Voicemail detection — hangs up cleanly on answering machines
- Full call transcripts with live browser streaming
