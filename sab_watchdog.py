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

# Konfiguration für Disk Full Management
DISK_FREE_THRESHOLD_GB = float(os.environ.get("DISK_FREE_THRESHOLD_GB", 5.0)) # Schwellenwert in GB
# Wie oft Disk-Full-Status überprüft wird, bevor gelöscht wird
MAX_DISK_FULL_COUNT = int(os.environ.get("MAX_DISK_FULL_COUNT", 2))
# Puffer in GB, den der Download mindestens UNTER dem freien Speicherplatz liegen sollte,
# um nicht als "zu groß" zu gelten. Erhöht die Toleranz.
SIZE_CHECK_BUFFER_GB = float(os.environ.get("SIZE_CHECK_BUFFER_GB", 1.0))

# Abbruch bei fehlender API
if not API_KEY:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ❌ Environment variable SABNZBD_APIKEY is missing.", flush=True)
    sys.exit(1)

# Zähler
zero_speed_hang_counter = 0
sabnzbd_paused_counter = 0
post_processing_active_counter = 0
disk_full_counter = 0

POST_PROCESSING_STATES = ["Verifying", "Extracting", "Moving", "Renaming", "Repairing", "Grabbing"]

def log_message(message):
    """Prints a message with a timestamp."""
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)

def parse_sab_size_string(size_str):
    """Parses SABnzbd size string (e.g., "10.23 GB") into float in GB."""
    try:
        # SABnzbd usually reports GB, but handle other units if they appear
        size_str = size_str.strip()
        if size_str.endswith(' GB'):
            return float(size_str.replace(' GB', ''))
        elif size_str.endswith(' MB'):
            return float(size_str.replace(' MB', '')) / 1024
        elif size_str.endswith(' KB'):
            return float(size_str.replace(' KB', '')) / (1024 * 1024)
        return float(size_str) # Fallback if no unit or already a number
    except ValueError:
        return 0.0 # Return 0 if parsing fails

def get_queue_info():
    """
    Fetches current queue info, download rate, active slots, SAB status,
    post-processing status, and disk space from SABnzbd API.
    """
    try:
        url = f"{SABNZBD_URL}/api?mode=queue&output=json&apikey={API_KEY}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        queue = data["queue"]
        speed_bps = float(queue["kbpersec"]) * 1024
        overall_status = queue["status"]
        active_download_slots = int(queue["noofslots"])

        disk_space_free_gb = parse_sab_size_string(queue["diskspace1"]) # Use diskspace1 for temp folder
        # diskspace2 for completed folder

        is_post_processing_active = False
        queue_items = []
        if "slots" in queue:
            for job_slot in queue["slots"]:
                queue_items.append(job_slot)
                if job_slot.get("status") in POST_PROCESSING_STATES:
                    is_post_processing_active = True

        return speed_bps, active_download_slots, overall_status, is_post_processing_active, disk_space_free_gb, queue_items
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️  Error fetching queue info: {e}")
        return -1, 0, "Error", False, 0.0, []
    except (KeyError, ValueError) as e:
        log_message(f"⚠️  Error parsing queue info: {e}. Full response: {data}")
        return -1, 0, "Error", False, 0.0, []

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

def delete_sabnzbd_job(nzo_id, job_name="N/A"):
    """Deletes a specific job from the SABnzbd queue by nzo_id."""
    try:
        url = f"{SABNZBD_URL}/api?mode=queue&name=delete&value={nzo_id}&output=json&apikey={API_KEY}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status"):
            log_message(f"✅ Job '{job_name}' (ID: {nzo_id}) successfully deleted from SABnzbd queue.")
            return True
        else:
            log_message(f"❌ Failed to delete job '{job_name}' (ID: {nzo_id}) from SABnzbd queue: {data}")
            return False
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️  Error sending delete command for job '{job_name}' (ID: {nzo_id}): {e}")
        return False

log_message("🚀 SABnzbd Watchdog started")

while True:
    speed, active_download_slots, overall_status, is_post_processing_active, disk_space_free_gb, queue_items = get_queue_info()
    log_message(f"⬇️  Speed: {speed:.0f} B/s | Active Downloads (slots): {active_download_slots} | SAB Status: {overall_status} | Post-Processing Active: {is_post_processing_active} | Disk Free: {disk_space_free_gb:.2f} GB")

    # --- Logik für das Entpausieren von SABnzbd ---
    if overall_status == "Paused":
        if is_post_processing_active:
            post_processing_active_counter += 1
            log_message(f"⏱️  SABnzbd is paused due to active Post-Processing ({post_processing_active_counter}). Will NOT unpause.")
            sabnzbd_paused_counter = 0
        else:
            sabnzbd_paused_counter += 1
            log_message(f"⏱️  SABnzbd is in 'Paused' status (no active PP) ({sabnzbd_paused_counter}/{MAX_PAUSED_COUNT_FOR_UNPAUSE})")
            post_processing_active_counter = 0

            if sabnzbd_paused_counter >= MAX_PAUSED_COUNT_FOR_UNPAUSE:
                log_message("💡 Attempting to unpause SABnzbd (paused without active Post-Processing)...")
                if resume_sabnzbd():
                    sabnzbd_paused_counter = 0
    else:
        sabnzbd_paused_counter = 0
        post_processing_active_counter = 0

    # --- Logik für Disk Full Management ---
    if disk_space_free_gb < DISK_FREE_THRESHOLD_GB:
        disk_full_counter += 1
        log_message(f"⚠️  Low disk space detected ({disk_space_free_gb:.2f} GB free, threshold {DISK_FREE_THRESHOLD_GB:.2f} GB) ({disk_full_counter}/{MAX_DISK_FULL_COUNT})")

        if disk_full_counter >= MAX_DISK_FULL_COUNT:
            log_message("🚨 Sustained low disk space detected. Evaluating downloads for deletion...")

            job_to_delete = None
            max_potential_size_gb = 0.0

            # Finden des größten Jobs in der gesamten Queue (egal ob Downloading, Queued, Paused)
            # der potenziell das Problem verursacht.
            for job in queue_items:
                # Überspringe bereits abgeschlossene oder in der Nachbearbeitung befindliche Jobs
                if job.get("status") in ["Completed", "Failed"] or job.get("status") in POST_PROCESSING_STATES:
                    continue

                # Für aktuell herunterladende Jobs, nutze 'sizeleft' als Indikator für den sofortigen Platzbedarf
                if job.get("status") == "Downloading":
                    job_current_size_check = parse_sab_size_string(job.get("sizeleft", "0 GB"))
                else:
                    # Für alle anderen Jobs (queued, paused etc.), nutze die Gesamtgröße 'size'
                    job_current_size_check = parse_sab_size_string(job.get("size", "0 GB"))

                if job_current_size_check > max_potential_size_gb:
                    max_potential_size_gb = job_current_size_check
                    job_to_delete = job

            if job_to_delete:
                nzo_id = job_to_delete.get("nzo_id")
                job_name = job_to_delete.get("filename", "N/A")
                estimated_needed_gb = parse_sab_size_string(job_to_delete.get("size", "0 GB")) # Gesamtgröße für Log

                log_message(f"ℹ️  Identified problematic job '{job_name}' (ID: {nzo_id}). Estimated total size: {estimated_needed_gb:.2f} GB.")

                # Löschen, wenn der Job alleine zu groß ist
                if estimated_needed_gb > (disk_space_free_gb + SIZE_CHECK_BUFFER_GB):
                    log_message(f"🗑️  Job '{job_name}' is too large ({estimated_needed_gb:.2f} GB) for available space ({disk_space_free_gb:.2f} GB + {SIZE_CHECK_BUFFER_GB} GB buffer). Deleting...")
                    if delete_sabnzbd_job(nzo_id, job_name):
                        disk_full_counter = 0 # Reset after action
                        sabnzbd_paused_counter = 0 # Reset paused counter as disk issue might be resolved
                        post_processing_active_counter = 0 # Reset PP counter
                else:
                    # Wenn der größte Job alleine nicht zu groß ist, aber Disk immer noch voll,
                    # dann liegt es an der Summe mehrerer Jobs oder einem anderen Problem.
                    # Als Fallback löschen wir den identifizierten größten Job, um Platz zu schaffen.
                    log_message(f"⚠️  Disk full, but largest identified job '{job_name}' ({estimated_needed_gb:.2f} GB) is not solely responsible for full disk. Deleting it as a primary measure to free space.")
                    if delete_sabnzbd_job(nzo_id, job_name):
                        disk_full_counter = 0 # Reset after action
                        sabnzbd_paused_counter = 0 # Reset paused counter as disk issue might be resolved
                        post_processing_active_counter = 0 # Reset PP counter
            else:
                log_message("⚠️  Low disk space detected, but no suitable download job found in queue to delete.")
                # disk_full_counter will continue to increment, potentially leading to no action
                # if there are no deletable jobs. This prevents infinite loops.

    else: # Disk space is above threshold
        disk_full_counter = 0 # Reset counter if disk space is fine

    # --- Logik für den Neustart bei echten Hängepartien ---
    if overall_status == "Downloading" and speed == 0:
        zero_speed_hang_counter += 1
        log_message(f"⏱️  Download hanging detected (SAB Status: {overall_status}, Speed: {speed:.0f} B/s) ({zero_speed_hang_counter}/{MAX_ZERO_COUNT})")
    else:
        zero_speed_hang_counter = 0

    if zero_speed_hang_counter >= MAX_ZERO_COUNT:
        log_message("🚨 Restarting SABnzbd container now due to sustained download hang...")
        os.system(f"docker restart {CONTAINER_NAME}")
        zero_speed_hang_counter = 0
        sabnzbd_paused_counter = 0
        post_processing_active_counter = 0
        disk_full_counter = 0 # Reset all counters after restart

    time.sleep(CHECK_INTERVAL)
