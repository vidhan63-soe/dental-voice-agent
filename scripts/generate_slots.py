"""
Generate/top-up appointment slots.

Run once after deployment:
    python scripts/generate_slots.py

Add to cron daily (cPanel → Cron Jobs):
    0 3 * * * cd /home/USER/dental_voice_agent && /home/USER/virtualenv/dental_voice_agent/3.10/bin/python scripts/generate_slots.py

The script is idempotent — it uses INSERT OR IGNORE, so running it multiple
times is safe. It rolls the 14-day window forward each day as time passes.
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

import db

db.init_db()
added = db.generate_slots()
print(f"Inserted {added} new slots.")
print(f"Clinic TZ: {db.CLINIC_TZ}  hours: {db.OPEN_HOUR:02d}:00 - {db.CLOSE_HOUR:02d}:00  horizon: {db.SLOT_HORIZON_DAYS} days")

# Print a quick summary
from datetime import datetime
vacant_dates = db.list_vacant_dates()
print(f"Dates with vacant slots: {len(vacant_dates)}")
for d in vacant_dates[:5]:
    slots = db.list_vacant_slots_by_date(d)
    print(f"  {d}: {len(slots)} vacant")
if len(vacant_dates) > 5:
    print(f"  ... and {len(vacant_dates) - 5} more")
