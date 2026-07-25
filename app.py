import os
import sys
import glob
import time
import json
import shutil
import subprocess
import threading
import urllib.request
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# --- Portable Auto-Detection Helpers ---
def find_makemkv():
    candidates = [
        r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
        r"C:\Program Files\MakeMKV\makemkvcon64.exe",
        r"C:\Program Files (x86)\MakeMKV\makemkvcon.exe",
        r"C:\Program Files\MakeMKV\makemkvcon.exe"
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]

def find_handbrake():
    winget_pattern = r"C:\Users\*\AppData\Local\Microsoft\WinGet\Packages\HandBrake.HandBrake.CLI*\HandBrakeCLI.exe"
    matches = glob.glob(winget_pattern)
    if matches:
        return matches[0]
    candidates = [
        r"C:\Program Files\HandBrake\HandBrakeCLI.exe",
        r"C:\Program Files (x86)\HandBrake\HandBrakeCLI.exe"
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return r"C:\Program Files\HandBrake\HandBrakeCLI.exe"

def find_optical_drive():
    try:
        cmd = "Get-Volume | Where-Object { $_.DriveType -eq 'CD-ROM' } | Select-Object -ExpandProperty DriveLetter"
        out = subprocess.check_output(["powershell", "-Command", cmd], text=True).strip()
        if out:
            return out.split('\n')[0].strip() + ":"
    except Exception:
        pass
    return "D:"

# --- Constants & Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")

def load_config():
    default_config = {
        "makemkv_path": find_makemkv(),
        "handbrake_path": find_handbrake(),
        "drive_letter": find_optical_drive(),
        "temp_raw_dir": r"C:\AutoRipTemp\Raw",
        "temp_encoded_dir": r"C:\AutoRipTemp\Encoded",
        "tv_destination": r"C:\Media\TV Shows",
        "movie_destination": r"C:\Media\Movies"
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                default_config.update(cfg)
        except Exception:
            pass
    return default_config

config = load_config()

# --- Global State ---
state = {
    "stage": "IDLE",
    "status_message": "System Ready - Insert a disc to begin",
    "progress_pct": 0,
    "current_file": "",
    "current_title": 0,
    "total_titles": 0,
    "drive_letter": config["drive_letter"],
    "disc_label": "No Disc Detected",
    "disc_present": False,
    "disc_type": "DVD",
    "fps": "0",
    "eta": "--:--",
    "start_timestamp": 0,
    "auto_start_countdown": 0,
    "auto_start_enabled": True,
    "artwork_url": "",
    "media_summary": "",
    "nas_storage": {"free_gb": 0, "total_gb": 0, "used_pct": 0},
    "logs": [f"{time.strftime('[%H:%M:%S]')} System Ready - AutoRip Control Center Online"],
    "settings": {
        "media_type": "tv",
        "format": "mp4",
        "preset": "Auto",
        "min_length_sec": 600,
        "auto_eject": True,
        "auto_rename": True,
        "include_episode_titles": True,
        "show_name": "TaleSpin",
        "movie_title": "Movie Title",
        "release_year": "1990",
        "season_number": 1,
        "start_episode": 1,
        "tv_destination": config["tv_destination"],
        "movie_destination": config["movie_destination"],
        "discord_webhook_url": "",
        "plex_url": "",
        "plex_token": ""
    }
}

process_lock = threading.Lock()
countdown_cancel_event = threading.Event()

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_history_entry(entry):
    history = load_history()
    history.insert(0, entry)
    history = history[:50]
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        add_log(f"Error saving history: {e}")

def add_log(msg):
    timestamp = time.strftime("[%H:%M:%S]")
    entry = f"{timestamp} {msg}"
    print(entry, flush=True)
    state["logs"].append(entry)
    if len(state["logs"]) > 300:
        state["logs"].pop(0)

def fetch_media_artwork():
    try:
        media_type = state["settings"]["media_type"]
        if media_type == "tv":
            query = state["settings"]["show_name"]
            url = f"https://api.tvmaze.com/singlesearch/shows?q={urllib.parse.quote(query)}"
            req = urllib.request.urlopen(url, timeout=3)
            data = json.loads(req.read().decode())
            if data and "image" in data and data["image"] and "original" in data["image"]:
                state["artwork_url"] = data["image"]["original"]
            if data and "summary" in data and data["summary"]:
                summary = data["summary"].replace("<p>", "").replace("</p>", "").replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
                state["media_summary"] = summary
        else:
            query = state["settings"]["movie_title"]
            url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=movie&limit=1"
            req = urllib.request.urlopen(url, timeout=3)
            data = json.loads(req.read().decode())
            if data and "results" in data and len(data["results"]) > 0:
                item = data["results"][0]
                if "artworkUrl100" in item:
                    state["artwork_url"] = item["artworkUrl100"].replace("100x100bb", "600x600bb")
                if "longDescription" in item:
                    state["media_summary"] = item["longDescription"]
    except Exception:
        pass

def fetch_episode_title(show_name, season_num, ep_num):
    try:
        url = f"https://api.tvmaze.com/singlesearch/shows?q={urllib.parse.quote(show_name)}&embed=episodes"
        req = urllib.request.urlopen(url, timeout=4)
        data = json.loads(req.read().decode())
        episodes = data.get("_embedded", {}).get("episodes", [])
        for ep in episodes:
            if ep.get("season") == int(season_num) and ep.get("number") == int(ep_num):
                name = ep.get("name", "")
                for char in r'/\:*?"<>|':
                    name = name.replace(char, "")
                return name.strip()
    except Exception:
        pass
    return ""

def send_discord_notification(title, description, poster_url=None, fields=None):
    webhook_url = state["settings"].get("discord_webhook_url", "")
    if not webhook_url:
        return
    try:
        embed = {
            "title": title,
            "description": description,
            "color": 65474,
            "fields": fields or [],
            "footer": {"text": "AutoRip Control Center • Portable Edition"}
        }
        if poster_url:
            embed["thumbnail"] = {"url": poster_url}
            
        payload = json.dumps({"embeds": [embed]}).encode('utf-8')
        req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json", "User-Agent": "AutoRipControlCenter"})
        urllib.request.urlopen(req, timeout=5)
        add_log("[Discord] Sent Discord webhook notification embed successfully!")
    except Exception as e:
        add_log(f"[Discord] Webhook notification failed: {e}")

def check_nas_storage():
    try:
        target_dir = state["settings"].get("tv_destination") or config["tv_destination"]
        drive_root = os.path.pathsplitdrive(target_dir)[0] + "\\"
        if os.path.exists(drive_root):
            usage = shutil.disk_usage(drive_root)
            free_gb = round(usage.free / (1024 ** 3), 1)
            total_gb = round(usage.total / (1024 ** 3), 1)
            used_pct = round(((usage.total - usage.free) / usage.total) * 100, 1)
            state["nas_storage"] = {
                "free_gb": free_gb,
                "total_gb": total_gb,
                "used_pct": used_pct
            }
    except Exception:
        state["nas_storage"] = {"free_gb": 0, "total_gb": 0, "used_pct": 0}

def check_drive_status():
    drive = state["drive_letter"][0]
    previous_disc_present = state["disc_present"]
    try:
        cmd = f"(Get-Volume -DriveLetter '{drive}' -ErrorAction SilentlyContinue).FileSystemLabel"
        output = subprocess.check_output(["powershell", "-Command", cmd], text=True).strip()
        if output:
            state["disc_label"] = output
            state["disc_present"] = True
            
            if "BD" in output.upper() or "BLURAY" in output.upper():
                state["disc_type"] = "Blu-ray"
            else:
                state["disc_type"] = "DVD"
        else:
            state["disc_label"] = "Empty Drive / No Disc"
            state["disc_present"] = False
            state["disc_type"] = "Unknown"
    except Exception:
        state["disc_label"] = "Drive Error / Empty"
        state["disc_present"] = False
        state["disc_type"] = "Unknown"

    if not previous_disc_present and state["disc_present"] and state["stage"] == "IDLE" and state["auto_start_enabled"]:
        trigger_auto_start_countdown()

def trigger_auto_start_countdown():
    if state["stage"] != "IDLE":
        return
    
    countdown_cancel_event.clear()
    state["stage"] = "COUNTDOWN"
    state["auto_start_countdown"] = 10
    
    def countdown_thread():
        for i in range(10, 0, -1):
            if countdown_cancel_event.is_set():
                state["stage"] = "IDLE"
                state["status_message"] = "Auto-Start Cancelled"
                state["auto_start_countdown"] = 0
                return
            state["auto_start_countdown"] = i
            state["status_message"] = f"Disc Detected! Starting rip automatically in {i} seconds..."
            time.sleep(1)
            
        if not countdown_cancel_event.is_set() and state["stage"] == "COUNTDOWN":
            state["auto_start_countdown"] = 0
            run_autorip_pipeline_async()

    threading.Thread(target=countdown_thread, daemon=True).start()

def run_autorip_pipeline_async():
    thread = threading.Thread(target=run_autorip_pipeline, daemon=True)
    thread.start()

def run_autorip_pipeline():
    if not process_lock.acquire(blocking=False):
        add_log("Pipeline already running!")
        return

    start_time = time.time()
    state["start_timestamp"] = int(start_time)
    temp_raw = config["temp_raw_dir"]
    temp_encoded = config["temp_encoded_dir"]
    
    try:
        os.makedirs(temp_raw, exist_ok=True)
        os.makedirs(temp_encoded, exist_ok=True)

        state["logs"] = [f"{time.strftime('[%H:%M:%S]')} === Starting AutoRip Pipeline ==="]

        check_drive_status()
        if not state["disc_present"]:
            add_log(f"No disc detected in drive {state['drive_letter']}. Aborting.")
            state["stage"] = "ERROR"
            state["status_message"] = "No disc in drive"
            return

        label = state["disc_label"]
        fmt = state["settings"]["format"]
        media_type = state["settings"]["media_type"]
        
        if media_type == "movie":
            min_sec = max(state["settings"]["min_length_sec"], 1800)
        else:
            min_sec = state["settings"]["min_length_sec"]

        preset = state["settings"]["preset"]
        add_log(f"Media Type: {media_type.upper()} | Disc Type: {state['disc_type']} | Preset/Encoder: {preset}")

        fetch_media_artwork()

        # --- STEP 1: RIPPING ---
        state["stage"] = "RIPPING"
        state["status_message"] = f"Ripping raw tracks with MakeMKV ({label})..."
        state["progress_pct"] = 10
        add_log(f"=== STAGE 1/3: MAKEMKV DISC EXTRACTION STARTED ({label}) ===")

        for f in os.listdir(temp_raw):
            fp = os.path.join(temp_raw, f)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass

        cmd_rip = [
            config["makemkv_path"],
            "-r", "mkv", "disc:0", "all", temp_raw,
            f"--minlength={min_sec}"
        ]

        p_rip = subprocess.Popen(cmd_rip, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p_rip.stdout:
            line_str = line.strip()
            if "MSG:3028" in line_str:
                parts = line_str.split(",")
                if len(parts) >= 6:
                    title_num = parts[5].replace('"', '')
                    duration = parts[4].replace('"', '') if len(parts) > 4 else "unknown"
                    add_log(f"[MakeMKV] Title #{title_num} found (Duration: {duration})")
            elif "MSG:5014" in line_str:
                parts = line_str.split(",")
                if len(parts) >= 5:
                    count = parts[4].replace('"', '')
                    add_log(f"[MakeMKV] Saving {count} titles to temp folder...")
            elif "MSG:5005" in line_str or "MSG:5036" in line_str:
                add_log("[MakeMKV] Disc track extraction completed successfully.")
            
            raw_files = glob.glob(os.path.join(temp_raw, "*.mkv"))
            if raw_files:
                completed_count = len(raw_files)
                state["progress_pct"] = min(10 + int((completed_count / 10.0) * 40), 48)
                state["status_message"] = f"Ripping track {completed_count} with MakeMKV..."

        p_rip.wait()
        if p_rip.returncode != 0:
            add_log(f"[MakeMKV] Extraction failed with exit code {p_rip.returncode}")
            state["stage"] = "ERROR"
            state["status_message"] = "MakeMKV Rip Failed"
            return

        raw_files = sorted(glob.glob(os.path.join(temp_raw, "*.mkv")))
        if not raw_files:
            add_log("[MakeMKV] No titles met the minimum length criteria.")
            state["stage"] = "COMPLETE"
            state["status_message"] = "Finished - No titles found"
            state["progress_pct"] = 100
            return

        if media_type == "tv" and state["settings"]["auto_rename"] and len(raw_files) > 1:
            add_log("Auto-Rename: Filtered out play-all compilation title track.")
            target_files = raw_files[1:]
        else:
            target_files = raw_files

        state["total_titles"] = len(target_files)
        add_log(f"=== STAGE 1 COMPLETE: Extracted {len(target_files)} title(s) to disk ===")

        # --- STEP 2: ENCODING ---
        state["stage"] = "ENCODING"
        add_log(f"=== STAGE 2/3: HANDBRAKE ENCODING STARTED ({len(target_files)} files) ===")
        encoded_files = []

        show_name = state["settings"]["show_name"]
        movie_title = state["settings"]["movie_title"]
        year = state["settings"]["release_year"]
        season = state["settings"]["season_number"]
        start_ep = state["settings"]["start_episode"]
        include_titles = state["settings"].get("include_episode_titles", True)

        for idx, raw_file in enumerate(target_files, start=1):
            state["current_title"] = idx
            
            if state["settings"]["auto_rename"]:
                if media_type == "movie":
                    if len(target_files) == 1:
                        target_filename = f"{movie_title} ({year}).{fmt}"
                    else:
                        target_filename = f"{movie_title} ({year}) - Part {idx:02d}.{fmt}"
                else:
                    ep_num = start_ep + idx - 1
                    ep_name = fetch_episode_title(show_name, season, ep_num) if include_titles else ""
                    if ep_name:
                        target_filename = f"{show_name} - S{season:02d}E{ep_num:02d} - {ep_name}.{fmt}"
                    else:
                        target_filename = f"{show_name} - S{season:02d}E{ep_num:02d}.{fmt}"
            else:
                target_filename = f"{label}_Title_{idx:02d}.{fmt}"

            state["current_file"] = target_filename
            state["status_message"] = f"Encoding Title {idx} of {len(target_files)} ({target_filename})..."
            
            out_file = os.path.join(temp_encoded, target_filename)
            add_log(f"[HandBrake] [{idx}/{len(target_files)}] Starting encode -> '{target_filename}'")

            cmd_enc = [config["handbrake_path"], "-i", raw_file, "-o", out_file]
            
            if "NVENC H.264" in preset:
                cmd_enc.extend(["--encoder", "nvenc_h264", "--quality", "20", "--encoder-preset", "fast"])
            elif "NVENC H.265" in preset:
                cmd_enc.extend(["--encoder", "nvenc_h265", "--quality", "22", "--encoder-preset", "fast"])
            elif "Intel QSV" in preset:
                cmd_enc.extend(["--encoder", "qsv_h264", "--quality", "20"])
            elif "AMD VCE" in preset:
                cmd_enc.extend(["--encoder", "vce_h264", "--quality", "20"])
            else:
                preset_arg = preset if preset != "Auto" else ("HQ 1080p30 Surround" if state["disc_type"] == "Blu-ray" else "HQ 720p30 Surround")
                cmd_enc.extend(["--preset", preset_arg])

            p_enc = subprocess.Popen(cmd_enc, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            last_logged_quarter = -1
            
            for line in p_enc.stdout:
                if "Encoding: task" in line:
                    try:
                        if "%" in line:
                            sub_pct = float(line.split("%")[0].split(",")[-1].strip())
                            overall = 50 + int((idx - 1 + (sub_pct / 100.0)) / len(target_files) * 40)
                            state["progress_pct"] = min(overall, 92)
                            
                            quarter = int(sub_pct // 25)
                            if quarter > last_logged_quarter and quarter > 0 and quarter <= 4:
                                last_logged_quarter = quarter
                                add_log(f"[HandBrake] [{target_filename}] Progress: {int(sub_pct)}% | Speed: {state['fps']} FPS | ETA: {state['eta']}")

                        if "fps" in line:
                            parts = line.split(",")
                            for p in parts:
                                if "fps" in p:
                                    state["fps"] = p.strip().split()[0]
                                if "ETA" in p:
                                    state["eta"] = p.strip().replace("ETA ", "")
                    except Exception:
                        pass

            p_enc.wait()
            if p_enc.returncode == 0 and os.path.exists(out_file):
                size_mb = round(os.path.getsize(out_file) / (1024 * 1024), 1)
                encoded_files.append(out_file)
                add_log(f"[HandBrake] Successfully encoded '{target_filename}' ({size_mb} MB)")
            else:
                add_log(f"[HandBrake] Encoding failed for title {idx}.")

        # --- STEP 3: TRANSFERRING TO DESTINATION ---
        state["stage"] = "TRANSFERRING"
        add_log(f"=== STAGE 3/3: TRANSFERRING {len(encoded_files)} FILE(S) TO DESTINATION ===")
        state["status_message"] = f"Moving {len(encoded_files)} file(s)..."
        state["progress_pct"] = 95

        dest_base_tv = state["settings"].get("tv_destination") or config["tv_destination"]
        dest_base_movie = state["settings"].get("movie_destination") or config["movie_destination"]

        if state["settings"]["auto_rename"]:
            if media_type == "movie":
                dest_folder = os.path.join(dest_base_movie, f"{movie_title} ({year})")
            else:
                dest_folder = os.path.join(dest_base_tv, f"{show_name} ({year})", f"Season {season:02d}")
        else:
            dest_folder = os.path.join(dest_base_tv, "Uncategorized")
            
        os.makedirs(dest_folder, exist_ok=True)
        add_log(f"[Destination] Directory: {dest_folder}")

        moved_names = []
        for ef in encoded_files:
            dest = os.path.join(dest_folder, os.path.basename(ef))
            size_mb = round(os.path.getsize(ef) / (1024 * 1024), 1)
            shutil.move(ef, dest)
            moved_names.append(os.path.basename(ef))
            add_log(f"[Transfer] Moved '{os.path.basename(ef)}' ({size_mb} MB) -> {dest}")

        if media_type == "tv" and state["settings"]["auto_rename"]:
            state["settings"]["start_episode"] = start_ep + len(encoded_files)
            add_log(f"Auto-incremented TV episode counter to S{season:02d}E{state['settings']['start_episode']:02d} for next disc!")

        # --- STEP 4: COMPLETE & EJECT ---
        state["stage"] = "COMPLETE"
        state["status_message"] = f"Finished! {len(encoded_files)} file(s) saved."
        state["progress_pct"] = 100
        state["fps"] = "0"
        state["eta"] = "--:--"
        state["start_timestamp"] = 0

        duration_min = round((time.time() - start_time) / 60, 1)
        save_history_entry({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "disc_label": label,
            "media_type": media_type.upper(),
            "disc_type": state["disc_type"],
            "episodes_saved": len(encoded_files),
            "destination": dest_folder,
            "duration_min": duration_min
        })

        send_discord_notification(
            title=f"🎉 Disc Processing Complete: {label}",
            description=f"Successfully ripped and encoded **{len(encoded_files)} episode(s)** in **{duration_min} minutes**!",
            poster_url=state["artwork_url"],
            fields=[
                {"name": "Media Target", "value": f"{show_name if media_type=='tv' else movie_title} ({year})", "inline": True},
                {"name": "Disc Type", "value": state["disc_type"], "inline": True},
                {"name": "Destination", "value": f"`{dest_folder}`", "inline": False},
                {"name": "Episodes Saved", "value": "\n".join([f"• `{n}`" for n in moved_names[:5]]) + (f"\n*...and {len(moved_names)-5} more*" if len(moved_names)>5 else ""), "inline": False}
            ]
        )

        if state["settings"]["auto_eject"]:
            add_log(f"Ejecting drive {state['drive_letter']}...")
            subprocess.run(["powershell", "-Command", "(New-Object -ComObject WMPlayer.OCX.7).cdromCollection.Item(0).Eject()"], capture_output=True)
            state["disc_present"] = False
            state["disc_label"] = "Ejected"

    except Exception as e:
        add_log(f"Fatal error in pipeline: {e}")
        state["stage"] = "ERROR"
        state["status_message"] = f"Error: {e}"
        state["start_timestamp"] = 0
    finally:
        process_lock.release()

# --- Background Poller ---
def drive_poller_thread():
    while True:
        time.sleep(3)
        check_nas_storage()
        if state["stage"] in ["IDLE", "COUNTDOWN"]:
            check_drive_status()

threading.Thread(target=drive_poller_thread, daemon=True).start()

# --- Routes ---
@app.route("/")
def index():
    return render_template("index.html")

@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route("/api/status")
def get_status():
    dest_files = []
    media_type = state["settings"]["media_type"]
    show_name = state["settings"]["show_name"]
    movie_title = state["settings"]["movie_title"]
    year = state["settings"]["release_year"]
    season = state["settings"]["season_number"]

    dest_base_tv = state["settings"].get("tv_destination") or config["tv_destination"]
    dest_base_movie = state["settings"].get("movie_destination") or config["movie_destination"]

    if media_type == "movie":
        target_dir = os.path.join(dest_base_movie, f"{movie_title} ({year})")
    else:
        target_dir = os.path.join(dest_base_tv, f"{show_name} ({year})", f"Season {season:02d}")

    if os.path.exists(target_dir):
        files_with_time = []
        for f in os.listdir(target_dir):
            fp = os.path.join(target_dir, f)
            if os.path.isfile(fp):
                files_with_time.append((f, os.path.getmtime(fp), round(os.path.getsize(fp) / (1024 * 1024), 1)))
        
        files_with_time.sort(key=lambda x: x[1], reverse=True)
        for f, mtime, size_mb in files_with_time[:20]:
            dest_files.append({"name": f, "size_mb": size_mb})
                
    state_copy = dict(state)
    state_copy["logs"] = state["logs"][-50:] if state["logs"] else ["System ready - waiting for process logs..."]

    return jsonify({
        "state": state_copy,
        "nas_files": dest_files,
        "history": load_history()
    })

@app.route("/api/start", methods=["POST"])
def start_pipeline():
    if state["stage"] in ["RIPPING", "ENCODING", "TRANSFERRING"]:
        return jsonify({"status": "error", "message": "Pipeline is already running"}), 400
    
    countdown_cancel_event.set()
    run_autorip_pipeline_async()
    return jsonify({"status": "success", "message": "Pipeline started"})

@app.route("/api/cancel-autostart", methods=["POST"])
def cancel_autostart():
    countdown_cancel_event.set()
    state["stage"] = "IDLE"
    state["status_message"] = "Auto-Start Cancelled"
    state["auto_start_countdown"] = 0
    return jsonify({"status": "success", "message": "Auto-start cancelled"})

@app.route("/api/eject", methods=["POST"])
def eject_drive():
    subprocess.run(["powershell", "-Command", "(New-Object -ComObject WMPlayer.OCX.7).cdromCollection.Item(0).Eject()"], capture_output=True)
    state["disc_present"] = False
    state["disc_label"] = "Ejected"
    add_log(f"Drive {state['drive_letter']} ejected manually.")
    return jsonify({"status": "success", "message": "Drive ejected"})

@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.json or {}
    for key, val in data.items():
        if key in ["season_number", "start_episode", "min_length_sec"]:
            try:
                state["settings"][key] = int(val)
            except Exception:
                pass
        elif key in ["auto_eject", "auto_rename", "include_episode_titles"]:
            state["settings"][key] = bool(val)
        else:
            state["settings"][key] = str(val)
            
    threading.Thread(target=fetch_media_artwork, daemon=True).start()
    return jsonify({"status": "success", "settings": state["settings"]})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
