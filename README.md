# Music Library Manager

A Python script that stages downloaded music into a clean, Plex-friendly library.
Unlike a simple file mover, it reads audio tags, judges album quality as a unit,
verifies every move by hash, and is **safe by default** — nothing is ever
permanently deleted in a normal run.

```
Library/
└── Artist/
    └── Album/
        ├── 01 - Track One.flac
        ├── 02 - Track Two.flac
        └── cover.jpg
```

---

## Design principles

This script is built to be cautious with your files:

- **Nothing is deleted.** Discards go to a trash folder and stay recoverable for a
  retention window (90 days by default). You can list and restore them.
- **`--dry-run` shows every decision** before anything moves, tags, or trashes.
- **Albums are judged once, as a unit**, on quality — not per-file and not on raw
  track count.
- **Moves are hash-verified** (SHA-256) so a file corrupted mid-copy is caught and
  left in staging rather than silently replacing a good copy.
- **Books are ignored.** Audiobook and ebook files are left untouched in staging
  for other tools to handle.
- **Zip extraction is path-validated** (no Zip Slip) and skips archives containing
  books.
- **Unparsable files are quarantined**, never moved into the library.

---

## Features

- **Tag-driven organization** — reads artist/album/title/track with
  [`mutagen`](https://mutagen.readthedocs.io/), falling back to filename and
  folder structure when tags are missing.
- **Album-level quality comparison** — compares a staged album against an existing
  library copy using lossless ratio → bit depth × sample rate → average bitrate →
  track count, with configurable margins.
- **Track-level merging** (default) — fills in songs missing from an existing
  library album instead of replacing the whole thing.
- **Upgrade mode** — with `--no-merge`, replaces a lower-quality library album
  wholesale when the staged copy clearly wins.
- **Review folder** — albums too close to call are set aside for you to judge;
  nothing is deleted.
- **Extension repair** — sniffs file magic bytes and renames mislabeled files
  (e.g. a `.mp3` that's really a `.flac`).
- **Corruption check** — if `ffmpeg` is on `PATH`, files are fully decoded to catch
  truncated/garbled audio a header-only check would miss.
- **Compilation detection** — albums with many distinct artists are filed under
  "Various Artists".
- **Companion files** — artwork, `.cue`, `.log`, `.nfo`, `.txt`, `.m3u` are moved
  with the album; recognized cover art is renamed to `cover.<ext>`.
- **Safety guards** — skips in-progress downloads (partial extensions, files
  modified in the last 30s), refuses to run below a free-space threshold, and
  handles Windows reserved names and long paths.
- **Rotating logs** — capped at 5 MB with 3 backups so the log never grows without
  bound.

---

## Requirements

- Python 3.8+ (uses the walrus operator and `pathlib`)
- [`mutagen`](https://pypi.org/project/mutagen/) — audio tag reading/writing
- [`python-dotenv`](https://pypi.org/project/python-dotenv/) — `.env` loading
- **`ffmpeg`** (optional but recommended) — enables full-decode corruption checks.
  Without it, the script falls back to a header-only test.

```bash
pip install mutagen python-dotenv
```

Install FFmpeg (optional):

```bash
# Debian/Ubuntu
sudo apt install ffmpeg
# macOS (Homebrew)
brew install ffmpeg
# Windows (winget)
winget install Gyan.FFmpeg
```

---

## Configuration

Settings are read from a `.env` file in the script's directory. Every value can
also be overridden per-run with a command-line flag (see below).

```env
# Required
STAGING_FOLDER=/path/to/downloads      # where new music arrives
LIBRARY_FOLDER=/path/to/plex/music     # your organized library root

# Required — and MUST live OUTSIDE the library folder,
# or Plex will index your discards
TRASH_FOLDER=/path/to/trash
REVIEW_FOLDER=/path/to/review          # ambiguous albums land here

# Optional
LOG_DIR=/path/to/logs                  # defaults to the script's directory
TRASH_RETAIN_DAYS=90                   # auto-purge trash older than this (0 = keep forever)
MIN_FREE_GB=1                          # refuse to run below this much free space
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `STAGING_FOLDER` | Yes | — | Folder scanned for new music. |
| `LIBRARY_FOLDER` | Yes | — | Destination library root (`Artist/Album/…`). |
| `TRASH_FOLDER` | Yes | — | Where discards go. **Must be outside the library.** |
| `REVIEW_FOLDER` | Yes | — | Where ambiguous albums are set aside. |
| `LOG_DIR` | No | script directory | Where `music_organizer.log` is written. |
| `TRASH_RETAIN_DAYS` | No | `90` | Days before trashed batches are auto-purged (`0` = forever). |
| `MIN_FREE_GB` | No | `1` | Minimum free space on the library drive to allow a run. |

Unparsable files are quarantined in a `_unparsable` subfolder of staging by
default (override with `--unparsable`).

---

## Usage

```bash
# See exactly what would happen — no changes made
python MusicOrganizerScript.py --dry-run

# Do it for real
python MusicOrganizerScript.py

# See what's been discarded and when it expires
python MusicOrganizerScript.py --list-trash

# Restore a discarded batch (copies it back out; trash stays intact)
python MusicOrganizerScript.py --restore-trash 20260712_143012
```

### Command-line options

| Flag | Description |
|---|---|
| `--staging`, `--library`, `--trash`, `--review` | Override the corresponding `.env` folders. |
| `--unparsable DIR` | Where to quarantine files that can't be parsed as audio (default: `_unparsable` inside staging). |
| `--log-file PATH` | Log file path (default: alongside the script). |
| `--dry-run` | Show every decision without moving, tagging, or trashing anything. |
| `--no-upgrade` | Never replace an album already in the library. |
| `--no-merge` | Disable track-level merging; fall back to whole-album quality comparison. |
| `--quiet` | Log everything to file, but only warnings to the console. |
| `--trash-retain-days N` | Auto-purge trash older than N days (`0` = forever). |
| `--list-trash` | Show what's been discarded. |
| `--full-list` | With `--list-trash`, show every file rather than a preview. |
| `--restore-trash BATCH` | Copy a trash batch back out. |
| `--restore-to DIR` | Destination for `--restore-trash` (default: `./restored_<batch>`). |

---

## How an album is processed

For each album gathered from staging, the script decides what to do based on
what's already in the library:

1. **No existing copy** → tags are written, files are hash-verified and moved into
   `Library/Artist/Album/`.
2. **Byte-identical copy exists** → the staged files are trashed as exact
   duplicates; the library is left as-is.
3. **A copy exists and merging is on (default)** → missing tracks (by song title)
   are imported into the existing album; tracks already present are trashed as
   duplicates.
4. **A copy exists and `--no-merge` is set** → the two albums are compared as
   units:
   - **Library wins** → staged copy is trashed.
   - **Too close to call** → staged copy goes to the review folder for you to
     decide; nothing is deleted.
   - **Staged wins** → the old library album is trashed (unless `--no-upgrade`)
     and the new one moves in.

Quality comparison order: **lossless ratio → resolution (bit depth × sample rate)
→ average bitrate → track count**. A contender must beat the other by 10% on
bitrate/resolution or 20% on track count to win, so near-ties fall through to
review rather than churning your library.

---

## The trash system

Every discard is moved into a timestamped batch under `TRASH_FOLDER`
(e.g. `20260712_143012/`), preserving relative structure. Batches older than the
retention window are purged automatically at the start of the next run.

- `--list-trash` shows each batch, its age, size, and when it expires.
- `--restore-trash <batch>` copies a batch back out. It makes **copies** — the
  trash itself is left untouched, so a failed restore never loses data.

> **Important:** Keep `TRASH_FOLDER` outside `LIBRARY_FOLDER`. The script refuses
> to run if the trash is inside the library, because Plex would otherwise index
> your discarded files.

---

## Logging & exit codes

A rotating log (`music_organizer.log`, 5 MB × 3 backups) records every decision.
A run ends with a summary of tracks moved, albums upgraded/merged, duplicates
trashed, books skipped, unparsable files, and errors.

The script exits `1` if any errors occurred during the run (or if the environment
check failed), and `0` otherwise — useful for scripting and cron.

---

## Notes & caveats

- **Always start with `--dry-run`** on a new setup to confirm the folder layout
  and decisions look right.
- **Books are never touched.** Audiobook/ebook extensions (`.m4b`, `.aax`,
  `.epub`, `.pdf`, `.cbz`, etc.) are skipped and left in staging, and zip archives
  containing them are skipped entirely.
- **In-progress downloads are protected.** Files with partial extensions
  (`.part`, `.crdownload`, `.tmp`, …) or modified within the last 30 seconds are
  skipped.
- **Quality reading depends on `mutagen`;** corruption detection depends on
  `ffmpeg`. Files with no readable audio stream are quarantined, not moved.
- **Windows-aware.** Reserved names, trailing dots/spaces, and the 260-character
  path limit are all handled.

---

