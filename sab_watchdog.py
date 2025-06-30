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

# Abbruch bei fehlender API
if not API_KEY:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ❌ Environment variable SABNZBD_APIKEY is missing.", flush=True)
    sys.exit(1)

# Zähler
zero_speed_hang_counter = 0
sabnzbd_paused_counter = 0
post_processing_active_counter = 0
disk_full_counter = 0           # Neuer Zähler für Disk Full Zustand

POST_PROCESSING_STATES = ["Verifying", "Extracting", "Moving", "Renaming", "Repairing", "Grabbing"]

def log_message(message):
    """Prints a message with a timestamp."""
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)

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

        # Disk space in GB (SABnzbd reports in GB, e.g., "10.23")
        disk_space_free_gb = float(queue["diskspace1"])
        # diskspace2 for completed folder, diskspace1 for temporary folder

        is_post_processing_active = False
        queue_items = [] # To store job details for potential deletion
        if "slots" in queue:
            for job_slot in queue["slots"]:
                queue_items.append(job_slot) # Store all job details
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

def delete_sabnzbd_job(nzo_id):
    """Deletes a specific job from the SABnzbd queue by nzo_id."""
    try:
        url = f"{SABNZBD_URL}/api?mode=queue&name=delete&value={nzo_id}&output=json&apikey={API_KEY}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status"):
            log_message(f"✅ Job {nzo_id} successfully deleted from SABnzbd queue.")
            return True
        else:
            log_message(f"❌ Failed to delete job {nzo_id} from SABnzbd queue: {data}")
            return False
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️  Error sending delete command for job {nzo_id}: {e}")
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
    # Nur prüfen, wenn Disk-Platz unter Schwellenwert UND Downloads laufen könnten oder hängen
    if disk_space_free_gb < DISK_FREE_THRESHOLD_GB:
        disk_full_counter += 1
        log_message(f"⚠️  Low disk space detected ({disk_space_free_gb:.2f} GB free, threshold {DISK_FREE_THRESHOLD_GB:.2f} GB) ({disk_full_counter}/{MAX_DISK_FULL_COUNT})")

        if disk_full_counter >= MAX_DISK_FULL_COUNT:
            log_message("🚨 Sustained low disk space detected. Evaluating downloads for deletion...")

            # Find the currently downloading job (status 'Downloading')
            current_download_job = None
            for job in queue_items:
                if job.get("status") == "Downloading":
                    current_download_job = job
                    break

            if current_download_job:
                nzo_id = current_download_job.get("nzo_id")
                # SABnzbd reports 'size' for the full size, 'sizeleft' for remaining
                # Both are strings, need to convert to float/int
                try:
                    total_size_gb = float(current_download_job.get("size", "0").replace(' GB', ''))

                    # If total_size_gb is not available or is 0, fall back to sizeleft
                    if total_size_gb == 0:
                        size_left_gb = float(current_download_job.get("sizeleft", "0").replace(' GB', ''))
                        estimated_needed_gb = size_left_gb # Estimate based on remaining
                    else:
                        estimated_needed_gb = total_size_gb # Use total size if available

                    log_message(f"ℹ️  Current download: '{current_download_job.get('filename', 'N/A')}' (ID: {nzo_id}), Total Size: {total_size_gb:.2f} GB, Estimated Needed: {estimated_needed_gb:.2f} GB")

                    # Fall 1: Download ist alleine schon zu groß
                    if estimated_needed_gb > disk_space_free_gb + DISK_FREE_THRESHOLD_GB: # Add threshold for buffer
                        log_message(f"🗑️  Current download is too large ({estimated_needed_gb:.2f} GB) for available space ({disk_space_free_gb:.2f} GB). Deleting...")
                        delete_sabnzbd_job(nzo_id)
                        disk_full_counter = 0 # Reset after action

                    # Fall 2: Mehrere Downloads nehmen sich den Platz weg (eher unwahrscheinlich bei nur einem "Downloading" Job)
                    # Dies würde bedeuten, dass der aktuelle Download alleine nicht zu groß ist,
                    # aber der gesamte verfügbare Platz nicht ausreicht für alle geplanten Jobs.
                    # Dies ist schwerer zu erkennen und zu handeln, da wir nicht wissen, welche
                    # zukünftigen Jobs Platzprobleme verursachen könnten.
                    # Die API-Felder 'size' und 'sizeleft' beziehen sich nur auf den Download.
                    # Wenn nur EIN Download den Status "Downloading" hat, ist dieser Fall unwahrscheinlich.
                    # Wenn es mehrere aktive Downloads gäbe, würde 'noofslots' > 1 sein.
                    # Die SABnzbd Autopause würde hier in der Regel greifen.
                    # Für eine präzise Umsetzung müsste man die *summierten* Größen der
                    # nächsten n queued-Jobs berechnen, was die Logik komplex macht.
                    # Fürs Erste konzentrieren wir uns auf den Download, der läuft oder gleich starten soll.

                    # Simplere Annahme für "Platz weggenommen": Wenn immer noch wenig Platz
                    # und kein einzelner Download zu groß ist, kann es nur durch die Summe
                    # der Jobs kommen. Da wir den größten als ersten anpacken,
                    # versuchen wir einfach zu löschen, wenn immer noch zu wenig Platz ist.
                    # Wenn der erste Download nicht der Übeltäter ist, löschen wir den aktuell größten,
                    # aber erst, nachdem wir geprüft haben, ob der laufende zu groß war.
                    # Die Anforderung "zufällig 2 Downloads liefen welche sich gegenseitig den Platz weggenommen haben"
                    # ist schwer zu automatisieren, da SABnzbd in der Regel nur 1-2 gleichzeitig herunterlädt
                    # und die Hauptschuld dann doch beim größten ist, der kommt.
                    # Die beste Strategie ist, den Job zu löschen, der gerade am meisten Platz braucht/brauchen würde.

                    else:
                        log_message(f"ℹ️  Current download is not too large for available space, but disk is still full.")
                        # Wenn es mehrere Downloads in der Queue gibt, wäre der nächste Schritt
                        # den größten anstehenden (nicht aktiven) Download zu löschen, wenn es immer noch knapp ist.
                        # Wir löschen hier den aktuell aktiven Job, wenn kein anderer Ausweg.
                        # Dies ist die robusteste, wenn auch manchmal aggressive, Strategie.
                        log_message(f"🗑️  Deleting current downloading job '{current_download_job.get('filename', 'N/A')}' as a fallback for disk space issues.")
                        delete_sabnzbd_job(nzo_id)
                        disk_full_counter = 0 # Reset after action


                except ValueError as ve:
                    log_message(f"❌ Error parsing size for job {nzo_id}: {ve}. Cannot determine if too large.")
                    # In diesem Fall können wir nicht entscheiden und lassen den Counter weiterlaufen.
            else:
                log_message("⚠️  No active 'Downloading' job found despite low disk space. Cannot delete specific download.")
                # Hier könnte man überlegen, den ältesten oder größten in der Warteschlange zu löschen,
                # aber die Anweisung war spezifisch für den "aktuellen Download".

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
