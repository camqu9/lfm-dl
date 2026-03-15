#!/usr/bin/env python3
"""
last.fm → yt-dlp downloader
Grabs your top tracks, loved tracks, recommendations, and artist tracks
then searches YouTube and downloads the best available audio.

Requirements:
    pip install requests yt-dlp

Setup:
    1. Get a Last.fm API key at https://www.last.fm/api/account/create
    2. Fill in the CONFIG section below
    3. Run: python3 lfm_dl.py

Usage:
    python3 lfm_dl.py                    # full last.fm sync
    python3 lfm_dl.py -a <artist>       # add artist + download discography
    python3 lfm_dl.py --sanitize        # ffmpeg re-encode pass to fix broken files
    python3 lfm_dl.py -h                # help
"""

import os
import re
import sys
import time
import hashlib
import argparse
import threading
import subprocess
import requests
import yt_dlp
from concurrent.futures import ThreadPoolExecutor

# ─── CONFIG ───────────────────────────────────────────────────────────────────

LASTFM_API_KEY    = "YOUR_API_KEY_HERE"
LASTFM_API_SECRET = "YOUR_API_SECRET_HERE"
LASTFM_USERNAME   = "YOUR_USERNAME_HERE"

OUTPUT_DIR   = "/path/to/your/music/folder"
DOWNLOAD_LOG = os.path.join(OUTPUT_DIR, "downloaded.txt")

# Artists to specifically grab discographies for
SPECIFIC_ARTISTS = [
    # Add artists here whose full discographies you want downloaded
    # e.g. "Jamie Paige",
]

TOP_TRACKS_LIMIT   = 1000
LOVED_TRACKS_LIMIT = 1000
RECOMMENDED_LIMIT  = 1000

API_DELAY    = 0.25
MAX_WORKERS  = 4   # number of simultaneous downloads

# ──────────────────────────────────────────────────────────────────────────────

BASE_URL = "https://ws.audioscrobbler.com/2.0/"
log_lock = threading.Lock()


def sign_params(params):
    sig_str = "".join(
        f"{k}{v}" for k, v in sorted(params.items()) if k != "format"
    ) + LASTFM_API_SECRET
    return hashlib.md5(sig_str.encode("utf-8")).hexdigest()


def api_call(method, extra_params={}, signed=False):
    params = {
        "method":  method,
        "api_key": LASTFM_API_KEY,
        "format":  "json",
        "limit":   200,
        **extra_params,
    }
    if signed:
        params["api_sig"] = sign_params(params)
    for attempt in range(3):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=30,
                                headers={"User-Agent": "lfm_dl/1.0"})
            resp.raise_for_status()
            break
        except Exception as e:
            if attempt < 2:
                print(f"  [retry] API call failed (attempt {attempt+1}/3): {e}")
                time.sleep(5)
            else:
                raise
    time.sleep(API_DELAY)
    return resp.json()


def get_all_pages(method, result_key, inner_key, extra_params={}, limit=500):
    items = []
    page  = 1
    while len(items) < limit:
        data = api_call(method, {**extra_params, "page": page, "limit": 200})
        batch = data.get(result_key, {}).get(inner_key, [])
        if not batch:
            break
        items.extend(batch)
        total_pages = int(data.get(result_key, {}).get("@attr", {}).get("totalPages", 1))
        if page >= total_pages:
            break
        page += 1
    return items[:limit]


def get_top_tracks(username, limit):
    print(f"\n[Last.fm] Fetching top tracks for {username}...")
    tracks = get_all_pages(
        "user.getTopTracks", "toptracks", "track",
        {"user": username, "period": "overall"}, limit
    )
    return [(t["artist"]["name"], t["name"]) for t in tracks]


def get_loved_tracks(username, limit):
    print(f"[Last.fm] Fetching loved tracks for {username}...")
    tracks = get_all_pages(
        "user.getLovedTracks", "lovedtracks", "track",
        {"user": username}, limit
    )
    return [(t["artist"]["name"], t["name"]) for t in tracks]


def get_recommended_tracks(username, limit):
    print(f"[Last.fm] Fetching recommendations via top artists for {username}...")
    artists_data = get_all_pages(
        "user.getTopArtists", "topartists", "artist",
        {"user": username, "period": "6month"}, 20
    )
    tracks = []
    for artist in artists_data:
        name = artist["name"]
        similar = api_call("artist.getSimilar", {"artist": name, "limit": 5})
        similar_artists = similar.get("similarartists", {}).get("artist", [])
        for sa in similar_artists:
            top = api_call("artist.getTopTracks", {"artist": sa["name"], "limit": 5})
            for t in top.get("toptracks", {}).get("track", []):
                tracks.append((sa["name"], t["name"]))
        if len(tracks) >= limit:
            break
    return list(dict.fromkeys(tracks))[:limit]


def get_artist_albums(artist):
    print(f"[Last.fm] Fetching albums for: {artist}...")
    data = api_call("artist.getTopAlbums", {"artist": artist, "limit": 50})
    albums = data.get("topalbums", {}).get("album", [])
    return [a["name"] for a in albums if a.get("name") and a["name"] != "(null)"]


def get_album_tracks(artist, album):
    data = api_call("album.getInfo", {"artist": artist, "album": album})
    tracks = data.get("album", {}).get("tracks", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]
    return [(artist, t["name"], album) for t in tracks if t.get("name")]


def get_musicbrainz_discography(artist_name):
    MB_BASE = "https://musicbrainz.org/ws/2"
    HEADERS = {"User-Agent": "lfm_dl/1.0 ( https://github.com/you/lfm_dl )"}
    try:
        r = requests.get(f"{MB_BASE}/artist/", params={
            "query": artist_name, "limit": 1, "fmt": "json"
        }, headers=HEADERS, timeout=10)
        r.raise_for_status()
        artists = r.json().get("artists", [])
        if not artists:
            return []
        artist_id  = artists[0]["id"]
        found_name = artists[0]["name"]
        print(f"  [MusicBrainz] Found artist: {found_name}")
        time.sleep(1)

        release_groups = []
        offset = 0
        while True:
            r = requests.get(f"{MB_BASE}/release-group/", params={
                "artist": artist_id,
                "type":   "Album|Single|EP",
                "limit":  100,
                "offset": offset,
                "fmt":    "json"
            }, headers=HEADERS, timeout=10)
            r.raise_for_status()
            data  = r.json()
            batch = data.get("release-groups", [])
            release_groups.extend(batch)
            time.sleep(1)
            if len(release_groups) >= data.get("release-group-count", 0):
                break
            offset += 100

        all_tracks = []
        for rg in release_groups:
            rg_id      = rg["id"]
            album_name = rg["title"]
            r = requests.get(f"{MB_BASE}/release/", params={
                "release-group": rg_id,
                "inc":           "recordings",
                "limit":         1,
                "fmt":           "json"
            }, headers=HEADERS, timeout=10)
            r.raise_for_status()
            time.sleep(1)
            releases = r.json().get("releases", [])
            if not releases:
                continue
            for medium in releases[0].get("media", []):
                for track in medium.get("tracks", []):
                    title = track.get("title") or track.get("recording", {}).get("title")
                    if title:
                        all_tracks.append((found_name, title, album_name))
            if all_tracks:
                print(f"  [MusicBrainz album] {album_name} — {len(releases[0].get('media', [{}])[0].get('tracks', []))} tracks")

        return all_tracks
    except Exception as e:
        print(f"  [MusicBrainz err] {e}")
        return []


def get_artist_discography(artist, limit=50):
    print(f"[Last.fm] Fetching discography for: {artist}...")
    albums = get_artist_albums(artist)
    all_tracks = []
    for album in albums[:limit]:
        try:
            tracks = get_album_tracks(artist, album)
        except Exception as e:
            print(f"  [skip] {album} — {e}")
            continue
        if tracks:
            print(f"  [album] {album} — {len(tracks)} tracks")
            all_tracks.extend(tracks)

    if all_tracks:
        return all_tracks

    print(f"  [Last.fm] No album data found, trying MusicBrainz...")
    mb_tracks = get_musicbrainz_discography(artist)
    if mb_tracks:
        return mb_tracks

    print(f"  [MusicBrainz] No data found either, falling back to Last.fm top tracks...")
    return []


def get_artist_top_tracks(artist, limit=50):
    print(f"[Last.fm] Fetching top tracks for artist: {artist}...")
    data = api_call("artist.getTopTracks", {"artist": artist, "limit": limit})
    tracks = data.get("toptracks", {}).get("track", [])
    return [(artist, t["name"]) for t in tracks]


def sanitize(name):
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


def track_key(artist, title):
    return f"{artist.lower().strip()}|||{title.lower().strip()}"


def load_download_log():
    if not os.path.exists(DOWNLOAD_LOG):
        return set()
    with open(DOWNLOAD_LOG, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def mark_downloaded(artist, title):
    with open(DOWNLOAD_LOG, "a", encoding="utf-8") as f:
        f.write(track_key(artist, title) + "\n")


def ydl_opts_base(out_template, artist, title, album=None):
    album_val = album if album else "Unknown Album"
    return {
        "format":            "bestaudio/best",
        "outtmpl":           out_template,
        "noplaylist":        True,
        "quiet":             True,
        "no_warnings":       True,
        "default_search":    "auto",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "best"},
            {
                "key":            "FFmpegMetadata",
                "add_metadata":   True,
                "add_chapters":   False,
            },
        ],
        "postprocessor_args": {
            "ffmpegmetadata": [
                "-metadata", f"title={title}",
                "-metadata", f"artist={artist}",
                "-metadata", f"album={album_val}",
                "-metadata", f"album_artist={artist}",
            ]
        },
        "cookiesfrombrowser":            ("firefox", None, None, None),
        "retries":                       10,
        "fragment_retries":              10,
        "concurrent_fragment_downloads": 1,
        "writethumbnail":                False,
    }


def get_track_album(artist, title):
    """Quick lookup to get album name for a single track from Last.fm."""
    try:
        data = api_call("track.getInfo", {"artist": artist, "track": title})
        album = data.get("track", {}).get("album", {}).get("title")
        return album if album else "Unknown Album"
    except Exception:
        return "Unknown Album"


def download_track(artist, title, output_dir, downloaded_log):
    key = track_key(artist, title)
    with log_lock:
        if key in downloaded_log:
            print(f"  [skip] Already have: {artist} - {title}")
            return

    query        = f"ytsearch1:{artist} - {title}"
    album        = get_track_album(artist, title)
    out_template = os.path.join(
        output_dir,
        sanitize(artist),
        sanitize(album),
        f"{sanitize(title)}.%(ext)s"
    )

    try:
        print(f"  [dl] {artist} - {title} [{album}]")
        with yt_dlp.YoutubeDL(ydl_opts_base(out_template, artist, title, album)) as ydl:
            ydl.download([query])
        with log_lock:
            mark_downloaded(artist, title)
            downloaded_log.add(key)
    except Exception as e:
        print(f"  [err] Failed: {artist} - {title} → {e}")


def download_track_with_album(artist, title, album, output_dir, downloaded_log):
    key = track_key(artist, title)
    with log_lock:
        if key in downloaded_log:
            print(f"  [skip] Already have: {artist} - {title}")
            return

    query        = f"ytsearch1:{artist} - {title}"
    album_folder = sanitize(album) if album else "Unknown Album"
    out_template = os.path.join(
        output_dir,
        sanitize(artist),
        album_folder,
        f"{sanitize(title)}.%(ext)s"
    )

    try:
        print(f"  [dl] {artist} - {title} [{album}]")
        with yt_dlp.YoutubeDL(ydl_opts_base(out_template, artist, title, album)) as ydl:
            ydl.download([query])
        with log_lock:
            mark_downloaded(artist, title)
            downloaded_log.add(key)
    except Exception as e:
        print(f"  [err] Failed: {artist} - {title} → {e}")


def add_artist_to_script(artist):
    script_path = os.path.abspath(__file__)
    with open(script_path, "r", encoding="utf-8") as f:
        content = f.read()
    if f'"{artist}"' in content:
        print(f"[Info] '{artist}' is already in SPECIFIC_ARTISTS")
        return
    old = "]\n\n# Artists to specifically"
    new = f'    "{artist}",\n]\n\n# Artists to specifically'
    if old not in content:
        print("[err] Couldn't find SPECIFIC_ARTISTS list to update — edit manually")
        return
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(content.replace(old, new, 1))
    print(f"[OK] Added '{artist}' to SPECIFIC_ARTISTS permanently")


def validate_artist_lastfm(artist):
    try:
        data = api_call("artist.getInfo", {"artist": artist})
        return "error" not in data
    except Exception:
        return False


def dedupe(track_list):
    seen = set()
    out  = []
    for pair in track_list:
        key = (pair[0].lower(), pair[1].lower())
        if key not in seen:
            seen.add(key)
            out.append(pair)
    return out


# ─── FFMPEG SANITIZE PASS ────────────────────────────────────────────────────

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".aac", ".wav", ".webm"}

def check_file(path):
    """Use ffmpeg to probe a file. Returns True if healthy, False if broken."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )
    return result.returncode == 0 and len(result.stderr) == 0


def reencode_file(path):
    """Re-encode file in place via a temp file. Returns True on success."""
    tmp = path + ".tmp"
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", path,
         "-c:a", "copy",   # copy stream — fast, just fixes container
         tmp],
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        os.replace(tmp, path)
        return True
    else:
        # copy failed — try full re-encode to aac as last resort
        result2 = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path,
             "-c:a", "aac", "-b:a", "128k",
             tmp],
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        )
        if result2.returncode == 0:
            os.replace(tmp, path)
            return True
        else:
            if os.path.exists(tmp):
                os.remove(tmp)
            return False


def get_audio_quality(path):
    """
    Use ffprobe to get bitrate and codec for a file.
    Returns a quality score — higher is better.
    FLAC/WAV = 9999 (lossless always wins), then by bitrate.
    """
    LOSSLESS = {"flac", "wav", "alac", "pcm_s16le", "pcm_s24le", "pcm_s32le"}
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,bit_rate",
             "-of", "default=noprint_wrappers=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        codec    = ""
        bitrate  = 0
        for line in result.stdout.splitlines():
            if line.startswith("codec_name="):
                codec = line.split("=", 1)[1].strip().lower()
            elif line.startswith("bit_rate="):
                val = line.split("=", 1)[1].strip()
                if val.isdigit():
                    bitrate = int(val)
        if codec in LOSSLESS:
            return 9999999
        return bitrate
    except Exception:
        return 0


def get_file_metadata(path):
    """
    Extract title, artist, album from file tags using ffprobe.
    Returns dict with lowercase stripped strings.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format_tags=title,artist,album",
             "-of", "default=noprint_wrappers=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        tags = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                tags[k.replace("TAG:", "").strip().lower()] = v.strip().lower()
        return tags
    except Exception:
        return {}


def get_acoustid_fingerprint(path):
    """
    Run fpcalc to get AcoustID fingerprint and duration.
    Returns (duration, fingerprint) or None if fpcalc not available.
    """
    try:
        result = subprocess.run(
            ["fpcalc", "-plain", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=30
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            duration    = float(lines[0].strip())
            fingerprint = lines[1].strip()
            return (duration, fingerprint)
    except (FileNotFoundError, Exception):
        pass
    return None


def fingerprint_similarity(fp1, fp2):
    """
    Simple fingerprint similarity — count matching characters in equal-length
    prefix. Not perfect but fast and good enough for near-duplicate detection.
    """
    min_len = min(len(fp1), len(fp2))
    if min_len == 0:
        return 0.0
    matches = sum(a == b for a, b in zip(fp1[:min_len], fp2[:min_len]))
    return matches / min_len


def dedupe_library(root):
    """
    Scan the library for duplicate tracks using a 3-tier approach:
      1. AcoustID fingerprint (catches same song with different filenames)
      2. Metadata tags (title+artist match)
      3. Filename stem match (original fallback)
    Keep the highest quality file, delete the rest.
    """
    print(f"\n[Dedupe] Scanning {root} for duplicate tracks...")
    print(f"[Dedupe] Collecting files and fingerprints (this may take a while)...")

    # Check if fpcalc is available
    fpcalc_available = subprocess.run(
        ["which", "fpcalc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0
    if not fpcalc_available:
        print(f"[Dedupe] fpcalc not found — skipping AcoustID, using metadata+filename only")
        print(f"[Dedupe] Install with: paru -S chromaprint")

    # Collect all audio files across the whole library
    all_files = []
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXTENSIONS:
                continue
            all_files.append(os.path.join(dirpath, fname))

    print(f"[Dedupe] Found {len(all_files)} audio files, analyzing...")

    # Build file info list (multithreaded)
    analyze_lock    = threading.Lock()
    analyzed_count  = [0]
    file_info       = [None] * len(all_files)

    def analyze_file(idx, path):
        meta        = get_file_metadata(path)
        stem        = os.path.splitext(os.path.basename(path))[0].lower().strip()
        quality     = get_audio_quality(path)
        fingerprint = get_acoustid_fingerprint(path) if fpcalc_available else None
        file_info[idx] = {
            "path":        path,
            "stem":        stem,
            "title":       meta.get("title", stem),
            "artist":      meta.get("artist", ""),
            "album":       meta.get("album", ""),
            "quality":     quality,
            "fingerprint": fingerprint,
        }
        with analyze_lock:
            analyzed_count[0] += 1
            if analyzed_count[0] % 100 == 0:
                print(f"  [Dedupe] Analyzed {analyzed_count[0]}/{len(all_files)}...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for idx, path in enumerate(all_files):
            executor.submit(analyze_file, idx, path)

    # Group duplicates
    processed = set()
    groups    = []  # list of lists of paths that are dupes of each other

    for i, fi in enumerate(file_info):
        if fi["path"] in processed:
            continue
        group = [fi["path"]]
        processed.add(fi["path"])

        for j, fj in enumerate(file_info):
            if i == j or fj["path"] in processed:
                continue

            is_dupe = False

            # Tier 1: AcoustID fingerprint similarity > 85%
            if (fi["fingerprint"] and fj["fingerprint"] and
                    abs(fi["fingerprint"][0] - fj["fingerprint"][0]) < 10):
                sim = fingerprint_similarity(fi["fingerprint"][1], fj["fingerprint"][1])
                if sim > 0.85:
                    is_dupe = True

            # Tier 2: metadata title+artist match
            if not is_dupe and fi["title"] and fj["title"]:
                same_title  = fi["title"] == fj["title"]
                same_artist = fi["artist"] == fj["artist"] if fi["artist"] and fj["artist"] else True
                if same_title and same_artist:
                    is_dupe = True

            # Tier 3: filename stem match (only within same artist folder)
            if not is_dupe:
                fi_artist_dir = path.split(os.sep)[-3] if path.count(os.sep) >= 2 else ""
                fj_artist_dir = fj["path"].split(os.sep)[-3] if fj["path"].count(os.sep) >= 2 else ""
                if fi["stem"] == fj["stem"] and fi_artist_dir == fj_artist_dir:
                    is_dupe = True

            if is_dupe:
                group.append(fj["path"])
                processed.add(fj["path"])

        if len(group) > 1:
            groups.append(group)

    # Delete lower quality dupes
    deleted = 0
    kept    = 0
    for group in groups:
        scored = sorted([(get_audio_quality(p), p) for p in group], reverse=True)
        best_score, best_path = scored[0]
        print(f"  [dupe] keeping: {os.path.relpath(best_path, root)} (score {best_score})")
        kept += 1
        for score, path in scored[1:]:
            print(f"    → deleting: {os.path.relpath(path, root)} (score {score})")
            try:
                os.remove(path)
                deleted += 1
                album_dir = os.path.dirname(path)
                if not os.listdir(album_dir):
                    os.rmdir(album_dir)
            except Exception as e:
                print(f"    [err] Could not delete {path}: {e}")

    print(f"\n[Dedupe] Done — {kept} unique tracks kept, {deleted} duplicates deleted")



def sanitize_library(root):
    """
    Walk the library, probe every audio file with ffmpeg.
    If broken: try to re-encode. If re-encode fails: delete.
    """
    print(f"\n[Sanitize] Scanning {root} for broken audio files...")
    checked = fixed = deleted = 0

    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXTENSIONS:
                continue
            path = os.path.join(dirpath, fname)
            checked += 1

            if check_file(path):
                continue

            print(f"  [broken] {path}")
            print(f"    → attempting re-encode...", end=" ")
            if reencode_file(path):
                print("fixed ✓")
                fixed += 1
            else:
                print("failed — deleting")
                os.remove(path)
                deleted += 1

    print(f"\n[Sanitize] Done — {checked} checked, {fixed} fixed, {deleted} deleted")


# ─── BEETS ───────────────────────────────────────────────────────────────────

def run_beets():
    """Run beet import on the whole library after a sync."""
    print(f"\n[Beets] Running beet import on {OUTPUT_DIR}...")
    try:
        subprocess.run(
            ["beet", "import", "-q", "-A", OUTPUT_DIR],
            check=False
        )
        print(f"[Beets] Done.")
    except FileNotFoundError:
        print(f"[Beets] beet not found — skipping (install with: paru -S beets)")
    except Exception as e:
        print(f"[Beets] Error: {e}")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-a", "--add-artist", metavar="ARTIST", dest="add_artist")
    parser.add_argument("--sanitize", action="store_true",
                        help="Run ffmpeg sanitize pass on the whole library")
    parser.add_argument("--dedupe", action="store_true",
                        help="Scan library for duplicate tracks and delete lowest quality")
    parser.add_argument("--disco", action="store_true",
                        help="Also fetch and download full discographies for every artist found")
    parser.add_argument("-h", "--help", action="store_true")
    args = parser.parse_args()

    if args.help:
        print(
            "lfm_dl — Last.fm → yt-dlp music downloader\n"
            "\n"
            "  (no args)       full Last.fm sync — grabs top/loved/recommended tracks\n"
            "  --disco         also fetch full discographies for every artist found\n"
            "  -a <artist>     add artist to watch list + download their discography\n"
            "  --sanitize      ffmpeg probe + fix/delete broken audio files\n"
            "  --dedupe        scan for duplicate tracks, keep highest quality\n"
            "  -h              show this help\n"
            "\n"
            "examples:\n"
            "  python3 lfm_dl.py\n"
            "  python3 lfm_dl.py --disco\n"
            "  python3 lfm_dl.py -a 'Jamie Paige'\n"
            "  python3 lfm_dl.py --sanitize\n"
            "  python3 lfm_dl.py --dedupe\n"
        )
        return

    # ── Sanitize mode ────────────────────────────────────────────────────────
    if args.sanitize:
        sanitize_library(OUTPUT_DIR)
        return

    # ── Dedupe mode ──────────────────────────────────────────────────────────
    if args.dedupe:
        dedupe_library(OUTPUT_DIR)
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    downloaded_log = load_download_log()

    # ── Add artist mode ──────────────────────────────────────────────────────
    if args.add_artist:
        artist = args.add_artist

        print(f"[Last.fm] Validating artist: {artist}...")
        if not validate_artist_lastfm(artist):
            print(f"[err] '{artist}' not found on Last.fm — check spelling and try again")
            sys.exit(1)
        print(f"[OK] Artist confirmed on Last.fm")

        add_artist_to_script(artist)
        tracks = get_artist_discography(artist)
        if not tracks:
            print(f"[Last.fm] No album data found, falling back to top tracks...")
            top = get_artist_top_tracks(artist)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                for i, (a, title) in enumerate(top, 1):
                    print(f"[{i}/{len(top)}]", end=" ")
                    executor.submit(download_track, a, title, OUTPUT_DIR, downloaded_log)
        else:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                for i, (a, title, album) in enumerate(tracks, 1):
                    print(f"[{i}/{len(tracks)}]", end=" ")
                    executor.submit(download_track_with_album, a, title, album, OUTPUT_DIR, downloaded_log)
        print("\n[Done]")
        return

    # ── Full Last.fm sync mode ───────────────────────────────────────────────
    if LASTFM_API_KEY == "YOUR_API_KEY_HERE":
        print("ERROR: Fill in your Last.fm API key and shared secret in the CONFIG section!")
        print("Get them free at https://www.last.fm/api/account/create")
        return

    try:
        raw_tracks = []
        raw_tracks += get_top_tracks(LASTFM_USERNAME, TOP_TRACKS_LIMIT)
        raw_tracks += get_loved_tracks(LASTFM_USERNAME, LOVED_TRACKS_LIMIT)
        try:
            raw_tracks += get_recommended_tracks(LASTFM_USERNAME, RECOMMENDED_LIMIT)
        except Exception as e:
            print(f"[warn] Recommendations timed out, skipping: {e}")
    except Exception as e:
        print(f"[err] Failed to fetch Last.fm data: {e}")
        sys.exit(1)

    raw_tracks = dedupe(raw_tracks)

    if args.disco:
        # ── Disco mode: fetch full discography for every artist ──────────────
        all_artists = list({a for a, _ in raw_tracks} | set(SPECIFIC_ARTISTS))
        print(f"\n[Info] --disco enabled, fetching discographies for {len(all_artists)} artists...")

        disco_tracks    = []
        fallback_tracks = []
        for artist in all_artists:
            for attempt in range(3):
                try:
                    disco = get_artist_discography(artist)
                    if disco:
                        disco_tracks.extend(disco)
                    else:
                        fallback_tracks += get_artist_top_tracks(artist)
                    break
                except Exception as e:
                    if attempt < 2:
                        print(f"[warn] {artist} attempt {attempt+1}/3 failed, retrying in 5s... ({e})")
                        time.sleep(5)
                    else:
                        print(f"[warn] Skipping {artist} after 3 failed attempts")

        disco_tracks    = list({(a.lower(), t.lower()): (a, t, al) for a, t, al in disco_tracks}.values())
        fallback_tracks = dedupe(fallback_tracks)

        print(f"[Info] {len(disco_tracks)} discography tracks + {len(fallback_tracks)} fallback tracks queued")
        print(f"[Info] {len(downloaded_log)} tracks already in download log, skipping those\n")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for i, (artist, title, album) in enumerate(disco_tracks, 1):
                print(f"[disco {i}/{len(disco_tracks)}]", end=" ")
                executor.submit(download_track_with_album, artist, title, album, OUTPUT_DIR, downloaded_log)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for i, (artist, title) in enumerate(fallback_tracks, 1):
                print(f"[fallback {i}/{len(fallback_tracks)}]", end=" ")
                executor.submit(download_track, artist, title, OUTPUT_DIR, downloaded_log)

    else:
        # ── Default mode: just download the tracks directly ──────────────────
        print(f"\n[Info] {len(raw_tracks)} tracks queued (use --disco to also grab full discographies)")
        print(f"[Info] {len(downloaded_log)} tracks already in download log, skipping those\n")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for i, (artist, title) in enumerate(raw_tracks, 1):
                print(f"[{i}/{len(raw_tracks)}]", end=" ")
                executor.submit(download_track, artist, title, OUTPUT_DIR, downloaded_log)

    print("\n[Done] All finished!")
    run_beets()


if __name__ == "__main__":
    main()
