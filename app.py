import os
import sys
import glob
import time
import json
import shutil
import subprocess
import threading
import csv
import io
import urllib.request
import urllib.parse
import difflib
import ctypes
import re
from flask import Flask, render_template, jsonify, request

NO_WINDOW_FLAG = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)

def get_volume_label(drive_letter):
    """Retrieve drive volume label using native Win32 C API without spawning console windows."""
    try:
        drive = f"{drive_letter.upper()}:\\"
        volume_name_buffer = ctypes.create_unicode_buffer(1024)
        file_system_name_buffer = ctypes.create_unicode_buffer(1024)
        serial_number = ctypes.c_ulong()
        max_component_length = ctypes.c_ulong()
        file_system_flags = ctypes.c_ulong()
        
        rc = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive),
            volume_name_buffer,
            ctypes.sizeof(volume_name_buffer),
            ctypes.byref(serial_number),
            ctypes.byref(max_component_length),
            ctypes.byref(file_system_flags),
            file_system_name_buffer,
            ctypes.sizeof(file_system_name_buffer)
        )
        if rc:
            return volume_name_buffer.value
    except Exception:
        pass
    return ""


app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True


# --- Configuration & Paths ---
def resolve_tool_path(configured_path, exe_name, extra_search_roots):
    """Verify a configured tool path exists; if not, search common install
    locations for it. Returns (path_or_None, found_via_fallback: bool)."""
    if configured_path and os.path.isfile(configured_path):
        return configured_path, False

    search_roots = [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
    ] + extra_search_roots

    for root in search_roots:
        if not os.path.isdir(root):
            continue
        pattern = os.path.join(root, "**", exe_name)
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0], True

    return None, False

MAKEMKV_PATH_CONFIGURED = r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe"
HANDBRAKE_PATH_CONFIGURED = r"C:\Users\matt\AppData\Local\Microsoft\WinGet\Packages\HandBrake.HandBrake.CLI_Microsoft.Winget.Source_8wekyb3d8bbwe\HandBrakeCLI.exe"

MAKEMKV_PATH, _makemkv_fallback = resolve_tool_path(MAKEMKV_PATH_CONFIGURED, "makemkvcon64.exe", [])
HANDBRAKE_PATH, _handbrake_fallback = resolve_tool_path(HANDBRAKE_PATH_CONFIGURED, "HandBrakeCLI.exe", [])
FFPROBE_PATH, _ = resolve_tool_path(None, "ffprobe.exe", [])

TOOL_CONFIG_ERRORS = []
if MAKEMKV_PATH is None:
    TOOL_CONFIG_ERRORS.append("MakeMKV (makemkvcon64.exe) was not found at the configured path or in common install locations. Ripping will fail until this is fixed.")
elif _makemkv_fallback:
    TOOL_CONFIG_ERRORS.append(f"MakeMKV was not found at the configured path — auto-detected fallback at '{MAKEMKV_PATH}' instead. Consider updating MAKEMKV_PATH_CONFIGURED.")
if HANDBRAKE_PATH is None:
    TOOL_CONFIG_ERRORS.append("HandBrakeCLI.exe was not found at the configured path or in common install locations. Encoding will fail until this is fixed.")
elif _handbrake_fallback:
    TOOL_CONFIG_ERRORS.append(f"HandBrakeCLI was not found at the configured path — auto-detected fallback at '{HANDBRAKE_PATH}' instead. Consider updating HANDBRAKE_PATH_CONFIGURED.")
if FFPROBE_PATH is None:
    TOOL_CONFIG_ERRORS.append("ffprobe.exe was not found — duration-based outlier detection for the Recover feature will be disabled (falls back to including everything).")

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
    "disc_label": "No Disc Detected",
    "disc_present": False,
    "disc_type": "DVD",          # DVD or Blu-ray
    "fps": "0",
    "eta": "--:--",
    "overall_eta": "--:--",
    "start_timestamp": 0,
    "queue_count": 0,
    "current_raw_dir": "",

    "auto_start_countdown": 0,
    "auto_start_enabled": True,
    "artwork_url": "https://static.tvmaze.com/uploads/images/original_untouched/633/1582667.jpg",
    "media_summary": "A young boy known as the Avatar must master the four elemental powers to save a world at war.",
    "nas_storage": {"free_gb": 0, "total_gb": 0, "used_pct": 0},

    "logs": [f"{time.strftime('[%H:%M:%S]')} System Ready - AutoRip Control Center Online"],
    "settings": {
        "media_type": "tv",       # tv or movie
        "format": "mp4",          # mp4 or mkv
        "preset": "Auto",         # Auto, HQ 720p30 Surround, HQ 1080p30 Surround, NVENC H.264, NVENC H.265, Intel QSV, AMD VCE
        "min_length_sec": 600,  # 10 min — raised from 5 to cut down on junk short titles (recaps/previews) at the source; the duration-outlier filter is the primary defense
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

state_lock = threading.RLock()
history_lock = threading.Lock()
rip_lock = threading.Lock()       # one disc ripped at a time (optical drive)
encode_lock = threading.Lock()    # one encode/transfer job at a time (CPU/GPU)
process_lock = rip_lock           # legacy alias used by /api/start
encode_queue = []
encode_queue_lock = threading.Lock()
countdown_cancel_event = threading.Event()

LAST_NAS_CHECK = 0

def request_nas_cache_refresh():
    global LAST_NAS_CHECK
    LAST_NAS_CHECK = 0

def detect_hardware_encoders():
    detected = []
    if HANDBRAKE_PATH and os.path.exists(HANDBRAKE_PATH):
        try:
            out = subprocess.check_output([HANDBRAKE_PATH, "--help"], text=True, stderr=subprocess.STDOUT, timeout=10, creationflags=NO_WINDOW_FLAG)
            out_lower = out.lower()
            if "nvenc_h264" in out_lower or "nvenc" in out_lower:
                detected.append("NVENC H.264")
            if "nvenc_h265" in out_lower or "nvenc" in out_lower:
                detected.append("NVENC H.265")
            if "qsv_h264" in out_lower or "qsv" in out_lower:
                detected.append("Intel QSV")
            if "vce_h264" in out_lower or "vce" in out_lower:
                detected.append("AMD VCE")
        except Exception:
            pass
    with state_lock:
        state["detected_encoders"] = detected
        if detected and state["settings"]["preset"] == "Auto":
            if "NVENC H.264" in detected:
                state["settings"]["preset"] = "NVIDIA NVENC H.264"
            elif "Intel QSV" in detected:
                state["settings"]["preset"] = "Intel QSV"
    if detected:
        add_log(f"[Startup] Hardware GPU Encoders Active: {', '.join(detected)}")
    else:
        add_log("[Startup] Software CPU Encoding active (No GPU hardware encoder detected).")

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_history_entry(entry):
    with history_lock:
        history = load_history()
        history.insert(0, entry)
        history = history[:50]
        try:
            temp_file = f"{HISTORY_FILE}.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
            os.replace(temp_file, HISTORY_FILE)
        except Exception as e:
            add_log(f"Error saving history: {e}")

def add_log(msg):
    timestamp = time.strftime("[%H:%M:%S]")
    entry = f"{timestamp} {msg}"
    print(entry, flush=True)
    with state_lock:
        state["logs"].append(entry)
        if len(state["logs"]) > 300:
            state["logs"].pop(0)

for _tool_error in TOOL_CONFIG_ERRORS:
    add_log(f"[STARTUP WARNING] {_tool_error}")

detect_hardware_encoders()

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
            if not data:
                add_log(f"[Artwork] TVMaze had no match for '{query}' — poster/summary left unchanged.")
        else:
            query = state["settings"]["movie_title"]
            found = False
            try:
                url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=movie&limit=1"
                req = urllib.request.urlopen(url, timeout=3)
                data = json.loads(req.read().decode())
                if data and "results" in data and len(data["results"]) > 0:
                    item = data["results"][0]
                    if "artworkUrl100" in item:
                        state["artwork_url"] = item["artworkUrl100"].replace("100x100bb", "600x600bb")
                    if "longDescription" in item:
                        state["media_summary"] = item["longDescription"]
                    found = True
            except Exception:
                pass

            if not found:
                try:
                    wiki_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(query)}"
                    w_req = urllib.request.Request(wiki_url, headers={"User-Agent": "AutoRipControlCenter/1.0"})
                    w_data = json.loads(urllib.request.urlopen(w_req, timeout=4).read())
                    if w_data.get("thumbnail", {}).get("source"):
                        state["artwork_url"] = w_data["thumbnail"]["source"]
                    if w_data.get("extract"):
                        state["media_summary"] = w_data["extract"]
                    add_log(f"[Artwork] Retrieved poster and summary from Wikipedia for '{query}'")
                except Exception:
                    add_log(f"[Artwork] Search had no match for '{query}' — poster/summary left unchanged.")
    except Exception as e:
        add_log(f"[Artwork] Fetch failed: {e}")

def fetch_episode_title(show_name, season_num, ep_num):
    def sanitize_title(name):
        for char in r'/\:*?"<>|':
            name = name.replace(char, "")
        return name.strip()

    # Clean show name for search (remove parenthetical years or tags)
    clean_show = re.sub(r'\s*\([^)]*\)', '', show_name).strip() if 're' in sys.modules else show_name
    queries = [show_name]
    if clean_show and clean_show != show_name:
        queries.append(clean_show)

    for q in queries:
        try:
            url = f"https://api.tvmaze.com/singlesearch/shows?q={urllib.parse.quote(q)}&embed=episodes"
            req = urllib.request.urlopen(url, timeout=4)
            data = json.loads(req.read().decode())
            episodes = data.get("_embedded", {}).get("episodes", [])
            for ep in episodes:
                if ep.get("season") == int(season_num) and ep.get("number") == int(ep_num):
                    ep_name = ep.get("name", "")
                    if ep_name:
                        return sanitize_title(ep_name)
        except Exception:
            pass

    # Fallback to search endpoint if single search failed
    try:
        url = f"https://api.tvmaze.com/search/shows?q={urllib.parse.quote(clean_show or show_name)}"
        req = urllib.request.urlopen(url, timeout=4)
        shows = json.loads(req.read().decode())
        if shows:
            show_id = shows[0].get("show", {}).get("id")
            if show_id:
                ep_url = f"https://api.tvmaze.com/shows/{show_id}/episodes"
                ep_req = urllib.request.urlopen(ep_url, timeout=4)
                episodes = json.loads(ep_req.read().decode())
                for ep in episodes:
                    if ep.get("season") == int(season_num) and ep.get("number") == int(ep_num):
                        ep_name = ep.get("name", "")
                        if ep_name:
                            return sanitize_title(ep_name)
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

def normalize_media_folder_name(name):
    n = name.strip().lower()
    for prefix in ("the ", "a ", "an "):
        if n.startswith(prefix):
            n = n[len(prefix):]
            break
    return " ".join(n.split())


def find_existing_media_folder(root_dir, proposed_name):
    """Match a proposed show/movie folder name against existing folders in
    root_dir, ignoring case and a leading 'The '/'A '/'An ' — prevents stray
    duplicate folders (e.g. 'Legend of Korra' vs 'The Legend of Korra') when
    show_name drifts slightly between rips or app restarts."""
    try:
        existing = os.listdir(root_dir)
    except Exception:
        return proposed_name
    target_norm = normalize_media_folder_name(proposed_name)
    for name in existing:
        full = os.path.join(root_dir, name)
        if os.path.isdir(full) and normalize_media_folder_name(name) == target_norm:
            return name
    return proposed_name


def check_nas_storage():
    try:
        usage = shutil.disk_usage("Z:\\")
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

AUTO_DETECT_MATCH_THRESHOLD = 0.5


def _label_match_score(query, candidate_name):
    if not candidate_name:
        return 0.0
    q_low = query.lower().strip()
    c_low = candidate_name.lower().strip()
    if q_low in c_low or c_low in q_low:
        return 0.90
    return difflib.SequenceMatcher(None, q_low, c_low).ratio()


def _lookup_tv_candidate(query):
    try:
        url = f"https://api.tvmaze.com/singlesearch/shows?q={urllib.parse.quote(query)}"
        req = urllib.request.urlopen(url, timeout=3)
        data = json.loads(req.read().decode())
        if data and "name" in data:
            return data
    except Exception:
        pass
    return None


def _lookup_movie_candidate(query):
    queries = [query]
    # Fallback query stripping trailing numbers/subtitle fragments
    if len(query.split()) > 2:
        queries.append(" ".join(query.split()[:3]))
        queries.append(" ".join(query.split()[:2]))

    for q in queries:
        try:
            url = f"https://itunes.apple.com/search?term={urllib.parse.quote(q)}&media=movie&limit=5"
            req = urllib.request.urlopen(url, timeout=4)
            data = json.loads(req.read().decode())
            results = data.get("results", [])
            if results:
                # Find best match or top movie
                for r in results:
                    if r.get("wrapperType") == "track" or r.get("kind") == "feature-movie":
                        return r
                return results[0]
        except Exception:
            pass

    # Wikipedia fallback search if iTunes catalog misses the title
    try:
        search_term = query + " film"
        wiki_url = "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=" + urllib.parse.quote(search_term) + "&format=json"
        req = urllib.request.Request(wiki_url, headers={"User-Agent": "AutoRipControlCenter/1.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=4).read())
        results = data.get("query", {}).get("search", [])
        if results:
            title = results[0]["title"].replace(" (film)", "").strip()
            snip = results[0].get("snippet", "")
            years = re.findall(r'\b(19\d\d|20\d\d)\b', snip)
            year = years[0] if years else ""
            return {"trackName": title, "releaseDate": f"{year}-01-01" if year else ""}
    except Exception:
        pass

    return None


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
    # Strip common format/edition tags (WS, FS, WIDESCREEN, FULLSCREEN, SE, EXTENDED, UNRATED, DIRECTORS_CUT, REMASTERED)
    clean_label = re.sub(r'\b(?:WS|FS|WIDESCREEN|FULLSCREEN|SE|SPECIAL_EDITION|DIRECTORS_CUT|UNRATED|EXTENDED|REMASTERED)\b', ' ', clean_label, flags=re.IGNORECASE)
    # Strip disc/part numbers (D1, D2, DISC1, CD1, PART1, etc.)
    clean_label = re.sub(r'\b(?:D|DISC|CD|PART|PT|VOL|VOLUME)\d+\b', ' ', clean_label, flags=re.IGNORECASE)
    for word in ["VOLUME", "VOL", "DISC", "SEASON", "DES", "DVD", "BLURAY", "MOVIE", "FEATURE", "SPECIAL", "EDITION"]:
        clean_label = re.sub(rf'\b{word}\b', ' ', clean_label, flags=re.IGNORECASE)

    clean_label = re.sub(r'\s+', ' ', clean_label).strip()
    query = clean_label
    if len(query) < 3:
        return

    # 3. Query both TVMaze and iTunes and score each match against the
    # cleaned label, instead of committing to TV on any hit — a movie whose
    # label loosely matches an unrelated show would otherwise get
    # misclassified before movie search is ever tried.
    tv_candidate = _lookup_tv_candidate(query)
    movie_candidate = _lookup_movie_candidate(query)

    tv_score = _label_match_score(query, tv_candidate.get("name")) if tv_candidate else 0.0
    movie_score = _label_match_score(query, movie_candidate.get("trackName")) if movie_candidate else 0.0

    if tv_score < AUTO_DETECT_MATCH_THRESHOLD and movie_score < AUTO_DETECT_MATCH_THRESHOLD:
        add_log(f"[Auto-Detect] No confident match for '{query}' (best TV={tv_score:.2f}, movie={movie_score:.2f}) — leaving media type/title as-is, set manually if needed.")
        return

    if tv_score >= movie_score:
        state["settings"]["media_type"] = "tv"
        state["settings"]["show_name"] = tv_candidate["name"]
        if tv_candidate.get("premiered"):
            state["settings"]["release_year"] = tv_candidate["premiered"].split("-")[0]
        add_log(f"[Auto-Detect] Recognized disc as TV Show: '{tv_candidate['name']}' ({state['settings']['release_year']}) [match={tv_score:.2f}]")
    else:
        state["settings"]["media_type"] = "movie"
        state["settings"]["movie_title"] = movie_candidate.get("trackName", query)
        if movie_candidate.get("releaseDate"):
            state["settings"]["release_year"] = movie_candidate["releaseDate"].split("-")[0]
        add_log(f"[Auto-Detect] Recognized disc as Movie: '{state['settings']['movie_title']}' ({state['settings']['release_year']}) [match={movie_score:.2f}]")

    fetch_media_artwork()


def parse_makemkv_duration_to_seconds(duration_str):
    try:
        parts = [int(p) for p in duration_str.strip().split(":")]
        if len(parts) == 3:
            h, m, s = parts
            return h * 3600 + m * 60 + s
        elif len(parts) == 2:
            m, s = parts
            return m * 60 + s
        return None
    except Exception:
        return None


def _classify_files_by_duration(raw_files, file_durations):
    """Shared outlier logic. file_durations is a possibly-partial
    {file_path: seconds} map; files without a known duration are included
    in the normal set by default (no penalty for missing data). Needs at
    least 3 known durations to compute a meaningful median.
    Returns (normal_files, outlier_files_with_duration, insufficient_data)."""
    durations = list(file_durations.values())
    if len(durations) < 3:
        return raw_files, [], True

    sorted_durs = sorted(durations)
    n = len(sorted_durs)
    median = sorted_durs[n // 2] if n % 2 else (sorted_durs[n // 2 - 1] + sorted_durs[n // 2]) / 2
    if median <= 0:
        return raw_files, [], True

    normal, outliers = [], []
    for f in raw_files:
        dur = file_durations.get(f)
        if dur is None:
            normal.append(f)
        elif dur < 0.5 * median or dur > 1.8 * median:
            outliers.append((f, dur))
        else:
            normal.append(f)

    return normal, outliers, False


def classify_titles_by_duration(raw_files, title_durations):
    """Live-rip classification using MakeMKV's own MSG:3028 durations,
    index-matched to each ripped file via its _tNN filename suffix. Catches
    'play-all' compilations (way longer than a real episode) and
    recap/preview snippets (way shorter) that MakeMKV otherwise includes as
    if they were single episodes — the old logic only ever dropped whichever
    track came first, which missed compilations/snippets at other positions."""
    file_durations = {}
    for f in raw_files:
        base = os.path.basename(f)
        if "_t" in base:
            idx_str = base.rsplit("_t", 1)[1].split(".")[0]
            try:
                idx = int(idx_str)
            except ValueError:
                continue
            if idx in title_durations:
                file_durations[f] = title_durations[idx]
    return _classify_files_by_duration(raw_files, file_durations)


def probe_file_duration_seconds(filepath):
    if FFPROBE_PATH is None:
        return None
    try:
        out = subprocess.check_output(
            [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", filepath],
            text=True, timeout=30, creationflags=NO_WINDOW_FLAG
        )
        return float(out.strip())
    except Exception:
        return None


def classify_raw_dir_by_duration(raw_files):
    """Recovery-path classification (e.g. /api/reencode) for standalone raw
    folders where no live MakeMKV duration data exists — probes each file
    directly with ffprobe instead."""
    if FFPROBE_PATH is None:
        return raw_files, [], True
    file_durations = {}
    for f in raw_files:
        dur = probe_file_duration_seconds(f)
        if dur is not None:
            file_durations[f] = dur
    return _classify_files_by_duration(raw_files, file_durations)


def is_makemkv_process_running():
    """Detect an actual makemkvcon64.exe RIP already running (e.g. an
    orphaned rip from a previous crashed/restarted instance, or a second app
    instance) — prevents two rip processes fighting over the same optical
    drive, which corrupts both. Must check the command line, not just the
    image name: MakeMKV keeps a persistent 'guiserver' helper process
    (makemkvcon64.exe with no rip arguments) running at all times, which
    would otherwise false-positive on every single rip attempt."""
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='makemkvcon64.exe'\" "
            "| Select-Object -ExpandProperty CommandLine"
        )
        out = subprocess.check_output(["powershell", "-Command", ps_cmd], text=True, timeout=8, creationflags=NO_WINDOW_FLAG)
        for line in out.splitlines():
            line_lower = line.lower()
            if "-r" in line_lower and "disc:" in line_lower:
                return True
        return False
    except Exception:
        return False


def check_drive_status():
    drive = state["drive_letter"][0]
    if not drive.isalpha():
        add_log(f"Invalid drive letter '{drive}' — skipping drive check.")
        return

    previous_disc_present = state["disc_present"]
    previous_label = state["disc_label"]
    try:
        output = get_volume_label(drive).strip()
        if output:
            is_new_disc = (not previous_disc_present) or (output != previous_label and previous_label not in ["Empty Drive / No Disc", "Ejected", "Drive Error / Empty"])
            
            state["disc_label"] = output
            state["disc_present"] = True
            
            if "BD" in output.upper() or "BLURAY" in output.upper() or "PS3" in output.upper():
                state["disc_type"] = "Blu-ray"
            else:
                state["disc_type"] = "DVD"
                
            if is_new_disc:
                add_log(f"[Drive D:] New disc inserted: '{output}'")
                threading.Thread(target=parse_disc_label_media, args=(output,), daemon=True).start()
                if state["auto_start_enabled"] and not rip_lock.locked() and state["stage"] not in ["COUNTDOWN", "RIPPING"]:
                    trigger_auto_start_countdown()
        else:
            state["disc_label"] = "Empty Drive / No Disc"
            state["disc_present"] = False
            state["disc_type"] = "Unknown"
    except Exception:
        state["disc_label"] = "Drive Error / Empty"
        state["disc_present"] = False
        state["disc_type"] = "Unknown"

def trigger_auto_start_countdown():
    if rip_lock.locked() or state["stage"] in ["COUNTDOWN", "RIPPING"]:
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
    if not rip_lock.acquire(blocking=False):
        add_log("Rip already in progress!")
        return

    state["start_timestamp"] = int(time.time())
    try:
        if MAKEMKV_PATH is None:
            add_log("Cannot start rip: MakeMKV (makemkvcon64.exe) was not found. Check the STARTUP WARNING in the log.")
            state["stage"] = "ERROR"
            state["status_message"] = "MakeMKV not found — check config"
            state["start_timestamp"] = 0
            return

        if is_makemkv_process_running():
            add_log("Cannot start rip: a makemkvcon64.exe process is already running (likely an orphaned rip from a previous session, or a second app instance). Starting a second rip against the same drive corrupts both. Let it finish, then use Recover on its raw folder if needed.")
            state["stage"] = "ERROR"
            state["status_message"] = "Another MakeMKV rip is already running — see log"
            state["start_timestamp"] = 0
            return

        os.makedirs(TEMP_RAW_DIR, exist_ok=True)
        os.makedirs(TEMP_ENCODED_DIR, exist_ok=True)

        # Append rather than replace — a full reset here would silently
        # discard the auto-detect confirmation logged during the countdown
        # phase (parse_disc_label_media runs well before this point).
        add_log("=== Starting AutoRip Pipeline ===")

        for attempt in range(1, 4):
            check_drive_status()
            if state["disc_present"]:
                break
            add_log(f"Waiting for drive D: volume mount (Attempt {attempt}/3)...")
            time.sleep(2)

        if not state["disc_present"]:
            add_log("No disc detected in drive D:. Please ensure disc is pushed in fully.")
            state["stage"] = "ERROR"
            state["status_message"] = "No disc in drive - Push disc tray in"
            return

        label = state["disc_label"]
        media_type = state["settings"]["media_type"]

        if media_type == "ps3":
            state["stage"] = "RIPPING"
            state["status_message"] = f"Dumping PS3 Game Disc with PS3 Disc Dumper ({label})..."
            state["progress_pct"] = 15
            add_log(f"=== STAGE 1/1: PS3 DISC DUMPER STARTED ({label}) ===")

            ps3_out_dir = os.path.join(DEFAULT_PS3_DESTINATION, label)
            os.makedirs(ps3_out_dir, exist_ok=True)

            cmd_ps3 = [PS3_DUMPER_PATH]
            p_ps3 = subprocess.Popen(cmd_ps3, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=r"C:\Tools\ps3-disc-dumper", creationflags=NO_WINDOW_FLAG)
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
                subprocess.run(["powershell", "-Command", "(New-Object -ComObject WMPlayer.OCX.7).cdromCollection.Item(0).Eject()"], capture_output=True, creationflags=NO_WINDOW_FLAG)
            return

        if media_type == "movie":
            min_sec = max(state["settings"]["min_length_sec"], 1800)
        else:
            min_sec = state["settings"]["min_length_sec"]

        preset = state["settings"]["preset"]
        # Read media_type fresh here rather than the value captured before
        # the 10-second auto-start countdown — auto-detect may have
        # corrected it since, and this log line renders after that delay.
        add_log(f"Media Type: {state['settings']['media_type'].upper()} | Disc Type: {state['disc_type']} | Preset/Encoder: {preset}")

        fetch_media_artwork()

        # --- STEP 1: RIPPING ---
        # Each job gets its own temp folder so parallel rip+encode don't collide.
        job_raw_dir = os.path.join(TEMP_RAW_DIR, f"job_{int(time.time())}")
        os.makedirs(job_raw_dir, exist_ok=True)
        state["current_raw_dir"] = job_raw_dir

        state["stage"] = "RIPPING"
        state["status_message"] = f"Ripping raw tracks with MakeMKV ({label})..."
        state["progress_pct"] = 10
        add_log(f"=== STAGE 1/3: MAKEMKV DISC EXTRACTION STARTED ({label}) ===")

        cmd_rip = [
            MAKEMKV_PATH,
            "-r", "mkv", "disc:0", "all", job_raw_dir,
            f"--minlength={min_sec}"
        ]

        title_durations = {}  # {title_num: duration_seconds}, used for compilation/snippet filtering below

        p_rip = subprocess.Popen(cmd_rip, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, creationflags=NO_WINDOW_FLAG)
        for line in p_rip.stdout:
            line_str = line.strip()
            if "PRGV:" in line_str:
                try:
                    prg_parts = line_str.split(":")[1].split(",")
                    if len(prg_parts) >= 2:
                        cur = float(prg_parts[0])
                        tot = float(prg_parts[1])
                        if tot > 0:
                            rip_pct = 10 + int((cur / tot) * 38)
                            state["progress_pct"] = min(rip_pct, 48)
                except Exception:
                    pass
            elif "CINFO:2,0," in line_str or "CINFO:32,0," in line_str or "MSG:3000" in line_str:
                try:
                    fields = next(csv.reader(io.StringIO(line_str)))
                    for item_val in fields:
                        clean_val = item_val.strip('"\' ')
                        if len(clean_val) > 3 and clean_val.upper() not in ["VOLUME_ID", "DVD_VIDEO", "BDMV", "DISC1", "UNKNOWN", "TITLE"]:
                            if state["disc_label"] in ["VOLUME_ID", "Empty Drive / No Disc", "No Disc Detected", "DVD_VIDEO", "BDMV"]:
                                state["disc_label"] = clean_val
                                add_log(f"[MakeMKV] Extracted internal disc title header: '{clean_val}'")
                                threading.Thread(target=parse_disc_label_media, args=(clean_val,), daemon=True).start()
                            break
                except Exception:
                    pass
            elif "MSG:3028" in line_str:
                try:
                    fields = next(csv.reader(io.StringIO(line_str)))
                    # fields: [msg_id, flags, count, fmt_str, human_str, param1, param2, ...]
                    title_num = fields[5].strip() if len(fields) > 5 else "?"
                    duration = fields[7].strip() if len(fields) > 7 else "unknown"
                    add_log(f"[MakeMKV] Title #{title_num} found (Duration: {duration})")
                    dur_sec = parse_makemkv_duration_to_seconds(duration)
                    if dur_sec is not None and title_num.isdigit():
                        title_durations[int(title_num)] = dur_sec
                except Exception:
                    pass
            elif "MSG:5014" in line_str:
                try:
                    fields = next(csv.reader(io.StringIO(line_str)))
                    # fields[5] = count of titles, fields[4] = human message
                    count = fields[5].strip() if len(fields) > 5 else "?"
                    add_log(f"[MakeMKV] Saving {count} titles to temp folder...")
                except Exception:
                    pass
            elif "MSG:5005" in line_str or "MSG:5036" in line_str:
                add_log("[MakeMKV] Disc track extraction completed successfully.")

            raw_files = glob.glob(os.path.join(job_raw_dir, "*.mkv"))
            if raw_files:
                state["status_message"] = f"Ripping track {len(raw_files)} with MakeMKV ({state['progress_pct']}%)..."

        p_rip.wait()
        if p_rip.returncode != 0:
            add_log(f"[MakeMKV] Extraction failed with exit code {p_rip.returncode}")
            state["stage"] = "ERROR"
            state["status_message"] = "MakeMKV Rip Failed"
            return

        raw_files = sorted(glob.glob(os.path.join(job_raw_dir, "*.mkv")))
        if not raw_files:
            add_log("[MakeMKV] No titles met the minimum length criteria.")
            state["stage"] = "IDLE"
            state["status_message"] = "Finished - No titles found. Insert next disc."
            state["progress_pct"] = 0
            return

        # Drive-label heuristics can mislabel Blu-rays as DVD; a real DVD
        # tops out around 8.5GB (dual-layer), so anything bigger flags a
        # misdetection that would otherwise pick the wrong "Auto" preset.
        total_extracted_gb = sum(os.path.getsize(f) for f in raw_files) / (1024 ** 3)
        if state["disc_type"] == "DVD" and total_extracted_gb > 8.5:
            add_log(f"[Disc Type] {round(total_extracted_gb, 1)}GB extracted exceeds DVD capacity — correcting disc type from DVD to Blu-ray.")
            state["disc_type"] = "Blu-ray"

        # Re-read media_type fresh here rather than trusting the value
        # captured at pipeline start — MakeMKV rips can run for a long time,
        # and if settings get corrected mid-rip (e.g. auto-detect failed on
        # a generic disc label and got fixed manually), using the stale
        # value here would wrongly apply TV-only filtering to a movie disc
        # or vice versa.
        current_media_type = state["settings"]["media_type"]
        if current_media_type == "tv" and state["settings"]["auto_rename"] and len(raw_files) > 1:
            normal_files, outlier_files, insufficient_data = classify_titles_by_duration(raw_files, title_durations)
            if insufficient_data:
                add_log("Auto-Rename: Not enough title duration data captured — falling back to dropping the first (likely play-all) track.")
                target_files = raw_files[1:]
            else:
                for f, dur in outlier_files:
                    mins = round(dur / 60, 1)
                    add_log(f"[Duration Filter] Excluding '{os.path.basename(f)}' ({mins} min) — unusual length vs. this disc's other titles (likely a play-all compilation or a recap/preview snippet). Left in the raw folder for manual review via Recover.")
                target_files = normal_files
                if not target_files:
                    add_log("[Duration Filter] All titles were flagged as outliers — falling back to including everything to avoid an empty job.")
                    target_files = raw_files
        else:
            target_files = raw_files

        state["total_titles"] = len(target_files)
        add_log(f"=== STAGE 1 COMPLETE: Extracted {len(target_files)} title(s) — queuing for encode ===")

        if state["settings"]["auto_eject"]:
            add_log("Rip done — ejecting so you can insert the next disc while encoding runs...")
            try:
                subprocess.run(["powershell", "-Command", "(New-Object -ComObject WMPlayer.OCX.7).cdromCollection.Item(0).Eject()"], capture_output=True, creationflags=NO_WINDOW_FLAG)
                state["disc_present"] = False
                state["disc_label"] = "Ejected"
            except Exception as e:
                add_log(f"Auto-eject warning: {e}")

        job = {
            "raw_files": target_files,
            "raw_dir": job_raw_dir,
            "label": label,
            "disc_type": state["disc_type"],
            "settings": dict(state["settings"]),
        }
        with encode_queue_lock:
            encode_queue.append(job)
            state["queue_count"] = len(encode_queue)

        add_log(f"Disc queued for encoding. Queue depth: {state['queue_count']}. Insert next disc anytime.")
        state["stage"] = "IDLE"
        state["status_message"] = f"Rip complete — {state['queue_count']} job(s) queued for encode. Ready for next disc."
        state["progress_pct"] = 0
        state["start_timestamp"] = 0
        state["current_raw_dir"] = ""

    except Exception as e:
        add_log(f"Fatal error in rip pipeline: {e}")
        state["stage"] = "ERROR"
        state["status_message"] = f"Rip Error: {e}"
        state["start_timestamp"] = 0
    finally:
        rip_lock.release()


def run_encode_transfer_stage(job):
    raw_files = job["raw_files"]
    job_raw_dir = job["raw_dir"]
    label = job["label"]
    disc_type = job["disc_type"]
    settings = job["settings"]
    media_type = settings["media_type"]
    fmt = settings["format"]
    preset = settings["preset"]

    if HANDBRAKE_PATH is None:
        add_log("Cannot encode: HandBrakeCLI.exe was not found. Check the STARTUP WARNING in the log. Raw files left in place for later recovery.")
        if not rip_lock.locked():
            state["stage"] = "ERROR"
            state["status_message"] = "HandBrakeCLI not found — check config"
        return

    start_time = time.time()

    def set_stage(stage, msg=None):
        if not rip_lock.locked():
            state["stage"] = stage
            if msg:
                state["status_message"] = msg

    def clean_str(s):
        for c in r'/\:*?"<>|':
            s = str(s).replace(c, "")
        return s.strip()

    show_name = clean_str(settings["show_name"])
    movie_title = clean_str(settings["movie_title"])
    year = settings["release_year"]
    season = settings["season_number"]
    start_ep = settings["start_episode"]
    include_titles = settings.get("include_episode_titles", True)

    try:
        # --- STEP 2: ENCODING ---
        q = state["queue_count"]
        q_label = f" [{q} more in queue]" if q > 0 else ""
        set_stage("ENCODING", f"Encoding {len(raw_files)} title(s) from '{label}'...{q_label}")
        add_log(f"=== STAGE 2/3: HANDBRAKE ENCODING STARTED ({len(raw_files)} files from '{label}') ===")
        if not rip_lock.locked():
            state["start_timestamp"] = int(start_time)
        encoded_files = []

        for idx, raw_file in enumerate(raw_files, start=1):
            if not rip_lock.locked():
                state["current_title"] = idx

            if settings["auto_rename"]:
                if media_type == "movie":
                    if len(raw_files) == 1:
                        target_filename = f"{movie_title} ({year}).{fmt}"
                    else:
                        # Find the largest raw file (the main feature film)
                        raw_sizes = {rf: os.path.getsize(rf) if os.path.exists(rf) else 0 for rf in raw_files}
                        largest_rf = max(raw_files, key=lambda rf: raw_sizes[rf])
                        largest_size = raw_sizes[largest_rf]
                        current_size = raw_sizes[raw_file]

                        if raw_file == largest_rf:
                            target_filename = f"{movie_title} ({year}).{fmt}"
                        elif largest_size > 0 and abs(current_size - largest_size) / largest_size < 0.08:
                            add_log(f"[Movie Deduplication] Skipping '{os.path.basename(raw_file)}' — duplicate playlist cut of main feature.")
                            continue
                        else:
                            target_filename = f"Featurettes\\{movie_title} ({year}) - Featurette {idx:02d}.{fmt}"
                else:
                    ep_num = start_ep + idx - 1
                    ep_name = clean_str(fetch_episode_title(show_name, season, ep_num)) if include_titles else ""
                    if ep_name:
                        target_filename = f"{show_name} - S{season:02d}E{ep_num:02d} - {ep_name}.{fmt}"
                    else:
                        target_filename = f"{show_name} - S{season:02d}E{ep_num:02d}.{fmt}"
            else:
                target_filename = f"{clean_str(label)}_Title_{idx:02d}.{fmt}"

            target_filename = clean_str(target_filename)
            if not rip_lock.locked():
                state["current_file"] = target_filename
                state["status_message"] = f"Encoding Title {idx}/{len(raw_files)} ({target_filename}){q_label}"

            out_file = os.path.join(TEMP_ENCODED_DIR, target_filename)
            add_log(f"[HandBrake] [{idx}/{len(raw_files)}] Starting encode -> '{target_filename}'")

            cmd_enc = [HANDBRAKE_PATH, "-i", raw_file, "-o", out_file]

            # Prefer English audio track automatically over foreign/Japanese primary tracks.
            # --audio-lang-list eng,any with --first-audio scans for English audio first.
            cmd_enc.extend(["--audio-lang-list", "eng,any", "--first-audio", "--native-language", "eng", "--native-dub"])

            if "NVENC H.264" in preset or "NVENC H264" in preset:
                cmd_enc.extend(["--encoder", "nvenc_h264", "--quality", "20", "--encoder-preset", "fast"])
            elif "NVENC H.265" in preset or "NVENC HEVC" in preset:
                cmd_enc.extend(["--encoder", "nvenc_h265", "--quality", "22", "--encoder-preset", "fast"])
            elif "Intel QSV" in preset:
                cmd_enc.extend(["--encoder", "qsv_h264", "--quality", "20"])
            elif "AMD VCE" in preset:
                cmd_enc.extend(["--encoder", "vce_h264", "--quality", "20"])
            else:
                preset_arg = preset if preset != "Auto" else ("HQ 1080p30 Surround" if disc_type == "Blu-ray" else "HQ 720p30 Surround")
                cmd_enc.extend(["--preset", preset_arg])

            p_enc = subprocess.Popen(cmd_enc, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, creationflags=NO_WINDOW_FLAG)
            last_logged_quarter = -1

            for line in p_enc.stdout:
                if "Encoding: task" in line:
                    try:
                        if "%" in line:
                            sub_pct = float(line.split("%")[0].split(",")[-1].strip())
                            if not rip_lock.locked():
                                overall = 50 + int((idx - 1 + (sub_pct / 100.0)) / len(raw_files) * 40)
                                state["progress_pct"] = min(overall, 92)

                            quarter = int(sub_pct // 25)
                            if quarter > last_logged_quarter and quarter > 0 and quarter <= 4:
                                last_logged_quarter = quarter
                                add_log(f"[HandBrake] [{target_filename}] {int(sub_pct)}% | {state['fps']} FPS | ETA: {state['eta']}")

                        if "fps" in line:
                            parts = line.split(",")
                            for p in parts:
                                if "fps" in p:
                                    state["fps"] = p.strip().split()[0]
                                if "ETA" in p:
                                    eta_str = p.strip().replace("ETA ", "")
                                    state["eta"] = eta_str
                                    try:
                                        eta_parts = [int(x) for x in eta_str.split(":")]
                                        if len(eta_parts) == 3:
                                            single_sec = eta_parts[0]*3600 + eta_parts[1]*60 + eta_parts[2]
                                        elif len(eta_parts) == 2:
                                            single_sec = eta_parts[0]*60 + eta_parts[1]
                                        else:
                                            single_sec = 0
                                        rem_titles = max(0, len(raw_files) - idx)
                                        tot_sec = single_sec + (rem_titles * max(single_sec, 180))
                                        m, s = divmod(tot_sec, 60)
                                        h, m = divmod(m, 60)
                                        if not rip_lock.locked():
                                            state["overall_eta"] = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
                                    except Exception:
                                        pass
                    except Exception:
                        pass

            p_enc.wait()
            if p_enc.returncode == 0 and os.path.exists(out_file):
                size_mb = round(os.path.getsize(out_file) / (1024 * 1024), 1)
                encoded_files.append(out_file)
                add_log(f"[HandBrake] Encoded '{target_filename}' ({size_mb} MB)")
            else:
                add_log(f"[HandBrake] Encoding failed for title {idx}.")

        # --- STEP 3: TRANSFERRING TO NAS ---
        set_stage("TRANSFERRING", f"Moving {len(encoded_files)} file(s) to NAS{q_label}...")
        add_log(f"=== STAGE 3/3: TRANSFERRING {len(encoded_files)} FILE(S) TO NAS ===")
        if not rip_lock.locked():
            state["progress_pct"] = 95

        if settings["auto_rename"]:
            if media_type == "movie":
                proposed = f"{movie_title} ({year})"
                matched = find_existing_media_folder(DEFAULT_MOVIE_DESTINATION, proposed)
                if matched != proposed:
                    add_log(f"[NAS] Matched existing folder '{matched}' instead of creating '{proposed}'.")
                nas_folder = os.path.join(DEFAULT_MOVIE_DESTINATION, matched)
            else:
                proposed = f"{show_name} ({year})"
                matched = find_existing_media_folder(DEFAULT_TV_DESTINATION, proposed)
                if matched != proposed:
                    add_log(f"[NAS] Matched existing folder '{matched}' instead of creating '{proposed}'.")
                nas_folder = os.path.join(DEFAULT_TV_DESTINATION, matched, f"Season {season:02d}")
        else:
            nas_folder = os.path.join(DEFAULT_TV_DESTINATION, "Uncategorized")

        os.makedirs(nas_folder, exist_ok=True)
        add_log(f"[NAS] Destination: {nas_folder}")

        moved_names = []
        for ef in encoded_files:
            dest = os.path.join(nas_folder, os.path.basename(ef))
            size_mb = round(os.path.getsize(ef) / (1024 * 1024), 1)
            shutil.move(ef, dest)
            moved_names.append(os.path.basename(ef))
            add_log(f"[NAS] Moved '{os.path.basename(ef)}' ({size_mb} MB) -> {dest}")

        if media_type == "tv" and settings["auto_rename"]:
            new_ep = start_ep + len(encoded_files)
            state["settings"]["start_episode"] = new_ep
            add_log(f"Auto-incremented episode counter to S{season:02d}E{new_ep:02d} for next disc!")

        # Remove only the raw files this job targeted for encoding — any
        # outlier titles the duration filter left behind (for manual review
        # via Recover) must survive this cleanup, so we don't rmtree the
        # whole directory blindly.
        for rf in raw_files:
            try:
                if os.path.exists(rf):
                    os.remove(rf)
            except Exception:
                pass
        try:
            if os.path.isdir(job_raw_dir):
                remaining = os.listdir(job_raw_dir)
                if not remaining:
                    os.rmdir(job_raw_dir)
                else:
                    add_log(f"[Cleanup] Left {len(remaining)} unencoded title(s) in '{job_raw_dir}' for review (use Recover to process them).")
        except Exception:
            pass

        duration_min = round((time.time() - start_time) / 60, 1)
        set_stage("COMPLETE", f"Done! {len(encoded_files)} file(s) from '{label}' saved to NAS.")
        if not rip_lock.locked():
            state["progress_pct"] = 100
            state["fps"] = "0"
            state["eta"] = "--:--"
            state["start_timestamp"] = 0

        save_history_entry({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "disc_label": label,
            "media_type": media_type.upper(),
            "disc_type": disc_type,
            "episodes_saved": len(encoded_files),
            "destination": nas_folder,
            "duration_min": duration_min
        })

        trigger_plex_refresh()

        send_discord_notification(
            title=f"🎉 Disc Processing Complete: {label}",
            description=f"Ripped and encoded **{len(encoded_files)} file(s)** to NAS in **{duration_min} min**!",
            poster_url=state["artwork_url"],
            fields=[
                {"name": "Media", "value": f"{show_name if media_type=='tv' else movie_title} ({year})", "inline": True},
                {"name": "Disc Type", "value": disc_type, "inline": True},
                {"name": "Destination", "value": f"`{nas_folder}`", "inline": False},
                {"name": "Files", "value": "\n".join([f"• `{n}`" for n in moved_names[:5]]) + (f"\n*...and {len(moved_names)-5} more*" if len(moved_names) > 5 else ""), "inline": False}
            ]
        )

        if settings["auto_eject"] and state["disc_present"]:
            subprocess.run(["powershell", "-Command", "(New-Object -ComObject WMPlayer.OCX.7).cdromCollection.Item(0).Eject()"], capture_output=True, creationflags=NO_WINDOW_FLAG)
            state["disc_present"] = False
            state["disc_label"] = "Ejected"

    except Exception as e:
        add_log(f"Fatal error in encode/transfer: {e}")
        if not rip_lock.locked():
            state["stage"] = "ERROR"
            state["status_message"] = f"Encode Error: {e}"


def encode_worker():
    while True:
        time.sleep(0.5)
        with encode_queue_lock:
            if not encode_queue:
                continue
        if not encode_lock.acquire(blocking=False):
            continue
        with encode_queue_lock:
            if not encode_queue:
                encode_lock.release()
                continue
            job = encode_queue.pop(0)
            state["queue_count"] = len(encode_queue)
        try:
            run_encode_transfer_stage(job)
        finally:
            encode_lock.release()

threading.Thread(target=encode_worker, daemon=True).start()

cached_nas_files = []

def update_nas_files_cache():
    global cached_nas_files
    try:
        media_type = state["settings"]["media_type"]
        show_name = state["settings"]["show_name"]
        movie_title = state["settings"]["movie_title"]
        year = state["settings"]["release_year"]
        season = state["settings"]["season_number"]

        if media_type == "movie":
            matched = find_existing_media_folder(DEFAULT_MOVIE_DESTINATION, f"{movie_title} ({year})")
            target_nas_dir = os.path.join(DEFAULT_MOVIE_DESTINATION, matched)
        elif media_type == "ps3":
            target_nas_dir = DEFAULT_PS3_DESTINATION
        else:
            matched = find_existing_media_folder(DEFAULT_TV_DESTINATION, f"{show_name} ({year})")
            target_nas_dir = os.path.join(DEFAULT_TV_DESTINATION, matched, f"Season {season:02d}")

        if os.path.exists(target_nas_dir):
            files_with_time = []
            for f in os.listdir(target_nas_dir):
                fp = os.path.join(target_nas_dir, f)
                if os.path.isfile(fp):
                    files_with_time.append((f, os.path.getmtime(fp), round(os.path.getsize(fp) / (1024 * 1024), 1)))
            
            files_with_time.sort(key=lambda x: x[1], reverse=True)
            cached_nas_files = [{"name": f, "size_mb": size_mb} for f, mtime, size_mb in files_with_time[:20]]
        else:
            cached_nas_files = []
    except Exception:
        pass

# --- Background Poller ---
def drive_poller_thread():
    global LAST_NAS_CHECK
    last_raw_bytes = 0
    last_check_time = time.time()
    while True:
        time.sleep(2.5)
        now = time.time()
        if now - LAST_NAS_CHECK >= 30:
            check_nas_storage()
            update_nas_files_cache()
            LAST_NAS_CHECK = now


        is_ripping_active = is_makemkv_process_running() or state["stage"] == "RIPPING"
        if is_ripping_active:
            with state_lock:
                if state["stage"] != "RIPPING":
                    state["stage"] = "RIPPING"
                    state["disc_present"] = True

                raw_dir = state["current_raw_dir"]
                if not raw_dir or not os.path.exists(raw_dir):
                    job_dirs = glob.glob(os.path.join(TEMP_RAW_DIR, "job_*"))
                    if job_dirs:
                        raw_dir = max(job_dirs, key=os.path.getmtime)
                        state["current_raw_dir"] = raw_dir
                    else:
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
                        state["progress_pct"] = min(10 + int(total_mb / 500), 48)

        check_drive_status()
        # A stale COMPLETE/ERROR state (e.g. from an unrelated job that
        # already finished or failed) shouldn't linger in the header once
        # the drive is empty and nothing is actively running — the log/
        # history entries remain as the permanent record either way.
        if state["stage"] in ("COMPLETE", "ERROR") and not state["disc_present"] and not encode_lock.locked() and not rip_lock.locked():
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
    state_copy = dict(state)
    state_copy["logs"] = state["logs"][-50:] if state["logs"] else ["System ready - waiting for process logs..."]

    return jsonify({
        "state": state_copy,
        "nas_files": cached_nas_files,
        "history": load_history()
    })

@app.route("/api/start", methods=["POST"])
def start_pipeline():
    if rip_lock.locked() or state["stage"] in ["COUNTDOWN", "RIPPING"]:
        return jsonify({"status": "error", "message": "Rip already in progress"}), 400

    state["disc_present"] = True
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
    subprocess.run(["powershell", "-Command", "(New-Object -ComObject WMPlayer.OCX.7).cdromCollection.Item(0).Eject()"], capture_output=True, creationflags=NO_WINDOW_FLAG)
    state["disc_present"] = False
    state["disc_label"] = "Ejected"
    add_log("Drive D: ejected manually.")
    return jsonify({"status": "success", "message": "Drive ejected"})

@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.json or {}
    with state_lock:
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
            
    request_nas_cache_refresh()
    threading.Thread(target=fetch_media_artwork, daemon=True).start()
    return jsonify({"status": "success", "settings": state["settings"]})

@app.route("/api/barcode-lookup", methods=["POST"])
def barcode_lookup():
    data = request.json or {}
    upc_raw = data.get("upc", "").strip()
    if not upc_raw:
        return jsonify({"status": "error", "message": "No UPC provided"}), 400

    # Auto-pad short barcodes to 12 digits if 10 or 11 digits
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


@app.route("/api/raw-dirs", methods=["GET"])
def list_raw_dirs():
    dirs = []
    if not os.path.exists(TEMP_RAW_DIR):
        return jsonify({"status": "success", "dirs": []})

    def dir_entry(path, name):
        mkv_files = sorted(glob.glob(os.path.join(path, "*.mkv")))
        if not mkv_files:
            return None
        total_gb = round(sum(os.path.getsize(f) for f in mkv_files) / (1024 ** 3), 2)
        first = os.path.basename(mkv_files[0])
        label = first.rsplit("_t", 1)[0].replace("-", ": ").strip() if "_t" in first else name
        return {"path": path, "name": name, "file_count": len(mkv_files), "total_gb": total_gb, "label": label}

    # Files directly in TEMP_RAW_DIR (pre-queue legacy layout)
    entry = dir_entry(TEMP_RAW_DIR, "Raw (root)")
    if entry:
        dirs.append(entry)

    # Subdirectories (job_* layout from queue system)
    try:
        for d in sorted(os.listdir(TEMP_RAW_DIR)):
            full = os.path.join(TEMP_RAW_DIR, d)
            if os.path.isdir(full):
                entry = dir_entry(full, d)
                if entry:
                    dirs.append(entry)
    except Exception:
        pass

    return jsonify({"status": "success", "dirs": dirs})


@app.route("/api/reencode", methods=["POST"])
def reencode_from_raw():
    data = request.json or {}
    raw_dir = data.get("raw_dir", "").strip()

    if not raw_dir:
        return jsonify({"status": "error", "message": "No raw_dir provided"}), 400

    abs_raw = os.path.abspath(raw_dir)
    abs_temp = os.path.abspath(TEMP_RAW_DIR)
    if not (abs_raw == abs_temp or abs_raw.startswith(abs_temp + os.sep)):
        return jsonify({"status": "error", "message": "Path not inside temp directory"}), 403

    if not os.path.isdir(abs_raw):
        return jsonify({"status": "error", "message": "Directory does not exist"}), 400

    raw_files = sorted(glob.glob(os.path.join(abs_raw, "*.mkv")))
    if not raw_files:
        return jsonify({"status": "error", "message": "No MKV files found"}), 400

    media_type = state["settings"]["media_type"]
    if media_type == "tv" and state["settings"]["auto_rename"] and len(raw_files) > 1:
        normal_files, outlier_files, insufficient_data = classify_raw_dir_by_duration(raw_files)
        if insufficient_data:
            add_log("[Recovery] Not enough title duration data (ffprobe unavailable or too few titles) — falling back to dropping the first (likely play-all) track.")
            target_files = raw_files[1:]
        else:
            for f, dur in outlier_files:
                mins = round(dur / 60, 1)
                add_log(f"[Recovery][Duration Filter] Excluding '{os.path.basename(f)}' ({mins} min) — unusual length vs. this folder's other titles. Left in place for manual review.")
            target_files = normal_files
            if not target_files:
                add_log("[Recovery][Duration Filter] All titles were flagged as outliers — falling back to including everything to avoid an empty job.")
                target_files = raw_files
    else:
        target_files = raw_files

    first = os.path.basename(raw_files[0])
    label = first.rsplit("_t", 1)[0].replace("- ", ": ").strip() if "_t" in first else first

    job = {
        "raw_files": target_files,
        "raw_dir": abs_raw,
        "label": label,
        "disc_type": state["disc_type"],
        "settings": dict(state["settings"]),
    }
    with encode_queue_lock:
        encode_queue.append(job)
        state["queue_count"] = len(encode_queue)

    add_log(f"[Recovery] Queued '{label}' ({len(target_files)} titles) for encode. Queue depth: {state['queue_count']}.")
    return jsonify({"status": "success", "message": f"Queued {len(target_files)} title(s)", "queue_count": state["queue_count"]})


if __name__ == "__main__":
    # Runs headless on purpose — access the dashboard via browser at
    # http://127.0.0.1:5000. A prior version opened a native pywebview
    # window here, but that tied the entire server's lifetime to the
    # window: closing it (even accidentally) silently killed the backend,
    # including any rip/encode in progress. Serving directly on the main
    # thread with no GUI window removes that failure mode entirely.
    from waitress import serve
    serve(app, host="127.0.0.1", port=5000)
