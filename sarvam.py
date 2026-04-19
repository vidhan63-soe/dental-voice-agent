"""
Sarvam AI wrappers — Saaras v3 STT and Bulbul v3 TTS.
"""
import os
import io
import base64
import logging

import requests

log = logging.getLogger("sarvam")

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
SARVAM_BASE = "https://api.sarvam.ai"

TTS_SPEAKER = os.getenv("SARVAM_TTS_SPEAKER", "anushka")
TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v2")
STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saaras:v2.5")
STT_LANG = os.getenv("SARVAM_STT_LANG", "en-IN")
TTS_LANG = os.getenv("SARVAM_TTS_LANG", "en-IN")


def _headers() -> dict:
    if not SARVAM_API_KEY:
        raise RuntimeError("SARVAM_API_KEY is not set")
    return {"api-subscription-key": SARVAM_API_KEY}


def sarvam_stt(audio_bytes: bytes, mime: str = "audio/wav") -> str:
    """
    Transcribe short audio (<=30s). Default mime is audio/wav because Twilio
    <Record> returns WAV by default.
    """
    url = f"{SARVAM_BASE}/speech-to-text"
    filename = "audio.wav" if "wav" in mime else "audio.mp3"
    files = {"file": (filename, io.BytesIO(audio_bytes), mime)}
    data = {"model": STT_MODEL, "language_code": STT_LANG}
    r = requests.post(url, headers=_headers(), files=files, data=data, timeout=25)
    if r.status_code >= 400:
        log.error(f"Sarvam STT {r.status_code}: {r.text[:400]}")
        r.raise_for_status()
    return (r.json().get("transcript") or "").strip()


def sarvam_tts(text: str, sample_rate: int = 8000) -> bytes:
    """Synthesize text → WAV bytes. 8kHz for telephony."""
    url = f"{SARVAM_BASE}/text-to-speech"
    body = {
        "text": text[:1500],
        "target_language_code": TTS_LANG,
        "speaker": TTS_SPEAKER,
        "model": TTS_MODEL,
        "pitch": 0,
        "pace": 1.0,
        "loudness": 1.0,
        "speech_sample_rate": sample_rate,
        "enable_preprocessing": True,
    }
    r = requests.post(
        url,
        headers={**_headers(), "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    if r.status_code >= 400:
        log.error(f"Sarvam TTS {r.status_code}: {r.text[:400]}")
        r.raise_for_status()
    audios = r.json().get("audios") or []
    if not audios:
        raise RuntimeError("Sarvam TTS returned no audio")
    return base64.b64decode(audios[0])
