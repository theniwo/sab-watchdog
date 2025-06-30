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
# Wie oft ein Neustart versucht wird, wenn Löschen bei Disk Full nicht geholfen hat
RESTART_ON_DISK_FULL_FAIL_COUNT = int(os.environ.get("RESTART_ON_DISK_FULL_FAIL_COUNT", 1))

# Abbruch bei fehlender API
if not API_KEY:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ❌ Environment variable SABNZBD_APIKEY is missing.", flush=True)
    sys.exit(1)

# Zähler
zero_speed_hang_counter = 0
sabnzbd_paused_counter = 0
post_processing_active_counter = 0
disk_full_counter = 0
disk_full_restart_counter = 0

# --- ANGEPASST: Post-Processing-Zustände mit beiden Varianten (mit und ohne Doppelpunkt) ---
POST_PROCESSING_STATES = [
    "Verifying", "Verifying:",
    "Extracting", "Extracting:",
    "Moving", "Moving:",
    "Renaming", "Renaming:",
    "Repairing", "Repairing:",
    "Grabbing", "Grabbing:",
    "Copying", "Copying:",
    "Direct Unpack", "Direct Unpack:"
]


def log_message(message):
    """Prints a message with a timestamp."""
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)

def parse_sab_size_string(size_str):
    """Parses SABnzbd size string (e.g., "10.23 GB") into float in GB."""
    try:
        size_str = size_str.strip()
        if size_str.endswith(' GB'):
            return float(size_str.replace(' GB', ''))
        elif size_str.endswith(' MB'):
            return float(size_str.replace(' MB', '')) / 1024
        elif size_str.endswith(' KB'):
            return float(size_str.replace(' KB', '')) / (1024 * 1024)
        return float(size_str)
    except ValueError:
        return 0.0

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

        disk_space_free_gb = parse_sab_size_string(queue["diskspace1"])

        is_post_processing_active = False
        queue_items = []
        if "slots" in queue:
            for job_slot in queue["slots"]:
                queue_items.append(job_slot)
                # Überprüfe den Status jedes Jobs gegen die erweiterte PP-Zustandsliste
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
            log_message(f"✅ Job '{job_name}' (ID: {nzo_id}) successfully deleted from SABnzbd queue via API.")
            return True
        else:
            log_message(f"❌ Failed to delete job '{job_name}' (ID: {nzo_id}) from SABnzbd queue via API: {data}")
            return False
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️  Error sending delete command for job '{job_name}' (ID: {nzo_id}): {e}")
        return False

def reset_sabnzbd_queue():
    """Sends the reset command to SABnzbd API to fix potential queue inconsistencies."""
    try:
        url = f"{SABNZBD_URL}/api?mode=queue&name=reset&output=json&apikey={API_KEY}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status"):
            log_message("✅ SABnzbd queue reset/repair command sent via API.")
            return True
        else:
            log_message(f"❌ Failed to send SABnzbd queue reset/repair command via API: {data}")
            return False
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️  Error sending queue reset command: {e}")
        return False


log_message("🚀 SABnzbd Watchdog started")

while True:
    speed, active_download_slots, overall_status, is_post_processing_active, disk_space_free_gb, queue_items = get_queue_info()
    log_message(f"⬇️  Speed: {speed:.0f} B/s | Active Downloads (slots): {active_download_slots} | SAB Status: {overall_status} | Post-Processing Active: {is_post_processing_active} | Disk Free: {disk_space_free_gb:.2f} GB")

    # --- Logik für das Entpausieren von SABnzbd ---
    # Diese Logik wird NICHT aktiv, wenn is_post_processing_active TRUE ist
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

            for job in queue_items:
                if job.get("status") in ["Completed", "Failed"] or job.get("status") in POST_PROCESSING_STATES:
                    continue

                if job.get("status") == "Downloading":
                    job_current_size_check = parse_sab_size_string(job.get("sizeleft", "0 GB"))
                else:
                    job_current_size_check = parse_sab_size_string(job.get("size", "0 GB"))

                if job_current_size_check > max_potential_size_gb:
                    max_potential_size_gb = job_current_size_check
                    job_to_delete = job

            if job_to_delete:
                nzo_id = job_to_delete.get("nzo_id")
                job_name = job_to_delete.get("filename", "N/A")
                estimated_needed_gb = parse_sab_size_string(job_to_delete.get("size", "0 GB"))

                log_message(f"ℹ️  Identified problematic job '{job_name}' (ID: {nzo_id}). Estimated total size: {estimated_needed_gb:.2f} GB.")

                deletion_successful = False
                if estimated_needed_gb > (disk_space_free_gb + SIZE_CHECK_BUFFER_GB):
                    log_message(f"🗑️  Job '{job_name}' is too large ({estimated_needed_gb:.2f} GB) for available space ({disk_space_free_gb:.2f} GB + {SIZE_CHECK_BUFFER_GB} GB buffer). Deleting...")
                    deletion_successful = delete_sabnzbd_job(nzo_id, job_name)
                else:
                    log_message(f"⚠️  Disk full, but largest identified job '{job_name}' ({estimated_needed_gb:.2f} GB) is not solely responsible for full disk. Deleting it as a primary measure to free space.")
                    deletion_successful = delete_sabnzbd_job(nzo_id, job_name)

                if deletion_successful:
                    time.sleep(5)

                    _, _, _, _, current_disk_free_gb, _ = get_queue_info()
                    log_message(f"🔄 Re-checking disk space after deletion attempt: {current_disk_free_gb:.2f} GB free.")

                    if current_disk_free_gb < DISK_FREE_THRESHOLD_GB:
                        disk_full_restart_counter += 1
                        log_message(f"❌ Disk space still critically low ({current_disk_free_gb:.2f} GB) after deleting job. File data likely not removed. ({disk_full_restart_counter}/{RESTART_ON_DISK_FULL_FAIL_COUNT})")

                        if disk_full_restart_counter >= RESTART_ON_DISK_FULL_FAIL_COUNT:
                            log_message("Attempting to reset SABnzbd queue to clear potential inconsistencies before restart...")
                            reset_sabnzbd_queue()
                            time.sleep(5)

                            log_message("🚨 Sustained low disk space after deletion and queue reset, restarting SABnzbd container to force cleanup and reset.")
                            os.system(f"docker restart {CONTAINER_NAME}")
                            zero_speed_hang_counter = 0
                            sabnzbd_paused_counter = 0
                            post_processing_active_counter = 0
                            disk_full_counter = 0
                            disk_full_restart_counter = 0
                    else:
                        log_message("✅ Disk space successfully increased after deletion. Problem resolved.")
                        disk_full_counter = 0
                        sabnzbd_paused_counter = 0
                        post_processing_active_counter = 0
                        disk_full_restart_counter = 0

            else:
                log_message("⚠️  Low disk space detected, but no suitable download job found in queue to delete.")
                disk_full_restart_counter = 0

    else:
        disk_full_counter = 0
        disk_full_restart_counter = 0


    # --- Logik für den Neustart bei echten Hängepartien ---
    # Diese Logik wird NICHT aktiv, wenn Post-Processing aktiv ist
    if overall_status == "Downloading" and speed == 0 and not is_post_processing_active:
        zero_speed_hang_counter += 1
        log_message(f"⏱️  Download hanging detected (SAB Status: {overall_status}, Speed: {speed:.0f} B/s, No PP Active) ({zero_speed_hang_counter}/{MAX_ZERO_COUNT})")
    else:
        zero_speed_hang_counter = 0

    if zero_speed_hang_counter >= MAX_ZERO_COUNT:
        log_message("🚨 Restarting SABnzbd container now due to sustained download hang...")
        os.system(f"docker restart {CONTAINER_NAME}")
        zero_speed_hang_counter = 0
        sabnzbd_paused_counter = 0
        post_processing_active_counter = 0
        disk_full_counter = 0
        disk_full_restart_counter = 0

    time.sleep(CHECK_INTERVAL)
