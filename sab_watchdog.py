#!/usr/bin/env python3
import requests
import time
import os
import sys
from datetime import datetime

# Konfiguration aus Umgebungsvariablen
API_KEY = os.environ.get("SABNZBD_APIKEY")
SABNZBD_URL = os.environ.get("SABNZBD_URL", "http://sabnzbd:8080")
CONTAINER_NAME = os.environ.get("SABNZBD_CONTAINER", "sabnzbd")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 60))          # Sekunden zwischen Checks
MAX_ZERO_COUNT = int(os.environ.get("MAX_ZERO_COUNT", 3))           # Wie oft 0 B/s erlaubt ist, bevor neu gestartet wird
# Zusätzliche Konfiguration für Entpausieren
MAX_PAUSED_COUNT = int(os.environ.get("MAX_PAUSED_COUNT", 5))       # Wie oft SABnzbd im Pausiert-Status überprüft wird, bevor entpausiert wird

# Abbruch bei fehlender API
if not API_KEY:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ❌ Environment variable SABNZBD_APIKEY is missing.", flush=True)
    sys.exit(1)

zero_speed_hang_counter = 0 # Zähler für hängende Downloads (Status "Downloading", aber 0 B/s)
paused_status_counter = 0   # Zähler für den Status "Paused"

def log_message(message):
    """Prints a message with a timestamp."""
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)

def get_download_rate():
    """Fetches current download rate and active slots from SABnzbd API."""
    try:
        url = f"{SABNZBD_URL}/api?mode=queue&output=json&apikey={API_KEY}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        queue = data["queue"]
        speed_bps = float(queue["kbpersec"]) * 1024
        # SABnzbd API queue status can be 'Paused', 'Downloading', 'Idle'
        return speed_bps, int(queue["noofslots"]), queue["status"]
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️  Error fetching queue info: {e}")
        return -1, 0, "Error"
    except (KeyError, ValueError) as e:
        log_message(f"⚠️  Error parsing queue info: {e}. Full response: {data}")
        return -1, 0, "Error"

def resume_sabnzbd():
    """Sends the resume command to SABnzbd API."""
    try:
        url = f"{SABNZBD_URL}/api?mode=resume&output=json&apikey={API_KEY}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status"):
            log_message("✅ SABnzbd successfully resumed via API.")
            return True
        else:
            log_message(f"❌ Failed to resume SABnzbd via API: {data}")
            return False
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️  Error sending resume command: {e}")
        return False

log_message("🚀 SABnzbd Watchdog started")

while True:
    speed, slots, status = get_download_rate()
    log_message(f"⬇️  Speed: {speed:.0f} B/s | Active Jobs: {slots} | Status: {status}")

    # Logic for unpausing SABnzbd if its overall status is "Paused"
    if status == "Paused":
        paused_status_counter += 1
        log_message(f"⏱️  SABnzbd is currently in 'Paused' status ({paused_status_counter}/{MAX_PAUSED_COUNT})")
        if paused_status_counter >= MAX_PAUSED_COUNT:
            log_message("💡 Attempting to unpause SABnzbd...")
            if resume_sabnzbd():
                paused_status_counter = 0 # Reset after successful unpause
            # If unpause fails, counter is not reset, it will retry after next interval
    else:
        paused_status_counter = 0 # Reset if not paused

    # Logic for restarting due to true hanging downloads
    # A "true hang" is detected ONLY if SABnzbd reports "Downloading" status
    # but the actual speed is 0 B/s. This avoids restarts for "Idle" or "Paused" states.
    if status == "Downloading" and speed == 0: # <-- Entscheidende Änderung hier!
        zero_speed_hang_counter += 1
        log_message(f"⏱️  Download hanging detected (Status: {status}, Speed: {speed:.0f} B/s) ({zero_speed_hang_counter}/{MAX_ZERO_COUNT})")
    else:
        zero_speed_hang_counter = 0 # Reset if conditions for hanging are not met

    if zero_speed_hang_counter >= MAX_ZERO_COUNT:
        log_message("🚨 Restarting SABnzbd container now due to sustained download hang...")
        os.system(f"docker restart {CONTAINER_NAME}")
        zero_speed_hang_counter = 0
        paused_status_counter = 0 # Reset all counters after restart

    time.sleep(CHECK_INTERVAL)
