# lfm_dl

> ⚠️ **vibecoded** — this was written with AI assistance. it works for me but i make no promises. read it before you run it, you're an adult.

downloads your last.fm listening history as local audio files using yt-dlp. grabs your top tracks, loved tracks, recommendations, and full artist discographies. stores everything organized by artist/album.

## what it does

- pulls your top tracks, loved tracks, and recommendations from last.fm
- looks up full discographies via last.fm + MusicBrainz fallback
- searches youtube for each track and downloads the best audio
- organizes files as `OutputDir/Artist/Album/Title.ext`
- keeps a log of already-downloaded tracks so reruns skip them
- `--sanitize` mode: ffmpeg-probes your whole library and re-encodes or deletes broken files
- `--dedupe` mode: finds duplicate tracks across your library and keeps the highest quality one
- `-a <artist>`: add an artist and download their whole discography

## requirements

```
pip install requests yt-dlp
```

also needs `ffmpeg` installed and on your PATH.

optional:
- `fpcalc` (from `chromaprint`) for fingerprint-based deduplication
- `beets` for auto-tagging after a sync

## setup

1. get a free last.fm API key at https://www.last.fm/api/account/create
2. open `lfm_dl.py` and fill in the CONFIG section at the top:

```python
LASTFM_API_KEY    = "your key here"
LASTFM_API_SECRET = "your secret here"
LASTFM_USERNAME   = "your username here"
OUTPUT_DIR        = "/path/to/your/music/folder"
```

3. run it

## usage

```bash
python3 lfm_dl.py                  # sync top/loved/recommended tracks
python3 lfm_dl.py --disco          # also grab full discographies for every artist found
python3 lfm_dl.py -a "Artist Name" # add an artist + download their discography
python3 lfm_dl.py --sanitize       # ffmpeg probe + fix/delete broken files
python3 lfm_dl.py --dedupe         # find and delete duplicate tracks
python3 lfm_dl.py -h               # help
```

## notes

- downloads are parallel (4 workers by default), change `MAX_WORKERS` in config
- youtube search is automatic, it picks the first result — not always perfect
- if a track fails it just logs the error and moves on, it won't crash the whole run
- re-running is safe, already-downloaded tracks are skipped via the log file
