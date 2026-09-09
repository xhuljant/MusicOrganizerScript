#!/usr/bin/env python3
"""
Music Library Manager — stages downloads into a Plex-friendly library.

Rewritten to be safe by default:
  * Nothing is ever deleted. Discards go to a trash folder with 90-day retention.
  * Shares one staging folder with the audiobook organizer. Ebooks, audiobook
    files, and anything the shared mediakind.py classifier calls an audiobook (or
    can't confidently place) are left untouched in staging for that script.
  * --dry-run shows every decision before anything moves.
  * Albums are judged ONCE, as a unit, on quality — not per-file, and not on track count.
  * Moves are hash-verified across drives.
  * Zip extraction is path-validated (no Zip Slip) and skips archives containing books.
  * Files that can't be parsed as audio are quarantined in the staging folder,
    never moved into the library.

Usage:
    python music_manager.py --dry-run          # see what it would do
    python music_manager.py                    # do it
    python music_manager.py --list-trash       # what got discarded, and when it expires
    python music_manager.py --restore-trash 20260712_143012
"""

import argparse
import atexit
import hashlib
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import mutagen
from dotenv import load_dotenv

os.environ.setdefault("SCRIPT_DIR", os.path.dirname(os.path.abspath(__file__)))
# Load .env from beside the script (not the cwd) so scheduled runs and the shared
# organize_downloads wrapper still resolve STAGING_FOLDER etc.
load_dotenv(os.path.join(os.environ["SCRIPT_DIR"], ".env"))

# Shared music-vs-audiobook classifier (a byte-identical copy lives in the
# audiobook organizer's folder). Keep both copies in sync - see mediakind.py.
sys.path.insert(0, os.environ["SCRIPT_DIR"])
import mediakind


def resolve_path(win_path, fallback=None):
    """Translate a Windows path (authored for the real host machine) into
    the container's mounted equivalent when this runs inside the
    script-manager container - only /scripts, /data and any drives
    bind-mounted at /mnt/<letter> (see the project's .env) exist in there.
    Returns the path unchanged when running directly on Windows."""
    if not win_path:
        return fallback if fallback is not None else win_path
    if os.name != "posix":
        return win_path
    drive, sep, rest = win_path.partition(":")
    if sep and len(drive) == 1 and drive.isalpha():
        mounted = os.path.join("/mnt", drive.lower())
        if os.path.isdir(mounted):
            return os.path.join(mounted, rest.lstrip("\\/").replace("\\", "/"))
    return fallback if fallback is not None else win_path


class Logger:
    """Console + file logger for script-manager scripts.

    Each run's block is written to the TOP of the log file, with that run's
    summary lines first and its detail lines after - so opening the file (or
    the script-manager web app's log tab, which reads the head of the file)
    always shows the latest run's outcome first, with no scrolling needed.

    Every line is streamed to stdout immediately (live-visible in the web
    app while a run is in progress) and appended to a "<name>.log.session"
    sidecar file as it happens, so a hard kill / power cut doesn't lose the
    run's detail - it's recovered as plain, unordered text on next startup.
    """

    def __init__(self, log_file, run_label="Run", max_bytes=5 * 1024 * 1024):
        self.log_file = Path(log_file)
        self.session_file = self.log_file.with_suffix(self.log_file.suffix + ".session")
        self.run_label = run_label
        self.max_bytes = max_bytes
        self._fh = None
        self._lock = threading.Lock()
        self._detail = []
        self._summary = []
        self._start_time = None

    def open(self):
        self._recover_orphaned_session()
        self._start_time = datetime.now()
        self._fh = open(self.session_file, "w", encoding="utf-8")
        self._stream(f"{self.run_label} started at {self._start_time:%Y-%m-%d %H:%M:%S}")

    def log(self, message: str):
        self._emit(message, is_summary=False)

    def log_summary(self, message: str):
        self._emit(message, is_summary=True)

    def _emit(self, message: str, is_summary: bool):
        with self._lock:
            (self._summary if is_summary else self._detail).append(message)
        self._stream(message)

    def _stream(self, message: str):
        try:
            print(message)
        except (BrokenPipeError, OSError):
            pass  # stdout closed (e.g. piped into head) — keep writing to the file
        if self._fh:
            with self._lock:
                self._fh.write(message + "\n")
                self._fh.flush()

    def close(self):
        if not self._fh:
            return
        end_time = datetime.now()
        self._fh.close()
        self._fh = None

        sep = "=" * 60
        block_lines = [
            sep,
            f"{self.run_label} started at {self._start_time:%Y-%m-%d %H:%M:%S}",
            f"{self.run_label} ended at   {end_time:%Y-%m-%d %H:%M:%S}",
            sep,
        ]
        if self._summary:
            block_lines.append("")
            block_lines.extend(self._summary)
        if self._detail:
            block_lines.append("")
            block_lines.append("--- detail " + "-" * 49)
            block_lines.extend(self._detail)
        block_lines.append("")
        self._merge("\n".join(block_lines) + "\n")

        try:
            self.session_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _recover_orphaned_session(self):
        """A leftover session file means the last run was killed. Recover it
        as plain, unordered detail - we can't know where a summary would
        have gone."""
        if not (self.session_file.exists() and self.session_file.stat().st_size > 0):
            return
        print("Recovering log from a previous interrupted run...")
        try:
            session = self.session_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        if session.strip():
            sep = "=" * 60
            self._merge(f"{sep}\n(recovered after an interrupted run)\n{sep}\n{session}\n")
        try:
            self.session_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _merge(self, new_block: str):
        history = ""
        if self.log_file.exists():
            try:
                history = self.log_file.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                print(f"Warning: could not read existing log: {e}")

        if len(history) > self.max_bytes:
            history = history[:self.max_bytes].rsplit("\n", 1)[0]
            history += "\n\n[... older entries truncated ...]\n"

        try:
            with open(self.log_file, "w", encoding="utf-8") as f:
                f.write(new_block)
                f.write(history)
        except OSError as e:
            print(f"Error writing log file: {e} (session kept at {self.session_file})")


class _LoggerHandler(logging.Handler):
    """Bridges the stdlib logging module to a Logger instance, so existing
    logging.info()/.warning()/.error() call sites keep working unchanged.
    A record is treated as a summary line (floated to the top of the log
    block) only when logged with extra={"summary": True}."""

    def __init__(self, target: "Logger"):
        super().__init__()
        self._target = target

    def emit(self, record):
        message = self.format(record)
        if getattr(record, "summary", False):
            self._target.log_summary(message)
        else:
            self._target.log(message)


# CONFIG
STAGING_FOLDER = resolve_path(os.getenv("STAGING_FOLDER"))    # Download / staging folder
LIBRARY_FOLDER = resolve_path(os.getenv("LIBRARY_FOLDER"))        # Plex music library root

# Trash MUST live outside the Plex library root, or Plex will index the discards.
TRASH_FOLDER = resolve_path(os.getenv("TRASH_FOLDER"))
REVIEW_FOLDER = resolve_path(os.getenv("REVIEW_FOLDER"))     # Ambiguous albums land here for you to judge

# Files that can't be parsed as audio go here instead of being moved into the
# library. Nothing is deleted. When music shares its staging folder with the
# audiobook organizer, set UNPARSABLE_DIR to a path OUTSIDE staging so the other
# script doesn't re-scan quarantined files; otherwise it defaults to an
# "_unparsable" subfolder of staging.
UNPARSABLE_DIRNAME = "_unparsable"
UNPARSABLE_DIR = resolve_path(os.getenv("UNPARSABLE_DIR"))

LOG_DIR = resolve_path(os.getenv("LOG_DIR"), fallback=os.path.dirname(__file__))
LOG_FILE = os.path.join(LOG_DIR, "music_organizer.log")
TRASH_RETAIN_DAYS = os.getenv("TRASH_RETAIN_DAYS", 90)             # Auto-purge trashed items older than this (0 = keep forever)
MIN_FREE_GB = os.getenv("MIN_FREE_GB",1)            # Refuse to run below this much free space on the library drive


# Directory containing this script. The log file defaults to living right here —
# i.e. in the parent directory of the Python file.
SCRIPT_DIR = Path(__file__).resolve().parent

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".wma", ".aac", ".aiff"}
BOOK_EXTENSIONS = {
    ".m4b", ".aax", ".aa", ".epub", ".mobi", ".azw3", ".azw",
    ".pdf", ".cbz", ".cbr", ".djvu", ".fb2", ".lit", ".ibooks"
}

ARTWORK_NAMES = {"cover", "folder", "front", "album", "albumart", "artwork"}
ARTWORK_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
# Removed .pdf so standalone pdf files are not treated as album companion files
COMPANION_EXTENSIONS = ARTWORK_EXTENSIONS | {".cue", ".log", ".nfo", ".txt", ".m3u"}

# Files a downloader is still writing. Never touch these.
PARTIAL_EXTENSIONS = {".part", ".crdownload", ".!ut", ".tmp", ".partial", ".downloading"}
MIN_FILE_AGE_SECONDS = 30       # Also skip anything modified in the last N seconds

CHUNK_SIZE = 1024 * 1024
TRASH_LIST_LIMIT = 15
QUALITY_MARGIN = 1.10           # A format must beat the other by 10% to win on bitrate
TRACK_COUNT_MARGIN = 1.20       # ...and by 20% on track count (tiebreaker only)
MAX_LOG_BYTES = 5 * 1024 * 1024

# Real corruption check. mutagen only reads a file's header; ffmpeg decodes every
# sample and so catches truncated/garbled audio that the header hides. Auto-detected
# on PATH — if it isn't installed, plays_ok() returns None and we fall back to the
# old header-only behaviour.
FFMPEG = shutil.which("ffmpeg")
FFMPEG_TIMEOUT = 30             # Per-file decode timeout, seconds

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

stats = defaultdict(int)
log = logging.getLogger("music_manager")


# ============================================================================
# HELPERS
# ============================================================================
def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def long_path(p: Path) -> str:
    """Windows caps paths at 260 chars unless you use the \\\\?\\ prefix."""
    s = str(p)
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + os.path.abspath(s)
    return s


def file_hash(path: Path) -> Optional[str]:
    sha = hashlib.sha256()
    try:
        with open(long_path(path), "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                sha.update(chunk)
        return sha.hexdigest()
    except OSError as e:
        log.warning(f"Could not hash {path.name}: {e}")
        return None


shared_logger: Optional[Logger] = None


def setup_logging(log_file: Path, quiet: bool):
    global shared_logger
    log.setLevel(logging.INFO)
    log.handlers.clear()

    shared_logger = Logger(log_file, run_label="Music organizer", max_bytes=MAX_LOG_BYTES)
    shared_logger.open()
    atexit.register(shared_logger.close)

    handler = _LoggerHandler(shared_logger)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(logging.WARNING if quiet else logging.INFO)
    log.addHandler(handler)


def sanitize_name(name: str, limit: int = 100) -> str:
    """Make a string safe as a Windows/Plex folder or file name."""
    if not name:
        return "Unknown"
    name = name.replace("/", " - ").replace("\\", " - ")
    name = re.sub(r'[<>:"|?*]', "", name)
    name = "".join(c for c in name if ord(c) >= 32)   # strip control chars
    name = " ".join(name.split())
    name = name[:limit].strip()
    # Windows silently drops trailing dots and spaces, which breaks path round-trips.
    name = name.rstrip(". ")
    if name.upper().split(".")[0] in WINDOWS_RESERVED:
        name = f"_{name}"
    return name or "Unknown"


def strip_featuring(artist: str) -> str:
    """Drop 'feat.' clauses only."""
    if not artist:
        return "Unknown Artist"
    pattern = r"\s+(?:feat\.?|ft\.?|featuring|with)\s+.*$"
    return re.sub(pattern, "", artist, flags=re.IGNORECASE).strip() or artist.strip()


def normalize_album_name(name: str) -> str:
    """Key for matching the same album across folders — but NOT across editions."""
    if not name:
        return ""
    name = re.sub(r"\s*[\(\[]?(19|20)\d{2}[\)\]]?\s*$", "", name)
    name = name.replace("_", " ")
    return " ".join(name.lower().split())


# ============================================================================
# TRASH
# ============================================================================
class Trash:
    """Everything this script discards lands here, recoverable, for N days."""

    def __init__(self, root: Path, retain_days: int, dry_run: bool = False):
        self.root = root
        self.retain_days = retain_days
        self.dry_run = dry_run
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    @property
    def batch(self) -> Path:
        return self.root / self.stamp

    def discard(self, path: Path, reason: str, origin: Optional[Path] = None) -> bool:
        """Move a file or folder into this run's trash batch."""
        rel = path.name if origin is None else str(path.relative_to(origin))
        target = self.batch / rel
        log.info(f"    -> trash ({reason}): {rel}")
        if self.dry_run:
            return True
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target = target.with_name(f"{target.stem}_{datetime.now():%H%M%S%f}{target.suffix}")
            shutil.move(long_path(path), long_path(target))
            stats["trashed"] += 1
            return True
        except OSError as e:
            log.error(f"    x Could not trash {path.name}: {e}")
            stats["errors"] += 1
            return False

    def batches(self):
        if not self.root.exists():
            return
        found = []
        for b in self.root.iterdir():
            if not b.is_dir():
                continue
            try:
                when = datetime.strptime(b.name, "%Y%m%d_%H%M%S")
            except ValueError:
                when = datetime.fromtimestamp(b.stat().st_mtime)
            files = sorted(f for f in b.rglob("*") if f.is_file())
            found.append((b, when, files, sum(f.stat().st_size for f in files)))
        yield from sorted(found, key=lambda x: x[1], reverse=True)

    def prune(self):
        if self.retain_days <= 0 or not self.root.exists():
            return
        cutoff = datetime.now() - timedelta(days=self.retain_days)
        for b, when, files, size in self.batches():
            if when >= cutoff:
                continue
            age = (datetime.now() - when).days
            if self.dry_run:
                log.info(f"Would purge trash {b.name} ({len(files)} file(s), {age} days old)")
                continue
            try:
                shutil.rmtree(b)
                log.info(f"Purged trash from {b.name} ({len(files)} file(s), "
                         f"{human_size(size)}, {age} days old, past the "
                         f"{self.retain_days}-day limit)")
            except OSError as e:
                log.error(f"Could not purge trash {b.name}: {e}")

    def show(self, detailed: bool = True, limit: int = TRASH_LIST_LIMIT) -> int:
        batches = list(self.batches())
        if not batches:
            print(f"Trash is empty ({self.root}).")
            return 0
        total = sum(len(f) for _, _, f, _ in batches)
        size = sum(s for _, _, _, s in batches)
        print(f"Trash folder: {self.root}")
        print(f"{total} item(s), {human_size(size)}, in {len(batches)} batch(es)")
        print(f"Batches are purged after {self.retain_days} days."
              if self.retain_days > 0 else "Auto-purge is off.")
        print("=" * 60)
        for b, when, files, size in batches:
            age = (datetime.now() - when).days
            if self.retain_days > 0:
                left = self.retain_days - age
                fate = "purged on the next run" if left <= 0 else f"purged in {left} day(s)"
            else:
                fate = "kept indefinitely"
            print(f"\n{b.name}  —  {when:%Y-%m-%d %H:%M} ({age} day(s) ago)")
            print(f"  {len(files)} file(s), {human_size(size)} — {fate}")
            if detailed:
                shown = files if limit == 0 else files[:limit]
                for f in shown:
                    print(f"    {f.relative_to(b)}  ({human_size(f.stat().st_size)})")
                if len(files) > len(shown):
                    print(f"    ... and {len(files) - len(shown)} more (--full-list for all)")
        print("\n" + "=" * 60)
        print("To get items back:  --restore-trash <batch-name>")
        return 0

    def restore(self, batch_name: str, restore_to: Optional[str]) -> int:
        batch = self.root / batch_name
        if not batch.is_dir():
            print(f"No such trash batch: {batch_name}\nRun --list-trash to see what's there.")
            return 1
        target = Path(restore_to).resolve() if restore_to else Path.cwd() / f"restored_{batch_name}"
        files = [f for f in batch.rglob("*") if f.is_file()]
        print(f"Restoring {len(files)} item(s) to {target}")
        ok = bad = 0
        for f in sorted(files):
            dst = target / f.relative_to(batch)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(long_path(f), long_path(dst))
                print(f"  {f.relative_to(batch)}")
                ok += 1
            except OSError as e:
                print(f"  x {f.relative_to(batch)}: {e}")
                bad += 1
        print(f"\nRestored {ok}, errors {bad}. Files are in: {target}")
        print("These are copies — the trash is untouched.")
        return 0 if bad == 0 else 1


# ============================================================================
# QUALITY
# ============================================================================
class Quality:
    """Everything we know about one audio file's fidelity."""

    __slots__ = ("path", "ext", "bitrate", "sample_rate", "bits", "channels",
                 "size", "lossless", "codec")

    def __init__(self, path, ext, bitrate, sample_rate, bits, channels, size, lossless, codec):
        self.path, self.ext = path, ext
        self.bitrate, self.sample_rate, self.bits = bitrate, sample_rate, bits
        self.channels, self.size, self.lossless, self.codec = channels, size, lossless, codec

    def __str__(self):
        kind = "Lossless" if self.lossless else "Lossy"
        br = f"{self.bitrate // 1000}kbps" if self.bitrate else "unknown bitrate"
        depth = f"/{self.bits}bit" if self.bits else ""
        return f"{self.ext.upper().lstrip('.')} ({kind}) | {br} | {self.sample_rate}Hz{depth}"


_quality_cache: Dict[Path, Optional[Quality]] = {}


def read_quality(path: Path) -> Optional[Quality]:
    if path in _quality_cache:
        return _quality_cache[path]
    _quality_cache[path] = q = _read_quality_uncached(path)
    return q


def _read_quality_uncached(path: Path) -> Optional[Quality]:
    try:
        audio = mutagen.File(long_path(path))
        if audio is None or not hasattr(audio, "info"):
            return None
        info = audio.info
        ext = path.suffix.lower()
        codec = str(getattr(info, "codec", "") or "").lower()
        bits = getattr(info, "bits_per_sample", 0) or 0

        if ext in (".flac", ".wav", ".aiff"):
            lossless = True
        elif ext == ".m4a":
            lossless = codec.startswith("alac") or bits > 0
        else:
            lossless = False

        return Quality(
            path=path, ext=ext,
            bitrate=getattr(info, "bitrate", 0) or 0,
            sample_rate=getattr(info, "sample_rate", 0) or 0,
            bits=bits,
            channels=getattr(info, "channels", 0) or 0,
            size=path.stat().st_size,
            lossless=lossless,
            codec=codec,
        )
    except Exception as e:
        log.warning(f"Could not read quality of {path.name}: {e}")
        return None


def plays_ok(path: Path) -> Optional[bool]:
    """Prove the audio actually decodes, not just that the header parses."""
    if FFMPEG is None:
        return None
    try:
        proc = subprocess.run(
            [FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=FFMPEG_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning(f"  Could not run ffmpeg on {path.name}: {e}")
        return None
    if proc.returncode == 0 and not proc.stderr.strip():
        return True
    detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    if detail:
        log.info(f"  ffmpeg flagged {path.name}: {detail[-1]}")
    return False


class AlbumQuality:
    """Aggregate fidelity of a set of tracks, for album-level comparison."""

    def __init__(self, files: List[Path]):
        qs = [q for q in (read_quality(f) for f in files) if q]
        self.tracks = len(files)
        self.readable = len(qs)
        self.lossless_ratio = (sum(1 for q in qs if q.lossless) / len(qs)) if qs else 0.0
        lossy = [q.bitrate for q in qs if not q.lossless and q.bitrate]
        self.avg_bitrate = (sum(lossy) / len(lossy)) if lossy else 0
        self.max_depth = max((q.bits * q.sample_rate for q in qs), default=0)
        self.total_size = sum(q.size for q in qs)
        self.sample = qs[0] if qs else None

    def __str__(self):
        if not self.readable:
            return f"{self.tracks} track(s), unreadable"
        kind = ("lossless" if self.lossless_ratio == 1
                else "mixed" if self.lossless_ratio else "lossy")
        br = f", ~{int(self.avg_bitrate // 1000)}kbps" if self.avg_bitrate else ""
        return f"{self.tracks} track(s), {kind}{br}, {human_size(self.total_size)}"


def compare_albums(new: AlbumQuality, old: AlbumQuality) -> Tuple[str, str]:
    if not new.readable or not old.readable:
        return "uncertain", "one side's files could not be read"

    if new.lossless_ratio > old.lossless_ratio:
        return "new", f"more lossless ({new.lossless_ratio:.0%} vs {old.lossless_ratio:.0%})"
    if old.lossless_ratio > new.lossless_ratio:
        return "existing", f"library is more lossless ({old.lossless_ratio:.0%} vs {new.lossless_ratio:.0%})"

    if new.lossless_ratio == 1.0:
        if new.max_depth > old.max_depth * QUALITY_MARGIN:
            return "new", "higher resolution lossless"
        if old.max_depth > new.max_depth * QUALITY_MARGIN:
            return "existing", "library is higher resolution lossless"

    elif new.avg_bitrate and old.avg_bitrate:
        if new.avg_bitrate > old.avg_bitrate * QUALITY_MARGIN:
            return "new", (f"higher bitrate ({int(new.avg_bitrate//1000)} vs "
                           f"{int(old.avg_bitrate//1000)}kbps)")
        if old.avg_bitrate > new.avg_bitrate * QUALITY_MARGIN:
            return "existing", (f"library has higher bitrate ({int(old.avg_bitrate//1000)} vs "
                                f"{int(new.avg_bitrate//1000)}kbps)")

    if new.tracks >= old.tracks * TRACK_COUNT_MARGIN:
        return "new", f"same fidelity, more tracks ({new.tracks} vs {old.tracks})"
    if old.tracks >= new.tracks * TRACK_COUNT_MARGIN:
        return "existing", f"same fidelity, library has more tracks ({old.tracks} vs {new.tracks})"

    return "uncertain", "the two copies look equivalent"


# ============================================================================
# METADATA
# ============================================================================
def sniff_format(path: Path) -> Optional[str]:
    try:
        with open(long_path(path), "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if len(head) < 12:
        return None

    if head[:4] == b"fLaC":
        return ".flac"
    if head[:4] == b"OggS":
        return ".ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return ".aiff"
    if head[4:8] == b"ftyp":
        return ".m4a"
    if head[:16] == b"0&\xb2u\x8ef\xcf\x11\xa6\xd9\x00\xaa\x00b\xcel":
        return ".wma"
    if head[:3] == b"ID3" or (head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return ".mp3"
    return None


def fix_extension(path: Path, dry_run: bool) -> Path:
    try:
        actual = sniff_format(path)
        if actual == ".ogg":
            try:
                with open(long_path(path), "rb") as f:
                    if b"OpusHead" in f.read(1024):
                        actual = ".opus"
            except OSError:
                pass
        if not actual or actual == path.suffix.lower():
            return path
        if actual == ".m4a" and path.suffix.lower() in (".m4a", ".m4b", ".mp4", ".aac"):
            return path

        new_path = path.with_suffix(actual)
        n = 1
        while new_path.exists():
            new_path = path.with_name(f"{path.stem}_{n}{actual}")
            n += 1
        log.info(f"  Extension mismatch: {path.name} is really "
                 f"{actual.upper().lstrip('.')} — renaming to {new_path.name}")
        if dry_run:
            return path
        path.rename(new_path)
        stats["extensions_fixed"] += 1
        return new_path
    except OSError as e:
        log.warning(f"  Could not check format of {path.name}: {e}")
    return path


def parse_from_filename(path: Path, staging: Path) -> Dict[str, Optional[str]]:
    meta = {"artist": None, "album": None, "title": None, "track": None}
    stem = path.stem

    m = re.match(r"^\s*(\d{1,3})\s*[-. _]+\s*(.+)$", stem)
    if m:
        meta["track"] = m.group(1).lstrip("0") or "0"
        stem = m.group(2)

    parts = [p.strip() for p in stem.split(" - ") if p.strip()]
    if len(parts) >= 3:
        meta["artist"], meta["album"], meta["title"] = parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        meta["artist"], meta["title"] = parts[0], parts[1]
    else:
        meta["title"] = parts[0] if parts else stem

    try:
        rel_parents = path.parent.resolve().relative_to(staging.resolve()).parts
    except ValueError:
        rel_parents = ()
    if not meta["album"] and len(rel_parents) >= 1:
        meta["album"] = rel_parents[-1]
    if not meta["artist"] and len(rel_parents) >= 2:
        meta["artist"] = rel_parents[-2]
    return meta


ASF_KEYS = {
    "artist": ("Author", "WM/AlbumArtist"),
    "albumartist": ("WM/AlbumArtist",),
    "album": ("WM/AlbumTitle",),
    "title": ("Title",),
    "track": ("WM/TrackNumber",),
}


def read_tags(path: Path, staging: Path) -> Dict[str, Optional[str]]:
    tags = {"artist": None, "albumartist": None, "album": None, "title": None, "track": None}
    try:
        audio = mutagen.File(long_path(path), easy=True)
        if audio:
            def first(key):
                v = audio.get(key) or []
                return str(v[0]).strip() if v and str(v[0]).strip() else None
            tags["artist"] = first("artist")
            tags["albumartist"] = first("albumartist")
            tags["album"] = first("album")
            tags["title"] = first("title")
            track = first("tracknumber")
            if track:
                tags["track"] = track.split("/")[0].strip()

            if type(audio).__name__ == "ASF":
                for field, keys in ASF_KEYS.items():
                    if tags[field]:
                        continue
                    for k in keys:
                        v = audio.get(k) or []
                        if v and str(v[0]).strip():
                            tags[field] = str(v[0]).strip().split("/")[0]
                            break
    except Exception as e:
        log.warning(f"  Could not read tags from {path.name}: {e}")

    if not all([tags["artist"], tags["album"], tags["title"]]):
        guess = parse_from_filename(path, staging)
        for k in ("artist", "album", "title", "track"):
            tags[k] = tags[k] or guess.get(k)

    tags["artist"] = tags["artist"] or "Unknown Artist"
    tags["album"] = tags["album"] or "Unknown Album"
    tags["title"] = tags["title"] or path.stem
    return tags


def write_tags(path: Path, tags: Dict[str, Optional[str]], dry_run: bool) -> bool:
    if dry_run:
        return False
    try:
        audio = mutagen.File(long_path(path), easy=True)
        if audio is None:
            return False
        if audio.tags is None:
            audio.add_tags()

        changed = False
        for key in ("artist", "albumartist", "album", "title"):
            if tags.get(key) and not audio.get(key):
                audio[key] = tags[key]
                changed = True
        if tags.get("track") and not audio.get("tracknumber"):
            audio["tracknumber"] = str(tags["track"])
            changed = True

        if changed:
            audio.save()
            stats["metadata_written"] += 1
        return changed
    except Exception as e:
        log.warning(f"  Could not write tags to {path.name}: {e}")
        return False


# ============================================================================
# ZIP EXTRACTION
# ============================================================================
def safe_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    dest = dest.resolve()
    count = 0
    for member in zf.infolist():
        if member.is_dir():
            continue
        target = (dest / member.filename).resolve()
        if not str(target).startswith(str(dest) + os.sep) and target != dest:
            log.error(f"  x Refusing unsafe path in archive: {member.filename}")
            stats["errors"] += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(long_path(target), "wb") as out:
            shutil.copyfileobj(src, out)
        count += 1
    return count


def extract_archives(staging: Path, trash: Trash, dry_run: bool) -> int:
    zips = [z for z in staging.rglob("*.zip")
            if trash.root not in z.parents
            and not any(part.lower() in mediakind.CONTROL_DIR_NAMES for part in z.parts)]
    if not zips:
        return 0
    log.info(f"=== Checking {len(zips)} archive(s) ===")
    done = 0
    for z in zips:
        try:
            with zipfile.ZipFile(z) as zf:
                members = [m.filename.lower() for m in zf.infolist() if not m.is_dir()]
                has_books = any(Path(m).suffix in BOOK_EXTENSIONS for m in members)
                has_audio = any(Path(m).suffix in AUDIO_EXTENSIONS for m in members)

                if has_books or not has_audio:
                    log.info(f"  Skipping archive (contains audiobook/ebook or no music): {z.name}")
                    stats["books_skipped"] += 1
                    continue

            dest = z.parent / sanitize_name(z.stem)
            log.info(f"  Unpacking {z.name} -> {dest.name}/")
            if dry_run:
                done += 1
                continue

            dest.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(z) as zf:
                n = safe_extract(zf, dest)
            log.info(f"  Extracted {n} file(s)")
            trash.discard(z, "archive unpacked")
            done += 1
            stats["archives_extracted"] += 1
        except (zipfile.BadZipFile, OSError) as e:
            log.error(f"  x Failed to inspect/unpack {z.name}: {e}")
            stats["errors"] += 1
    return done


# ============================================================================
# MOVING
# ============================================================================
def move_verified(src: Path, dst: Path, dry_run: bool) -> bool:
    if dry_run:
        return True
    part = dst.with_suffix(dst.suffix + ".part")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256()
        with open(long_path(src), "rb") as fin, open(long_path(part), "wb") as fout:
            while chunk := fin.read(CHUNK_SIZE):
                sha.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        shutil.copystat(long_path(src), long_path(part))

        if file_hash(part) != sha.hexdigest():
            log.error(f"    x Verification failed for {src.name}, leaving it in staging")
            part.unlink(missing_ok=True)
            stats["errors"] += 1
            return False

        os.replace(long_path(part), long_path(dst))
        src.unlink()
        return True
    except OSError as e:
        log.error(f"    x Failed to move {src.name}: {e}")
        part.unlink(missing_ok=True)
        stats["errors"] += 1
        return False


def quarantine_unparsable(path: Path, unparsable_dir: Path, dry_run: bool) -> None:
    log.info(f"  Unparsable (no readable audio stream): {path.name} "
             f"-> {unparsable_dir.name}/")
    stats["unparsable"] += 1
    if dry_run:
        return
    try:
        unparsable_dir.mkdir(parents=True, exist_ok=True)
        target = unparsable_dir / path.name
        if target.exists():
            target = target.with_name(
                f"{target.stem}_{datetime.now():%H%M%S%f}{target.suffix}")
        shutil.move(long_path(path), long_path(target))
    except OSError as e:
        log.error(f"    x Could not quarantine {path.name}: {e}")
        stats["errors"] += 1


# ============================================================================
# ALBUM PIPELINE
# ============================================================================
class Album:
    def __init__(self, artist: str, album: str):
        self.artist = artist
        self.album = album
        self.tracks: List[Tuple[Path, Dict]] = []
        self.companions: List[Path] = []

    @property
    def files(self) -> List[Path]:
        return [p for p, _ in self.tracks]

    @property
    def key(self) -> str:
        return f"{self.artist} / {self.album}"


def find_library_album(library: Path, artist: str, album: str) -> Optional[Path]:
    artist_dir = library / artist
    if not artist_dir.is_dir():
        return None
    target = normalize_album_name(album)
    for sub in artist_dir.iterdir():
        if sub.is_dir() and normalize_album_name(sub.name) == target:
            if any(f.suffix.lower() in AUDIO_EXTENSIONS for f in sub.rglob("*")):
                return sub
    return None


def library_tracks(folder: Path) -> List[Path]:
    return [f for f in folder.rglob("*") if f.suffix.lower() in AUDIO_EXTENSIONS]


def is_ready(path: Path) -> bool:
    if path.suffix.lower() in PARTIAL_EXTENSIONS:
        return False
    try:
        age = datetime.now().timestamp() - path.stat().st_mtime
    except OSError:
        return False
    if age < MIN_FILE_AGE_SECONDS:
        log.info(f"  Skipping (still being written): {path.name}")
        stats["skipped_incomplete"] += 1
        return False
    return True


def collect_albums(staging: Path, exclude: List[Path], unparsable_dir: Path,
                   dry_run: bool) -> List[Album]:
    audio: List[Path] = []
    for f in staging.rglob("*"):
        if not f.is_file() or any(x in f.parents for x in exclude):
            continue
        if any(part.lower() in mediakind.CONTROL_DIR_NAMES for part in f.parts):
            continue  # the audiobook organizer's trash/work folders, if inside staging
        ext = f.suffix.lower()
        if ext in BOOK_EXTENSIONS:
            stats["books_skipped"] += 1
            continue
        if ext in AUDIO_EXTENSIONS and is_ready(f):
            audio.append(f)

    if not audio:
        return []

    log.info(f"Reading {len(audio)} audio file(s)...")

    groups: Dict[tuple, Album] = {}
    pending: Dict[tuple, List[Tuple[Path, Dict]]] = defaultdict(list)

    for path in sorted(audio):
        path = fix_extension(path, dry_run)
        tags = read_tags(path, staging)

        has_meta = (tags["artist"] and tags["artist"] != "Unknown Artist"
                    and tags["album"] and tags["album"] != "Unknown Album")

        if not has_meta:
            verdict = plays_ok(path)
            if verdict is False:
                quarantine_unparsable(path, unparsable_dir, dry_run)
                continue
            if verdict is None and read_quality(path) is None:
                quarantine_unparsable(path, unparsable_dir, dry_run)
                continue
            tags["_inherit"] = True

        parent = path.parent.resolve()
        if parent == staging.resolve():
            album_norm = normalize_album_name(tags["album"])
            key = (str(parent), album_norm, sanitize_name(strip_featuring(tags["artist"])))
        else:
            key = (str(parent),)
        pending[key].append((path, tags))

    for key, tracks in pending.items():
        tag_list = [t for _, t in tracks]

        real_albums = [t["album"] for t in tag_list
                       if t["album"] and t["album"] != "Unknown Album"]
        album_name = real_albums[0] if real_albums else "Unknown Album"

        explicit = {t["albumartist"] for t in tag_list if t["albumartist"]}
        artists = {strip_featuring(t["artist"]) for t in tag_list
                   if t["artist"] and t["artist"] != "Unknown Artist"}

        if len(explicit) == 1:
            album_artist = explicit.pop()
        elif len(artists) == 1:
            album_artist = artists.pop()
        elif len(artists) > 2:
            album_artist = "Various Artists"
            log.info(f"  {len(artists)} distinct artists in one album — "
                     f"filing as Various Artists (compilation)")
        elif artists:
            album_artist = sorted(artists)[0]
        else:
            album_artist = "Unknown Artist"

        alb = Album(sanitize_name(album_artist), sanitize_name(album_name))
        for path, t in tracks:
            t["albumartist"] = album_artist
            if t.pop("_inherit", False):
                t["album"] = album_name
                t["artist"] = album_artist
            alb.tracks.append((path, t))
        groups[key] = alb

    # Music shares this staging folder with the audiobook organizer. Ask the
    # shared classifier who owns each album - keep only "music"; leave audiobooks
    # and anything ambiguous in staging, untouched.
    music_groups: Dict[tuple, Album] = {}
    for key, alb in groups.items():
        verdict, reasons = mediakind.classify(alb.files, staging)
        if verdict == "audiobook":
            log.info(f"  Audiobook, not music - left in staging for the audiobook "
                     f"organizer: {alb.key} [{'; '.join(reasons)}]")
            stats["books_skipped"] += len(alb.files)
            continue
        if verdict == "ambiguous":
            log.warning(f"  AMBIGUOUS - left in staging for you to sort: {alb.key} "
                        f"[{'; '.join(reasons)}]")
            stats["ambiguous"] += len(alb.files)
            continue
        music_groups[key] = alb
    groups = music_groups

    for alb in groups.values():
        for folder in {p.parent for p in alb.files}:
            if folder == staging:
                continue
            for f in folder.iterdir():
                if f.is_file() and f.suffix.lower() in COMPANION_EXTENSIONS:
                    alb.companions.append(f)

    return list(groups.values())


def normalize_title(name: Optional[str]) -> str:
    if not name:
        return ""
    name = re.sub(r"[^\w\s]", "", name, flags=re.UNICODE)
    return " ".join(name.lower().split())


def merge_album(alb: Album, existing: Path, old_files: List[Path], library: Path,
                trash: Trash, dry_run: bool) -> None:
    have = {normalize_title(read_tags(f, library).get("title")) for f in old_files}
    have.discard("")

    imported = skipped = 0
    for path, tags in alb.tracks:
        title = normalize_title(tags.get("title"))
        if title and title in have:
            trash.discard(path, "same song title already in the library album",
                          origin=path.parent)
            skipped += 1
            stats["duplicates"] += 1
            continue

        write_tags(path, tags, dry_run)
        target = existing / sanitize_name(path.name, limit=150)
        n = 1
        while target.exists():
            target = existing / sanitize_name(f"{path.stem}_{n}{path.suffix}", limit=150)
            n += 1
        log.info(f"    + {tags.get('title') or path.name} -> {existing.name}/{target.name}")
        if move_verified(path, target, dry_run):
            imported += 1
            stats["files_moved"] += 1
            stats["tracks_imported"] += 1
            if title:
                have.add(title)

    for c in alb.companions:
        is_art = (c.suffix.lower() in ARTWORK_EXTENSIONS and c.stem.lower() in ARTWORK_NAMES)
        name = f"cover{c.suffix.lower()}" if is_art else c.name
        target = existing / sanitize_name(name, limit=150)
        if target.exists():
            continue
        if move_verified(c, target, dry_run):
            stats["companions_moved"] += 1

    log.info(f"  Merged by name: {imported} track(s) added, {skipped} already present")
    stats["albums_merged"] += 1


def process_album(alb: Album, library: Path, review: Path, trash: Trash,
                  dry_run: bool, upgrade: bool, merge: bool = True) -> None:
    log.info(f"\n--- {alb.key} ---")
    new_q = AlbumQuality(alb.files)
    log.info(f"  Staged:  {new_q}")

    existing = find_library_album(library, alb.artist, alb.album)
    if existing:
        old_files = library_tracks(existing)
        old_q = AlbumQuality(old_files)
        log.info(f"  Library: {old_q}  [{existing.name}]")

        old_hashes = {file_hash(f) for f in old_files}
        if old_hashes and all(file_hash(f) in old_hashes for f in alb.files):
            log.info("  Identical to the library copy — nothing new here")
            for f in alb.files + alb.companions:
                trash.discard(f, "exact duplicate of library copy", origin=f.parent)
            stats["duplicates"] += len(alb.files)
            stats["albums_kept"] += 1
            return

        if merge:
            merge_album(alb, existing, old_files, library, trash, dry_run)
            return

        verdict, why = compare_albums(new_q, old_q)

        if verdict == "existing":
            log.info(f"  Keeping library copy — {why}")
            for f in alb.files + alb.companions:
                trash.discard(f, "library copy is better", origin=f.parent)
            stats["albums_kept"] += 1
            return

        if verdict == "uncertain":
            log.info(f"  UNCERTAIN — {why}. Moving to review folder, nothing deleted.")
            dest = review / alb.artist / alb.album
            for f in alb.files + alb.companions:
                log.info(f"    -> review: {f.name}")
                if not dry_run:
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.move(long_path(f), long_path(dest / f.name))
            stats["albums_needing_review"] += 1
            return

        if not upgrade:
            log.info(f"  Staged copy is better ({why}), but --no-upgrade is set. Skipping.")
            stats["albums_kept"] += 1
            return
        log.info(f"  UPGRADE — {why}")
        trash.discard(existing, "replaced by better copy", origin=library)
        stats["albums_upgraded"] += 1

    dest = library / alb.artist / alb.album
    for path, tags in alb.tracks:
        write_tags(path, tags, dry_run)
        target = dest / sanitize_name(path.name, limit=150)

        if target.exists():
            if file_hash(path) == file_hash(target):
                trash.discard(path, "identical copy already in library", origin=path.parent)
                stats["duplicates"] += 1
                continue
            q_new, q_old = read_quality(path), read_quality(target)
            if q_new and q_old and (q_new.lossless, q_new.bitrate) > (q_old.lossless, q_old.bitrate):
                log.info(f"    Replacing {target.name}: {q_old} -> {q_new}")
                trash.discard(target, "lower-quality track replaced", origin=library)
            else:
                trash.discard(path, "library track is equal or better", origin=path.parent)
                stats["duplicates"] += 1
                continue

        log.info(f"    -> {alb.artist}/{alb.album}/{target.name}")
        if move_verified(path, target, dry_run):
            stats["files_moved"] += 1

    for c in alb.companions:
        is_art = (c.suffix.lower() in ARTWORK_EXTENSIONS and c.stem.lower() in ARTWORK_NAMES)
        name = f"cover{c.suffix.lower()}" if is_art else c.name
        target = dest / sanitize_name(name, limit=150)
        if target.exists():
            continue
        log.info(f"    -> {name}")
        if move_verified(c, target, dry_run):
            stats["companions_moved"] += 1


# ============================================================================
# ENVIRONMENT & CLEANUP
# ============================================================================
def verify_environment(staging: Path, library: Path, trash_root: Path) -> bool:
    issues = []

    if not staging.is_dir():
        issues.append(f"Staging folder missing: {staging}")
    elif not os.access(str(staging), os.R_OK | os.W_OK):
        issues.append(f"No read/write access to staging: {staging}")

    for folder in (library, trash_root):
        if not folder.exists():
            try:
                folder.mkdir(parents=True, exist_ok=True)
                log.info(f"Created: {folder}")
            except OSError as e:
                issues.append(f"Cannot create {folder}: {e}")
        elif not os.access(str(folder), os.R_OK | os.W_OK):
            issues.append(f"No read/write access to {folder}")

    try:
        if library.resolve() in trash_root.resolve().parents or trash_root.resolve() == library.resolve():
            issues.append(f"Trash folder is inside the Plex library — move it outside {library}")
    except OSError:
        pass

    try:
        free_gb = shutil.disk_usage(library).free / (1024 ** 3)
        if free_gb < MIN_FREE_GB:
            issues.append(f"Only {free_gb:.1f}GB free on the library drive")
        else:
            log.info(f"Disk space: {free_gb:.1f}GB free on the library drive")
    except OSError as e:
        issues.append(f"Cannot check disk space: {e}")

    if FFMPEG is None:
        log.warning("ffmpeg not found on PATH — corruption checks fall back to a "
                    "header-only test. Install ffmpeg for a full decode check.")
    else:
        log.info(f"ffmpeg: {FFMPEG}")

    for issue in issues:
        log.error(f"FATAL: {issue}")
    if not issues:
        log.info("Environment check: PASSED")
    return not issues


def cleanup_empty_dirs(root: Path, exclude: List[Path], dry_run: bool):
    """Clean up empty directories, protecting excluded system/quarantine subfolders."""
    if dry_run or not root.exists():
        return
    exclude_resolved = {e.resolve() for e in exclude if e.exists()}
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        if d.resolve() in exclude_resolved or any(e in d.parents for e in exclude):
            continue
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


# ============================================================================
# MAIN
# ============================================================================
def main():
    p = argparse.ArgumentParser(description="Organize downloaded music into a Plex library")
    p.add_argument("--staging", default=STAGING_FOLDER)
    p.add_argument("--library", default=LIBRARY_FOLDER)
    p.add_argument("--trash", default=TRASH_FOLDER)
    p.add_argument("--review", default=REVIEW_FOLDER)
    p.add_argument("--unparsable", default=None,
                   help="Where to quarantine files that can't be parsed as audio "
                        f"(default: a '{UNPARSABLE_DIRNAME}' folder inside staging)")
    p.add_argument("--log-file", default=str(SCRIPT_DIR / LOG_FILE),
                   help="Log file path (default: alongside this script)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show every decision without moving, tagging, or trashing anything")
    p.add_argument("--no-upgrade", action="store_true",
                   help="Never replace an album already in the library")
    p.add_argument("--no-merge", action="store_false", dest="merge",
                   help="Disable track-level merging into existing library albums")
    p.add_argument("--quiet", action="store_true", help="Only warnings and the summary are logged/shown")
    p.add_argument("--trash-retain-days", type=int, default=TRASH_RETAIN_DAYS,
                   help=f"Auto-purge trash older than N days (default: {TRASH_RETAIN_DAYS}, 0 = forever)")
    p.add_argument("--list-trash", action="store_true", help="Show what's been discarded")
    p.add_argument("--full-list", action="store_true", help="With --list-trash, show every file")
    p.add_argument("--restore-trash", metavar="BATCH", help="Copy a trash batch back out")
    p.add_argument("--restore-to", metavar="DIR")
    args = p.parse_args()

    # Always keep track-level merging active unless explicitly disabled
    if not hasattr(args, "merge") or args.merge is None:
        args.merge = True

    staging = Path(args.staging)
    library = Path(args.library)
    trash_root = Path(args.trash)
    review = Path(args.review)
    if args.unparsable:
        unparsable_dir = Path(args.unparsable)
    elif UNPARSABLE_DIR:
        unparsable_dir = Path(UNPARSABLE_DIR)
    else:
        unparsable_dir = staging / UNPARSABLE_DIRNAME

    setup_logging(Path(args.log_file), args.quiet)
    trash = Trash(trash_root, args.trash_retain_days, args.dry_run)

    if args.list_trash:
        sys.exit(trash.show(detailed=not args.quiet,
                            limit=0 if args.full_list else TRASH_LIST_LIMIT))
    if args.restore_trash:
        sys.exit(trash.restore(args.restore_trash, args.restore_to))

    log.info("=" * 60)
    log.info(f"Music manager starting{' (DRY RUN)' if args.dry_run else ''}")
    log.info(f"  Staging: {staging}")
    log.info(f"  Library: {library}")
    log.info("=" * 60)

    if not verify_environment(staging, library, trash_root):
        sys.exit(1)

    try:
        trash.prune()
        extract_archives(staging, trash, args.dry_run)

        exclude_dirs = [trash_root, review, unparsable_dir]
        albums = collect_albums(staging, exclude=exclude_dirs,
                                unparsable_dir=unparsable_dir, dry_run=args.dry_run)
        if not albums:
            log.info("No music found in staging.")
        else:
            log.info(f"\n{len(albums)} album(s) to process")
            for alb in sorted(albums, key=lambda a: a.key):
                process_album(alb, library, review, trash, args.dry_run,
                              not args.no_upgrade, args.merge)

        cleanup_empty_dirs(staging, exclude=exclude_dirs, dry_run=args.dry_run)

    except KeyboardInterrupt:
        log.warning("\nInterrupted. Files already moved are safe; the rest are untouched in staging.")
        sys.exit(130)

    summary = {"summary": True}
    log.info("\n" + "=" * 60, extra=summary)
    log.info("RUN COMPLETE" + (" (DRY RUN — nothing changed)" if args.dry_run else ""), extra=summary)
    log.info("=" * 60, extra=summary)
    log.info(f"  Tracks moved:        {stats['files_moved']}", extra=summary)
    log.info(f"  Artwork/extras:      {stats['companions_moved']}", extra=summary)
    log.info(f"  Tags written:        {stats['metadata_written']}", extra=summary)
    log.info(f"  Extensions fixed:    {stats['extensions_fixed']}", extra=summary)
    log.info(f"  Albums upgraded:     {stats['albums_upgraded']}", extra=summary)
    log.info(f"  Albums merged:       {stats['albums_merged']}", extra=summary)
    log.info(f"  Missing tracks added:{stats['tracks_imported']}", extra=summary)
    log.info(f"  Albums kept as-is:   {stats['albums_kept']}", extra=summary)
    log.info(f"  Needing review:      {stats['albums_needing_review']}", extra=summary)
    log.info(f"  Duplicates trashed:  {stats['duplicates']}", extra=summary)
    log.info(f"  Audiobooks skipped:  {stats['books_skipped']}", extra=summary)
    log.info(f"  Ambiguous (left):    {stats['ambiguous']}", extra=summary)
    log.info(f"  Unparsable:          {stats['unparsable']}", extra=summary)
    log.info(f"  Incomplete skipped:  {stats['skipped_incomplete']}", extra=summary)
    log.info(f"  Errors:              {stats['errors']}", extra=summary)
    log.info("=" * 60, extra=summary)

    if stats["books_skipped"]:
        log.info(f"\n{stats['books_skipped']} book/audiobook item(s) skipped "
                 "and left untouched in staging.")
    if stats["albums_needing_review"]:
        log.info(f"\n{stats['albums_needing_review']} album(s) were too close to call. "
                 f"They're in {review} — nothing was deleted.")
    if stats["tracks_imported"]:
        log.info(f"\n{stats['tracks_imported']} missing track(s) imported into "
                 f"{stats['albums_merged']} existing library album(s).")
    if stats["unparsable"]:
        log.info(f"\n{stats['unparsable']} unparsable file(s) moved to {unparsable_dir} "
                 f"— nothing was deleted.")
    if stats["trashed"] and not args.dry_run:
        log.info(f"\n{stats['trashed']} item(s) moved to trash, recoverable for "
                 f"{args.trash_retain_days} days. See them with --list-trash")

    sys.exit(1 if stats["errors"] else 0)


if __name__ == "__main__":
    main()