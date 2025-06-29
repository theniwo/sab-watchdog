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
# Wie oft SABnzbd im Pausiert-Status überprüft wird, bevor entpausiert wird,
# wenn keine Post-Processing-Jobs laufen.
MAX_PAUSED_COUNT_FOR_UNPAUSE = int(os.environ.get("MAX_PAUSED_COUNT_FOR_UNPAUSE", 5))

# Abbruch bei fehlender API
if not API_KEY:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ❌ Environment variable SABNZBD_APIKEY is missing.", flush=True)
    sys.exit(1)

# Zähler
zero_speed_hang_counter = 0     # Zähler für hängende Downloads (Status "Downloading", aber 0 B/s)
sabnzbd_paused_counter = 0      # Zähler für den Gesamtstatus "Paused"
post_processing_active_counter = 0 # Zähler, wenn Post-Processing läuft während SABnzbd pausiert ist

# Definition von Post-Processing-Status in SABnzbd (Basierend auf API-Dokumentation)
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
        # SABnzbd API queue status can be 'Paused', 'Downloading', 'Idle'
        overall_status = queue["status"]
        # noofslots counts items with status 'Downloading' (actual downloads)
        # and also 'Queued' items that are next in line. It does NOT count post-processing.
        active_download_slots = int(queue["noofslots"])

        # Check individual items for post-processing states
        is_post_processing_active = False
        # The 'jobs' list contains all queue items, regardless of their main queue status
        if "slots" in queue: # Check if 'slots' key exists in queue response
            for job_slot in queue["slots"]:
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
    # Abrufen der erweiterten Queue-Informationen
    speed, active_download_slots, overall_status, is_post_processing_active = get_queue_info()
    log_message(f"⬇️  Speed: {speed:.0f} B/s | Active Downloads (slots): {active_download_slots} | SAB Status: {overall_status} | Post-Processing Active: {is_post_processing_active}")

    # Logik für das Entpausieren von SABnzbd (nur wenn es explizit pausiert ist UND kein Post-Processing läuft)
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
                    sabnzbd_paused_counter = 0 # Reset after successful unpause
                # If unpause fails, counter is not reset, it will retry after next interval
    else: # SABnzbd is not in overall "Paused" status
        sabnzbd_paused_counter = 0 # Reset paused counter
        post_processing_active_counter = 0 # Reset PP counter


    # Logik für den Neustart bei echten Hängepartien
    # Ein "echter Hänger" liegt nur vor, wenn SABnzbd den Gesamtstatus "Downloading" meldet,
    # aber die Geschwindigkeit 0 B/s beträgt. Dies ignoriert pausierte Einzel-Downloads
    # oder "Idle"-Zustände, die nicht wirklich hängen.
    if overall_status == "Downloading" and speed == 0:
        zero_speed_hang_counter += 1
        log_message(f"⏱️  Download hanging detected (SAB Status: {overall_status}, Speed: {speed:.0f} B/s) ({zero_speed_hang_counter}/{MAX_ZERO_COUNT})")
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
