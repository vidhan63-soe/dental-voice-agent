"""
Pre-generate the outbound opening TTS.

Run once after deployment:
    python scripts/generate_opening.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass

from sarvam import sarvam_tts
from prompts import OUTBOUND_OPENING_TEXT

AUDIO_DIR = Path(os.getenv("AUDIO_DIR", HERE / "audio"))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
OUT = AUDIO_DIR / "opening.wav"

print(f"Generating opening.wav → {OUT}")
print(f"Text: {OUTBOUND_OPENING_TEXT}")
wav = sarvam_tts(OUTBOUND_OPENING_TEXT)
OUT.write_bytes(wav)
print(f"Done. {len(wav)} bytes.")
