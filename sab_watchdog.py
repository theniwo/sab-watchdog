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
MAX_PAUSED_COUNT_FOR_UNPAUSE = int(os.environ.get("MAX_PAUSED_COUNT_FOR_UNPAUSE", 5))

# Abbruch bei fehlender API
if not API_KEY:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ❌ Environment variable SABNZBD_APIKEY is missing.", flush=True)
    sys.exit(1)

# Zähler
zero_speed_hang_counter = 0     # Zähler für hängende Downloads (Aktive Slots, 0 B/s, NICHT Pausiert)
sabnzbd_paused_counter = 0      # Zähler für den Gesamtstatus "Paused"
post_processing_active_counter = 0 # Zähler, wenn Post-Processing läuft während SABnzbd pausiert ist

POST_PROCESSING_STATES = ["Verifying", "Extracting", "Moving", "Renaming", "Repairing", "Grabbing"]

def log_message(message):
    """Prints a message with a timestamp."""
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)

def get_queue_info():
    """Fetches current queue info, download rate, and active slots from SABnzbd API."""
    try:
        url = f"{SABNZBD_URL}/api?mode=queue&output=json&apikey={API_KEY}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        queue = data["queue"]
        speed_bps = float(queue["kbpersec"]) * 1024
        overall_status = queue["status"]
        active_download_slots = int(queue["noofslots"])

        is_post_processing_active = False
        if "slots" in queue:
            for job_slot in queue["slots"]:
                # Check for individual job status indicating post-processing
                if job_slot.get("status") in POST_PROCESSING_STATES:
                    is_post_processing_active = True
                    break

        return speed_bps, active_download_slots, overall_status, is_post_processing_active
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️  Error fetching queue info: {e}")
        return -1, 0, "Error", False
    except (KeyError, ValueError) as e:
        log_message(f"⚠️  Error parsing queue info: {e}. Full response: {data}")
        return -1, 0, "Error", False

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
    speed, active_download_slots, overall_status, is_post_processing_active = get_queue_info()
    log_message(f"⬇️  Speed: {speed:.0f} B/s | Active Downloads (slots): {active_download_slots} | SAB Status: {overall_status} | Post-Processing Active: {is_post_processing_active}")

    # --- Logik für das Entpausieren von SABnzbd ---
    # SABnzbd wird nur entpausiert, wenn es den Gesamtstatus "Paused" hat
    # UND kein Post-Processing aktiv ist.
    if overall_status == "Paused":
        if is_post_processing_active:
            post_processing_active_counter += 1
            log_message(f"⏱️  SABnzbd is paused due to active Post-Processing ({post_processing_active_counter}). Will NOT unpause.")
            sabnzbd_paused_counter = 0 # Reset normal paused counter if PP is running
        else:
            sabnzbd_paused_counter += 1
            log_message(f"⏱️  SABnzbd is in 'Paused' status (no active PP) ({sabnzbd_paused_counter}/{MAX_PAUSED_COUNT_FOR_UNPAUSE})")
            post_processing_active_counter = 0 # Reset PP counter if no PP is running

            if sabnzbd_paused_counter >= MAX_PAUSED_COUNT_FOR_UNPAUSE:
                log_message("💡 Attempting to unpause SABnzbd (paused without active Post-Processing)...")
                if resume_sabnzbd():
                    sabnzbd_paused_counter = 0
    else: # SABnzbd is not in overall "Paused" status
        sabnzbd_paused_counter = 0
        post_processing_active_counter = 0


    # --- Logik für den Neustart bei echten Hängepartien ---
    # Ein "echter Hänger" liegt vor, wenn:
    # 1. Es gibt mindestens einen aktiven Download-Slot (> 0).
    # 2. Die Download-Geschwindigkeit ist 0 B/s.
    # 3. Der Gesamtstatus ist NICHT "Paused" (da dieser Fall von der Entpausierungslogik behandelt wird).
    # Dies fängt sowohl "Downloading" mit 0 B/s als auch "Idle" mit aktiven Slots und 0 B/s ab.
    if active_download_slots > 0 and speed == 0 and overall_status != "Paused": # <-- Wichtige Änderung hier!
        zero_speed_hang_counter += 1
        log_message(f"⏱️  Potential download hang detected (SAB Status: {overall_status}, Speed: {speed:.0f} B/s, Slots: {active_download_slots}) ({zero_speed_hang_counter}/{MAX_ZERO_COUNT})")
    else:
        zero_speed_hang_counter = 0 # Reset if conditions for hanging are not met

    if zero_speed_hang_counter >= MAX_ZERO_COUNT:
        log_message("🚨 Restarting SABnzbd container now due to sustained download hang...")
        os.system(f"docker restart {CONTAINER_NAME}")
        # Reset all relevant counters after a restart
        zero_speed_hang_counter = 0
        sabnzbd_paused_counter = 0
        post_processing_active_counter = 0

    time.sleep(CHECK_INTERVAL)
