# 📀 AutoRip & Encode Control Center (Portable Edition)

An automated, hands-free web-based control center for digitizing DVD and Blu-ray collections. It handles MakeMKV disc extraction, HandBrake encoding, metadata artwork fetching, auto-renaming for Plex/Jellyfin, and automatic NAS file transfers.

![AutoRip Control Center](https://images.unsplash.com/photo-1518709268805-4e9042af9f23?w=800&q=80)

---

## ✨ Features

- **📀 Smart DVD & Blu-ray Detection:** Auto-detects disc insertion and selects the optimal HandBrake encoding preset (`720p` for DVD / `1080p` for Blu-ray).
- **🎨 TMDB & TVMaze Poster Artwork:** Automatically fetches and displays official posters, summaries, and metadata for your TV shows and movies.
- **🏷️ Plex / Jellyfin Auto-Renaming:** Filters out play-all compilation tracks, formats files into standard Plex naming conventions (`Show Name - S01E01.mp4`), and auto-increments episode counters.
- **⏱️ Live Process Metrics & Timer:** Real-time digital clock (`00:14:23`), HandBrake FPS speed gauge, remaining ETA, and percentage progress bars.
- **📜 Rich Streaming Logs:** Human-readable logs detailing MakeMKV title lengths, HandBrake quarter milestones, and exact NAS file transfer sizes.
- **🔔 Audio Completion Chimes & Auto-Eject:** Synthesizes a completion chime and automatically ejects the disc tray when encoding finishes.
- **📁 Customizable Paths:** Easily set custom destination folders for TV Shows and Movies directly from the UI settings.

---

## 🚀 Quick Setup Guide

### 1. Prerequisites (Tools to Install)
Make sure the following 3 free applications are installed on your Windows PC:

1. **Python 3.10+** (Tick "Add Python to PATH" during installation)
2. **[MakeMKV](https://www.makemkv.com/download/)** (Free disc ripper software)
3. **[HandBrake CLI](https://handbrake.fr/downloads2.php)** (Free video encoder CLI)

---

### 2. How to Launch (1-Click)

1. Double-click **`Start-AutoRip.bat`**.
2. The launcher will automatically install Flask (if needed), start the server, and open your web browser to **`http://localhost:5000`**.

---

### ⚙️ How It Works

1. **Insert a Disc:** Place a DVD or Blu-ray into your optical drive.
2. **Select Title & Category:** Set the TV Show or Movie name, release year, season, and starting episode number on the **Media Setup Card**.
3. **Automatic Ripping & Encoding:** The system will automatically rip raw tracks using MakeMKV, encode them into `.mp4` format using HandBrake CLI, rename them to Plex standards, and move them to your media directory!
4. **Auto-Eject:** The disc tray automatically pops open when finished and increments the starting episode counter for your next disc!

---

## 🛠️ Configuration & Defaults

The app automatically searches for standard installation paths for MakeMKV, HandBrake CLI, and optical drive letters. If you wish to customize default paths manually, edit `config.json`:

```json
{
  "makemkv_path": "C:\\Program Files (x86)\\MakeMKV\\makemkvcon64.exe",
  "handbrake_path": "C:\\Program Files\\HandBrake\\HandBrakeCLI.exe",
  "drive_letter": "D:",
  "tv_destination": "C:\\Media\\TV Shows",
  "movie_destination": "C:\\Media\\Movies"
}
```

---

## 📱 Mobile & Network Access
You can access and monitor the control center from any phone, tablet, or secondary PC on your local Wi-Fi network by opening:
`http://<YOUR-PC-IP-ADDRESS>:5000`

Enjoy digitizing your collection! 🍿
