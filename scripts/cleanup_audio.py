"""
Delete audio files older than N hours. Keeps opening.wav.

Add to cron every 6 hours:
    0 */6 * * * cd /home/USER/dental_voice_agent && /home/USER/virtualenv/dental_voice_agent/3.10/bin/python scripts/cleanup_audio.py
"""
import os
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
AUDIO_DIR = Path(os.getenv("AUDIO_DIR", HERE / "audio"))
MAX_AGE_HOURS = float(os.getenv("AUDIO_MAX_AGE_HOURS", "6"))

if not AUDIO_DIR.exists():
    raise SystemExit(0)

cutoff = time.time() - MAX_AGE_HOURS * 3600
removed = 0
for p in AUDIO_DIR.iterdir():
    if not p.is_file():
        continue
    if p.name == "opening.wav":
        continue
    if not re.fullmatch(r"[0-9a-f]{1,64}\.wav", p.name):
        continue
    try:
        if p.stat().st_mtime < cutoff:
            p.unlink()
            removed += 1
    except OSError:
        pass

print(f"Removed {removed} audio files older than {MAX_AGE_HOURS}h")
