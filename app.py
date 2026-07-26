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
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True


# --- Configuration & Paths ---
MAKEMKV_PATH = r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe"
HANDBRAKE_PATH = r"C:\Users\matt\AppData\Local\Microsoft\WinGet\Packages\HandBrake.HandBrake.CLI_Microsoft.Winget.Source_8wekyb3d8bbwe\HandBrakeCLI.exe"
PS3_DUMPER_PATH = r"C:\Tools\ps3-disc-dumper\ps3-disc-dumper.exe"
TEMP_RAW_DIR = r"C:\AutoRipTemp\Raw"
TEMP_ENCODED_DIR = r"C:\AutoRipTemp\Encoded"
DEFAULT_TV_DESTINATION = r"Z:\TV Shows"
DEFAULT_MOVIE_DESTINATION = r"Z:\Movies"
DEFAULT_PS3_DESTINATION = r"Z:\Games\PS3"
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")


# --- Global State ---
state = {
    "stage": "IDLE",            # IDLE, COUNTDOWN, RIPPING, ENCODING, TRANSFERRING, COMPLETE, ERROR
    "status_message": "System Ready - Insert a disc to begin",
    "progress_pct": 0,
    "current_file": "",
    "current_title": 0,
    "total_titles": 0,
    "drive_letter": "D:",
    "disc_label": "Avatar_Book_1_Disc_1",
    "disc_present": False,
    "disc_type": "DVD",          # DVD or Blu-ray
    "fps": "0",
    "eta": "--:--",
    "overall_eta": "--:--",
    "start_timestamp": 0,
    "auto_start_countdown": 0,
    "auto_start_enabled": True,
    "artwork_url": "https://static.tvmaze.com/uploads/images/original_untouched/633/1582667.jpg",
    "media_summary": "A young boy known as the Avatar must master the four elemental powers to save a world at war.",
    "nas_storage": {"free_gb": 0, "total_gb": 0, "used_pct": 0},
    "logs": [f"{time.strftime('[%H:%M:%S]')} System Ready - AutoRip Control Center Online"],
    "settings": {
        "media_type": "tv",       # tv, movie, or ps3
        "format": "mp4",          # mp4 or mkv
        "preset": "NVIDIA NVENC H.264",
        "min_length_sec": 300,
        "auto_eject": True,
        "auto_rename": True,
        "include_episode_titles": True,
        "show_name": "Avatar: The Last Airbender",
        "movie_title": "Dragon Ball Z Dead Zone",
        "release_year": "2005",
        "season_number": 1,
        "start_episode": 1,
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
        elif media_type == "movie":
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
            "footer": {"text": "AutoRip Control Center • NAS Processing Suite"}
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
        usage = shutil.disk_usage(r"Z:\ ")
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

DBZ_MOVIES = {
    "DRAGON_BALL_Z_MOVIE_1": ("Dragon Ball Z: Dead Zone", "1989"),
    "DRAGON_BALL_Z_MOVIE_2": ("Dragon Ball Z: The World's Strongest", "1990"),
    "DRAGON_BALL_Z_MOVIE_3": ("Dragon Ball Z: The Tree of Might", "1990"),
    "DRAGON_BALL_Z_MOVIE_4": ("Dragon Ball Z: Lord Slug", "1991"),
    "DRAGON_BALL_Z_MOVIE_5": ("Dragon Ball Z: Cooler's Revenge", "1991"),
    "DRAGON_BALL_Z_MOVIE_6": ("Dragon Ball Z: The Return of Cooler", "1992"),
    "DRAGON_BALL_Z_MOVIE_7": ("Dragon Ball Z: Super Android 13!", "1992"),
    "DRAGON_BALL_Z_MOVIE_8": ("Dragon Ball Z: Broly - The Legendary Super Saiyan", "1993"),
    "DRAGON_BALL_Z_MOVIE_9": ("Dragon Ball Z: Bojack Unbound", "1993"),
    "DRAGON_BALL_Z_MOVIE_10": ("Dragon Ball Z: Broly - Second Coming", "1994"),
    "DRAGON_BALL_Z_MOVIE_11": ("Dragon Ball Z: Bio-Broly", "1994"),
    "DRAGON_BALL_Z_MOVIE_12": ("Dragon Ball Z: Fusion Reborn", "1995"),
    "DRAGON_BALL_Z_MOVIE_13": ("Dragon Ball Z: Wrath of the Dragon", "1995"),
}

def parse_disc_label_media(label):
    if not label or label in ["Empty Drive / No Disc", "Ejected", "Drive Error / Empty"]:
        return

    # 1. Direct DBZ & Known Franchise Dictionary Check
    upper_label = label.upper().strip()
    if upper_label in DBZ_MOVIES:
        title, year = DBZ_MOVIES[upper_label]
        state["settings"]["media_type"] = "movie"
        state["settings"]["movie_title"] = title
        state["settings"]["release_year"] = year
        add_log(f"[Auto-Detect] Recognized disc from Franchise Map: '{title}' ({year})")
        fetch_media_artwork()
        return

    # 2. General Clean Query
    clean_label = label.replace("_", " ").replace(".", " ").replace("-", " ")
    for word in ["VOLUME", "VOL", "DISC", "SEASON", "DES", "DVD", "BLURAY", "MOVIE", "FEATURE", "SPECIAL", "EDITION"]:
        clean_label = clean_label.replace(word, " ").replace(word.lower(), " ")
        
    query = clean_label.strip()
    if len(query) < 3:
        return

    try:
        url = f"https://api.tvmaze.com/singlesearch/shows?q={urllib.parse.quote(query)}"
        req = urllib.request.urlopen(url, timeout=3)
        data = json.loads(req.read().decode())
        if data and "name" in data:
            state["settings"]["media_type"] = "tv"
            state["settings"]["show_name"] = data["name"]
            if "premiered" in data and data["premiered"]:
                state["settings"]["release_year"] = data["premiered"].split("-")[0]
            add_log(f"[Auto-Detect] Recognized disc as TV Show: '{data['name']}' ({state['settings']['release_year']})")
            fetch_media_artwork()
            return
    except Exception:
        pass

    try:
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=movie&limit=1"
        req = urllib.request.urlopen(url, timeout=3)
        data = json.loads(req.read().decode())
        if data and "results" in data and len(data["results"]) > 0:
            item = data["results"][0]
            state["settings"]["media_type"] = "movie"
            state["settings"]["movie_title"] = item.get("trackName", query)
            if "releaseDate" in item:
                state["settings"]["release_year"] = item["releaseDate"].split("-")[0]
            add_log(f"[Auto-Detect] Recognized disc as Movie: '{state['settings']['movie_title']}' ({state['settings']['release_year']})")
            fetch_media_artwork()
    except Exception:
        pass

def check_drive_status():
    if state["stage"] in ["RIPPING", "ENCODING", "TRANSFERRING"]:
        return

    drive = state["drive_letter"][0]
    previous_disc_present = state["disc_present"]
    try:
        cmd = f"(Get-Volume -DriveLetter '{drive}' -ErrorAction SilentlyContinue).FileSystemLabel"
        output = subprocess.check_output(["powershell", "-Command", cmd], text=True).strip()
        if output:
            state["disc_label"] = output
            state["disc_present"] = True
            
            if "BD" in output.upper() or "BLURAY" in output.upper() or "PS3" in output.upper():
                state["disc_type"] = "Blu-ray"
            else:
                state["disc_type"] = "DVD"
                
            if not previous_disc_present:
                threading.Thread(target=parse_disc_label_media, args=(output,), daemon=True).start()
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

def trigger_plex_refresh():
    url = state["settings"]["plex_url"]
    token = state["settings"]["plex_token"]
    if url:
        try:
            req_url = f"{url.rstrip('/')}/library/sections/all/refresh"
            if token:
                req_url += f"?X-Plex-Token={token}"
            add_log(f"Sending Plex refresh signal to {url}...")
            urllib.request.urlopen(req_url, timeout=5)
            add_log("Plex library refresh triggered successfully!")
        except Exception as e:
            add_log(f"Plex refresh failed: {e}")

def run_autorip_pipeline_async():
    thread = threading.Thread(target=run_autorip_pipeline, daemon=True)
    thread.start()

def run_autorip_pipeline():
    if not process_lock.acquire(blocking=False):
        add_log("Pipeline already running!")
        return

    start_time = time.time()
    state["start_timestamp"] = int(start_time)
    try:
        os.makedirs(TEMP_RAW_DIR, exist_ok=True)
        os.makedirs(TEMP_ENCODED_DIR, exist_ok=True)

        state["logs"] = [f"{time.strftime('[%H:%M:%S]')} === Starting AutoRip Pipeline ==="]

        check_drive_status()
        if not state["disc_present"]:
            add_log("No disc detected in drive D:. Aborting.")
            state["stage"] = "ERROR"
            state["status_message"] = "No disc in drive"
            return

        label = state["disc_label"]
        fmt = state["settings"]["format"]
        media_type = state["settings"]["media_type"]
        
        if media_type == "ps3":
            state["stage"] = "RIPPING"
            state["status_message"] = f"Dumping PS3 Game Disc with PS3 Disc Dumper ({label})..."
            state["progress_pct"] = 15
            add_log(f"=== STAGE 1/1: PS3 DISC DUMPER STARTED ({label}) ===")
            
            ps3_out_dir = os.path.join(DEFAULT_PS3_DESTINATION, label)
            os.makedirs(ps3_out_dir, exist_ok=True)
            
            cmd_ps3 = [PS3_DUMPER_PATH]
            p_ps3 = subprocess.Popen(cmd_ps3, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=r"C:\Tools\ps3-disc-dumper")
            for line in p_ps3.stdout:
                line_str = line.strip()
                if line_str:
                    add_log(f"[PS3 Dumper] {line_str}")
                    if "%" in line_str:
                        try:
                            pct = int(line_str.split("%")[0].split()[-1])
                            state["progress_pct"] = min(pct, 95)
                        except Exception:
                            pass

            p_ps3.wait()
            state["stage"] = "COMPLETE"
            state["status_message"] = f"PS3 Game Disc Dumped Successfully to {ps3_out_dir}!"
            state["progress_pct"] = 100
            
            send_discord_notification(
                title=f"🎮 PS3 Game Dumped: {label}",
                description=f"Successfully dumped PS3 game disc directly to NAS!",
                fields=[
                    {"name": "Game Label", "value": label, "inline": True},
                    {"name": "Destination", "value": f"`{ps3_out_dir}`", "inline": False}
                ]
            )
            
            if state["settings"]["auto_eject"]:
                subprocess.run(["powershell", "-Command", "(New-Object -ComObject WMPlayer.OCX.7).cdromCollection.Item(0).Eject()"], capture_output=True)
            return

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

        for f in os.listdir(TEMP_RAW_DIR):
            fp = os.path.join(TEMP_RAW_DIR, f)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass

        cmd_rip = [
            MAKEMKV_PATH,
            "-r", "mkv", "disc:0", "all", TEMP_RAW_DIR,
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
            
            raw_files = glob.glob(os.path.join(TEMP_RAW_DIR, "*.mkv"))
            if raw_files:
                completed_count = len(raw_files)
                state["progress_pct"] = min(10 + int((completed_count / 10.0) * 40), 48)

        p_rip.wait()
        if p_rip.returncode != 0:
            add_log(f"[MakeMKV] Extraction failed with exit code {p_rip.returncode}")
            state["stage"] = "ERROR"
            state["status_message"] = "MakeMKV Rip Failed"
            return

        raw_files = sorted(glob.glob(os.path.join(TEMP_RAW_DIR, "*.mkv")))
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
            
            out_file = os.path.join(TEMP_ENCODED_DIR, target_filename)
            add_log(f"[HandBrake] [{idx}/{len(target_files)}] Starting encode -> '{target_filename}'")

            cmd_enc = [HANDBRAKE_PATH, "-i", raw_file, "-o", out_file]
            
            # Preset & GPU Acceleration logic
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
                                    eta_str = p.strip().replace("ETA ", "")
                                    state["eta"] = eta_str
                                    # Calculate overall ETA across remaining titles
                                    try:
                                        eta_parts = [int(x) for x in eta_str.split(":")]
                                        if len(eta_parts) == 3:
                                            single_sec = eta_parts[0]*3600 + eta_parts[1]*60 + eta_parts[2]
                                        elif len(eta_parts) == 2:
                                            single_sec = eta_parts[0]*60 + eta_parts[1]
                                        else:
                                            single_sec = 0
                                        
                                        rem_titles = max(0, len(target_files) - idx)
                                        tot_sec = single_sec + (rem_titles * max(single_sec, 180))
                                        m, s = divmod(tot_sec, 60)
                                        h, m = divmod(m, 60)
                                        if h > 0:
                                            state["overall_eta"] = f"{h:02d}:{m:02d}:{s:02d}"
                                        else:
                                            state["overall_eta"] = f"{m:02d}:{s:02d}"
                                    except Exception:
                                        pass
                    except Exception:
                        pass

            p_enc.wait()
            if p_enc.returncode == 0 and os.path.exists(out_file):
                size_mb = round(os.path.getsize(out_file) / (1024 * 1024), 1)
                encoded_files.append(out_file)
                add_log(f"[HandBrake] Successfully encoded '{target_filename}' ({size_mb} MB)")
            else:
                add_log(f"[HandBrake] Encoding failed for title {idx}.")

        # --- STEP 3: TRANSFERRING TO NAS ---
        state["stage"] = "TRANSFERRING"
        add_log(f"=== STAGE 3/3: TRANSFERRING {len(encoded_files)} FILE(S) TO NAS ===")
        state["status_message"] = f"Moving {len(encoded_files)} file(s) to NAS..."
        state["progress_pct"] = 95

        if state["settings"]["auto_rename"]:
            if media_type == "movie":
                nas_folder = os.path.join(DEFAULT_MOVIE_DESTINATION, f"{movie_title} ({year})")
            else:
                nas_folder = os.path.join(DEFAULT_TV_DESTINATION, f"{show_name} ({year})", f"Season {season:02d}")
        else:
            nas_folder = os.path.join(DEFAULT_TV_DESTINATION, "Uncategorized")
            
        os.makedirs(nas_folder, exist_ok=True)
        add_log(f"[NAS] Destination Directory: {nas_folder}")

        moved_names = []
        for ef in encoded_files:
            dest = os.path.join(nas_folder, os.path.basename(ef))
            size_mb = round(os.path.getsize(ef) / (1024 * 1024), 1)
            shutil.move(ef, dest)
            moved_names.append(os.path.basename(ef))
            add_log(f"[NAS Transfer] Moved '{os.path.basename(ef)}' ({size_mb} MB) -> {dest}")

        if media_type == "tv" and state["settings"]["auto_rename"]:
            state["settings"]["start_episode"] = start_ep + len(encoded_files)
            add_log(f"Auto-incremented TV episode counter to S{season:02d}E{state['settings']['start_episode']:02d} for next disc!")

        # --- STEP 4: COMPLETE & EJECT ---
        state["stage"] = "COMPLETE"
        state["status_message"] = f"Finished! {len(encoded_files)} file(s) saved to NAS."
        state["progress_pct"] = 100
        state["fps"] = "0"
        state["eta"] = "--:--"
        state["overall_eta"] = "--:--"
        state["start_timestamp"] = 0

        duration_min = round((time.time() - start_time) / 60, 1)
        save_history_entry({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "disc_label": label,
            "media_type": media_type.upper(),
            "disc_type": state["disc_type"],
            "episodes_saved": len(encoded_files),
            "destination": nas_folder,
            "duration_min": duration_min
        })

        trigger_plex_refresh()

        # Send Discord Webhook Embed Notification
        send_discord_notification(
            title=f"🎉 Disc Processing Complete: {label}",
            description=f"Successfully ripped and encoded **{len(encoded_files)} episode(s)** to NAS in **{duration_min} minutes**!",
            poster_url=state["artwork_url"],
            fields=[
                {"name": "Media Target", "value": f"{show_name if media_type=='tv' else movie_title} ({year})", "inline": True},
                {"name": "Disc Type", "value": state["disc_type"], "inline": True},
                {"name": "Destination", "value": f"`{nas_folder}`", "inline": False},
                {"name": "Episodes Saved", "value": "\n".join([f"• `{n}`" for n in moved_names[:5]]) + (f"\n*...and {len(moved_names)-5} more*" if len(moved_names)>5 else ""), "inline": False}
            ]
        )

        if state["settings"]["auto_eject"]:
            add_log("Ejecting drive D:...")
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
    last_raw_bytes = 0
    last_check_time = time.time()
    while True:
        time.sleep(1.5)
        check_nas_storage()

        if state["stage"] == "RIPPING":
            raw_dir = TEMP_RAW_DIR
            if os.path.exists(raw_dir):
                total_bytes = sum(os.path.getsize(os.path.join(raw_dir, f)) for f in os.listdir(raw_dir) if os.path.isfile(os.path.join(raw_dir, f)))
                now = time.time()
                dt = now - last_check_time
                if dt > 0 and last_raw_bytes > 0:
                    mb_s = round(((total_bytes - last_raw_bytes) / (1024 * 1024)) / dt, 1)
                    if mb_s >= 0:
                        state["fps"] = f"{mb_s} MB/s"
                last_raw_bytes = total_bytes
                last_check_time = now
                total_mb = round(total_bytes / (1024 * 1024), 1)
                if total_mb > 0:
                    state["status_message"] = f"MakeMKV Extracting Disc Data ({total_mb} MB total extracted)..."

        if state["stage"] in ["IDLE", "COUNTDOWN", "COMPLETE"]:
            check_drive_status()
            if state["stage"] == "COMPLETE" and not state["disc_present"]:
                state["stage"] = "IDLE"
                state["status_message"] = "System Ready - Insert a disc to begin"

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
    nas_files = []
    media_type = state["settings"]["media_type"]
    show_name = state["settings"]["show_name"]
    movie_title = state["settings"]["movie_title"]
    year = state["settings"]["release_year"]
    season = state["settings"]["season_number"]

    if media_type == "movie":
        target_nas_dir = os.path.join(DEFAULT_MOVIE_DESTINATION, f"{movie_title} ({year})")
    elif media_type == "ps3":
        target_nas_dir = DEFAULT_PS3_DESTINATION
    else:
        target_nas_dir = os.path.join(DEFAULT_TV_DESTINATION, f"{show_name} ({year})", f"Season {season:02d}")

    if os.path.exists(target_nas_dir):
        files_with_time = []
        for f in os.listdir(target_nas_dir):
            fp = os.path.join(target_nas_dir, f)
            if os.path.isfile(fp):
                files_with_time.append((f, os.path.getmtime(fp), round(os.path.getsize(fp) / (1024 * 1024), 1)))
        
        files_with_time.sort(key=lambda x: x[1], reverse=True)
        for f, mtime, size_mb in files_with_time[:20]:
            nas_files.append({"name": f, "size_mb": size_mb})
                
    state_copy = dict(state)
    state_copy["logs"] = state["logs"][-50:] if state["logs"] else ["System ready - waiting for process logs..."]

    return jsonify({
        "state": state_copy,
        "nas_files": nas_files,
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
    add_log("Drive D: ejected manually.")
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

@app.route("/api/barcode-lookup", methods=["POST"])
def barcode_lookup():
    data = request.json or {}
    upc_raw = data.get("upc", "").strip()
    if not upc_raw:
        return jsonify({"status": "error", "message": "No UPC provided"}), 400

    if len(upc_raw) < 12 and upc_raw.isdigit():
        upc = upc_raw.zfill(12)
    else:
        upc = upc_raw

    add_log(f"[Barcode Engine] Looking up UPC Barcode: {upc_raw} (Padded: {upc})...")
    
    # 1. Query UPCItemDB API
    try:
        url = f"https://api.upcitemdb.com/prod/trial/lookup?upc={upc}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        res = urllib.request.urlopen(req, timeout=4)
        item_data = json.loads(res.read().decode())
        items = item_data.get("items", [])
        if items:
            title = items[0].get("title", "")
            add_log(f"[Barcode Engine] Found UPCItemDB Title: '{title}'")
            
            clean_title = title
            for sub in ["(Blu-ray)", "(DVD)", "[Blu-ray]", "[DVD]", "Blu-ray", "Blu Ray", "DVD", ": The Complete Series", "The Complete Series", "Complete Series", "Season", "Collection", "Box Set", "(4K)", "[4K]"]:
                clean_title = clean_title.replace(sub, "")
            clean_title = clean_title.strip(" :-()")
            add_log(f"[Barcode Engine] Cleaned Search Query: '{clean_title}'")

            # Query TVMaze or iTunes for media details
            url_tv = f"https://api.tvmaze.com/singlesearch/shows?q={urllib.parse.quote(clean_title)}"
            try:
                res_tv = urllib.request.urlopen(url_tv, timeout=3)
                tv_data = json.loads(res_tv.read().decode())
                if tv_data and "name" in tv_data:
                    state["settings"]["media_type"] = "tv"
                    state["settings"]["show_name"] = tv_data["name"]
                    if "premiered" in tv_data and tv_data["premiered"]:
                        state["settings"]["release_year"] = tv_data["premiered"].split("-")[0]
                    fetch_media_artwork()
                    return jsonify({"status": "success", "media_type": "tv", "title": tv_data["name"], "year": state["settings"]["release_year"], "artwork": state["artwork_url"]})
            except Exception:
                pass

            # Try iTunes Movie Search
            url_movie = f"https://itunes.apple.com/search?term={urllib.parse.quote(clean_title)}&entity=movie&limit=1"
            try:
                res_m = urllib.request.urlopen(url_movie, timeout=3)
                m_data = json.loads(res_m.read().decode())
                if m_data and "results" in m_data and len(m_data["results"]) > 0:
                    m_item = m_data["results"][0]
                    state["settings"]["media_type"] = "movie"
                    state["settings"]["movie_title"] = m_item.get("trackName", clean_title)
                    if "releaseDate" in m_item:
                        state["settings"]["release_year"] = m_item["releaseDate"].split("-")[0]
                    fetch_media_artwork()
                    return jsonify({"status": "success", "media_type": "movie", "title": state["settings"]["movie_title"], "year": state["settings"]["release_year"], "artwork": state["artwork_url"]})
            except Exception:
                pass
    except Exception as e:
        add_log(f"[Barcode Engine] Note: {e}")

    # 2. Fallback Direct Search
    try:
        url_movie = f"https://itunes.apple.com/search?term={urllib.parse.quote(upc)}&entity=movie&limit=1"
        res_m = urllib.request.urlopen(url_movie, timeout=3)
        m_data = json.loads(res_m.read().decode())
        if m_data and "results" in m_data and len(m_data["results"]) > 0:
            m_item = m_data["results"][0]
            state["settings"]["media_type"] = "movie"
            state["settings"]["movie_title"] = m_item.get("trackName", "")
            if "releaseDate" in m_item:
                state["settings"]["release_year"] = m_item["releaseDate"].split("-")[0]
            fetch_media_artwork()
            return jsonify({"status": "success", "media_type": "movie", "title": state["settings"]["movie_title"], "year": state["settings"]["release_year"], "artwork": state["artwork_url"]})
    except Exception:
        pass

    return jsonify({"status": "error", "message": "Barcode not found in database. Please enter title manually."}), 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
