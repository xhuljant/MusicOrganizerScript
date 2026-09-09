#!/usr/bin/env python3
r"""
mediakind.py - shared "is this music or an audiobook?" classifier.

*** KEEP THIS FILE BYTE-IDENTICAL IN BOTH LOCATIONS ***
    C:\Scripts\AudiobookOrganizerScript\mediakind.py
    C:\Scripts\MusicOrganizerScript\mediakind.py

The audiobook organizer and the music organizer now share one loose staging
folder. Each asks this module who owns a given file / group of files:

    verdict, reasons = classify(paths, staging_folder)

    "audiobook" -> the audiobook organizer moves it; the music organizer skips it
    "music"     -> the music organizer moves it; the audiobook organizer skips it
    "ambiguous" -> neither touches it; it stays in staging and is logged

The bias is deliberate: a file is only pulled out as an audiobook when it carries
a real positive audiobook signal. Everything else stays "music", so ordinary,
lightly tagged music keeps flowing through the music organizer unchanged.
"ambiguous" is reserved for the genuinely unclear middle - e.g. one long track
with a musical genre tag.

After editing EITHER copy: run `python mediakind.py --self-test`, then copy the
file over the other location so the two never drift (if they disagree, a file can
be claimed by both organizers or by neither).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:  # the classifier still runs without mutagen, just with weaker signals
    import mutagen
except Exception:  # pragma: no cover
    mutagen = None

Verdict = str  # "audiobook" | "music" | "ambiguous"

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
AUDIOBOOK_CONTAINER_EXTS = {".m4b", ".aax", ".aa"}
AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aax", ".aa", ".flac", ".ogg", ".opus",
              ".wav", ".wma", ".aac", ".aiff"}
EBOOK_EXTS = {".epub", ".mobi", ".azw3", ".azw", ".pdf", ".cbz", ".cbr",
              ".djvu", ".fb2", ".lit", ".ibooks"}

# A genre tag in here is a hard "audiobook".
AUDIOBOOK_GENRES = {
    "audiobook", "audio book", "audiobooks", "spoken", "spoken word",
    "spoken-word", "speech", "podcast", "radio drama", "graphicaudio",
    "graphic audio", "lecture", "sermon",
}
# A genre tag in here can only pull a *weak* audiobook signal down to
# "ambiguous"; it never overrides a hard audiobook signal.
MUSIC_GENRES = {
    "rock", "pop", "hip hop", "hip-hop", "rap", "electronic", "edm", "house",
    "techno", "trance", "metal", "punk", "jazz", "blues", "classical",
    "soundtrack", "folk", "country", "r&b", "rnb", "soul", "reggae", "indie",
    "alternative", "dance", "ambient", "post-rock", "instrumental", "disco",
    "funk", "gospel", "latin", "k-pop", "grunge", "hardcore",
}

# Substring match against raw tag KEYS (lower-cased).
AUDIOBOOK_TAG_HINTS = ("asin", "audible", "narrat", "audnex", "book_series",
                       "series-part", "series_part", "shortdescription",
                       "longdescription")
MUSIC_TAG_HINTS = ("musicbrainz", "replaygain", "----:com.apple.itunes:isrc",
                   "isrc", "beatport", "discogs_", "acoustid", "barcode",
                   "catalognumber")

# Only per-item subfolder names count here (the check is staging-relative), so a
# shared root that happens to be called "Downloads\Audiobooks" won't taint music.
PATH_KEYWORDS = ("audiobook", "audio book", "unabridged", "narrated", "read by",
                 "graphicaudio", "graphic audio", "librivox", "audible")
SIDECAR_NAMES = {"metadata.json", "desc.txt", "reader.txt", "bookinfo.txt",
                 "book.nfo"}

# Deliberately does NOT match bare "Track 01" - that is an overwhelmingly common
# *music* filename. Audiobooks use "Chapter" / "Part" / "Section".
CHAPTER_RE = re.compile(
    r"\b(chapters?|chapitre|kapitel|part\s*\d+|ch\.?\s*\d{1,3}|"
    r"pt\.?\s*\d{1,3}|\d{1,3}\s*of\s*\d{1,3}|disc\s*\d+\s*[-_]\s*track|"
    r"section\s*\d+|book\s*\d+\s*[-_]\s*\d+)\b", re.I)
NARRATION_RE = re.compile(
    r"\b(unabridged|abridged|narrat(?:ed|or)|read by|performed by|dramatiz)", re.I)

# Directory names either organizer uses for its own bookkeeping - never scan into
# these even if they end up sitting inside the shared staging folder.
CONTROL_DIR_NAMES = {
    "_music_trash", "_music_review", "_music_unparsable", "_unparsable",
    "_audiobook_trash", "_needs_sorting",
}

# Duration thresholds (seconds).
LONG_TRACK = 20 * 60          # median track at/above this -> weak audiobook signal
SEQ_PART_MIN_MEDIAN = 10 * 60  # "many sequential parts" only counts if parts are long-ish

_BLANK_PROBE: Dict[str, object] = {
    "genre": "", "keys": set(), "duration": 0.0, "title": "",
    "album": "", "artist": "", "albumartist": "", "track": 0, "comment": "",
}

# Tests set this to a callable(Path) -> partial probe dict, to stay hermetic.
_PROBE_HOOK = None


# ---------------------------------------------------------------------------
# Metadata probe
# ---------------------------------------------------------------------------
def _long_path(p: Path) -> str:
    s = str(p)
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + os.path.abspath(s)
    return s


def _stringify(value) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(_stringify(v) for v in value)
    try:
        text = getattr(value, "text", value)
    except Exception:
        text = value
    if isinstance(text, (list, tuple)):
        return " ".join(str(v) for v in text)
    return str(text)


def _norm(s: str) -> str:
    return " ".join(str(s or "").strip().lower().split())


def _probe(path: Path) -> Dict[str, object]:
    """Return the handful of metadata facts the classifier needs about one file."""
    if _PROBE_HOOK is not None:
        merged = dict(_BLANK_PROBE)
        merged["keys"] = set()
        merged.update(_PROBE_HOOK(path) or {})
        return merged

    info: Dict[str, object] = dict(_BLANK_PROBE)
    info["keys"] = set()
    if mutagen is None:
        return info

    try:
        easy = mutagen.File(_long_path(path), easy=True)
    except Exception:
        easy = None
    if easy is not None:
        def first(key: str) -> str:
            v = easy.get(key) or []
            return str(v[0]).strip() if v else ""

        info["genre"] = _norm(first("genre"))
        info["title"] = first("title")
        info["album"] = first("album")
        info["artist"] = first("artist")
        info["albumartist"] = first("albumartist")
        raw_track = first("tracknumber")
        m = re.match(r"\s*(\d+)", raw_track)
        info["track"] = int(m.group(1)) if m else 0
        if getattr(easy, "info", None) is not None:
            info["duration"] = float(getattr(easy.info, "length", 0) or 0)

    try:
        raw = mutagen.File(_long_path(path))
    except Exception:
        raw = None
    if raw is not None:
        try:
            info["keys"] = {str(k).lower() for k in raw.keys()}
        except Exception:
            info["keys"] = set()
        comment_bits: List[str] = []
        for key in list(info["keys"]):
            if ("comm" in key or "description" in key or "ldes" in key
                    or key in ("\xa9cmt", "comment", "tit3", "subtitle", "©cmt")):
                try:
                    comment_bits.append(_stringify(raw[key]))
                except Exception:
                    pass
        info["comment"] = " ".join(comment_bits)
        if not info["genre"]:
            for key in ("tcon", "\xa9gen", "©gen", "genre"):
                if key in raw:
                    info["genre"] = _norm(_stringify(raw[key]))
                    break
    return info


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
def _sequential(tracks: Sequence[int]) -> bool:
    nums = sorted(n for n in tracks if n > 0)
    if len(nums) < 2 or nums[0] not in (0, 1):
        return False
    return nums == list(range(nums[0], nums[0] + len(nums)))


def _context_names(paths: Sequence[Path], context: Optional[Iterable[str]],
                   staging: Optional[Path]) -> set:
    if context is not None:
        return {str(c).lower() for c in context}
    names: set = set()
    try:
        staging_res = Path(staging).resolve() if staging is not None else None
    except OSError:
        staging_res = None
    for folder in {p.parent for p in paths}:
        # Files loose in the shared staging root have no meaningful "folder
        # context" - the whole download dump is not an album/book. Only a
        # dedicated per-item subfolder counts.
        try:
            if staging_res is not None and folder.resolve() == staging_res:
                continue
        except OSError:
            pass
        try:
            names |= {c.name.lower() for c in folder.iterdir()}
        except OSError:
            pass
    return names


def classify(paths: Sequence, staging, *,
             context: Optional[Iterable[str]] = None) -> Tuple[Verdict, List[str]]:
    """Classify one file or one already-grouped album/book.

    `paths`   - the audio files that belong together (an album or a book).
    `staging` - the shared staging root, used to keep path-keyword checks
                per-item (relative) rather than tainting everything under a
                root that happens to contain the word "audiobook".
    `context` - optional iterable of sibling file names; when omitted the
                containing folder(s) are listed from disk.
    """
    paths = [Path(p) for p in paths]
    if not paths:
        return "music", ["no files"]

    exts = {p.suffix.lower() for p in paths}
    if exts & AUDIOBOOK_CONTAINER_EXTS:
        return "audiobook", ["dedicated audiobook container "
                             f"({', '.join(sorted(exts & AUDIOBOOK_CONTAINER_EXTS))})"]

    names = _context_names(paths, context, staging)
    if any(name.rsplit(".", 1)[-1] == ext.lstrip(".")
           for name in names for ext in EBOOK_EXTS if "." in name):
        return "audiobook", ["an ebook file sits alongside the audio"]
    if any(name.endswith(tuple(AUDIOBOOK_CONTAINER_EXTS)) for name in names):
        return "audiobook", ["a .m4b/.aax file sits alongside the audio"]
    if any(name in SIDECAR_NAMES or name.endswith(".opf") for name in names):
        return "audiobook", ["an audiobook sidecar (.opf / metadata.json) is present"]

    try:
        rel = str(paths[0].parent.resolve()
                  .relative_to(Path(staging).resolve())).lower().replace("\\", "/")
    except Exception:
        rel = ""
    for kw in PATH_KEYWORDS:
        if kw in rel:
            return "audiobook", [f"folder path contains {kw!r}"]

    probes = [_probe(p) for p in paths]
    keys: set = set().union(*(p["keys"] for p in probes)) if probes else set()
    genres = {p["genre"] for p in probes if p["genre"]}
    durations = sorted(float(p["duration"]) for p in probes if p["duration"])

    for key in keys:
        for hint in AUDIOBOOK_TAG_HINTS:
            if hint in key:
                return "audiobook", [f"audiobook-style tag present ({hint!r})"]
    hit_genres = genres & AUDIOBOOK_GENRES
    if hit_genres:
        return "audiobook", [f"genre tag is {sorted(hit_genres)}"]

    music_genre = bool(genres & MUSIC_GENRES)
    music_tag = any(hint in key for key in keys for hint in MUSIC_TAG_HINTS)

    weak: List[str] = []
    median = durations[len(durations) // 2] if durations else 0.0
    if median >= LONG_TRACK:
        weak.append(f"median track ~{int(median // 60)} min")

    per_file = [f"{p['title'] or path.stem} {path.name}"
                for p, path in zip(probes, paths)]
    chapter_hits = sum(1 for text in per_file if CHAPTER_RE.search(text))
    if chapter_hits >= 5 and chapter_hits >= 0.6 * len(paths):
        # Many files literally named "Chapter N" / "Part N" - a music album
        # never looks like this, whatever the durations are.
        if music_genre or music_tag:
            return "ambiguous", [f"{chapter_hits}/{len(paths)} files chapter-named",
                                 "but also looks like a music release"]
        return "audiobook", [f"{chapter_hits} of {len(paths)} files are chapter/part-named"]
    if chapter_hits >= 1:
        weak.append("chapter / part naming")

    if NARRATION_RE.search(" ".join(str(p["comment"]) for p in probes)):
        weak.append("narration wording in comments/subtitle")

    albums = {_norm(p["album"]) for p in probes if p["album"]}
    artists = {_norm(p["albumartist"] or p["artist"])
               for p in probes if (p["albumartist"] or p["artist"])}
    tracks = [int(p["track"]) for p in probes if p["track"]]
    if (len(paths) > 15 and len(albums) <= 1 and len(artists) == 1
            and _sequential(tracks)
            and (not durations or median >= SEQ_PART_MIN_MEDIAN)):
        weak.append(f"one author, {len(paths)} sequential parts")

    if weak and (music_genre or music_tag):
        # Real audiobook signal, but it also looks like a music release - do not
        # let either organizer move it on a guess.
        extra = ([f"but genre {sorted(genres & MUSIC_GENRES)} is musical"] if music_genre else [])
        extra += (["but music-catalog tags are present"] if music_tag else [])
        return "ambiguous", weak + extra
    if len(weak) >= 2:
        return "audiobook", weak
    if len(weak) == 1:
        return "ambiguous", weak
    if music_genre:
        return "music", [f"musical genre {sorted(genres & MUSIC_GENRES)}"]
    if music_tag:
        return "music", ["music-catalog tags present"]
    return "music", ["no audiobook signal"]


# Readability alias for callers that pass a whole album/book at once.
classify_group = classify


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def _self_test() -> int:
    global _PROBE_HOOK

    fixtures: Dict[str, Dict[str, object]] = {}

    def hook(path: Path) -> Dict[str, object]:
        return fixtures.get(Path(path).name, {})

    _PROBE_HOOK = hook
    cases = []

    def case(name, paths, probes, expected, ctx=()):
        fixtures.clear()
        fixtures.update(probes)
        got, why = classify(paths, r"X:\staging", context=ctx)
        cases.append((name, expected, got, why))

    S = r"X:\staging"

    case("genre=Audiobook mp3 group",
         [S + r"\01 - Circe.mp3", S + r"\02 - Circe.mp3"],
         {"01 - Circe.mp3": {"genre": "audiobook", "duration": 2400},
          "02 - Circe.mp3": {"genre": "audiobook", "duration": 2500}},
         "audiobook")

    case("normal tagged rock album",
         [S + fr"\Band - Album - {i:02d}.flac" for i in range(1, 11)],
         {f"Band - Album - {i:02d}.flac": {"genre": "rock", "duration": 200,
                                           "album": "Album", "artist": "Band",
                                           "albumartist": "Band", "track": i}
          for i in range(1, 11)},
         "music")

    case("16-track one-artist album (must stay music)",
         [S + fr"\Band - Big Album - {i:02d}.flac" for i in range(1, 17)],
         {f"Band - Big Album - {i:02d}.flac": {"duration": 210, "album": "Big Album",
                                               "artist": "Band", "albumartist": "Band",
                                               "track": i}
          for i in range(1, 17)},
         "music")

    case("20 chapter files, no genre, no durations",
         [S + fr"\Chapter {i}.mp3" for i in range(1, 21)],
         {f"Chapter {i}.mp3": {"album": "The Book", "artist": "A Narrator",
                               "albumartist": "A Narrator", "track": i}
          for i in range(1, 21)},
         "audiobook")

    case("lone 30-min file with a musical genre -> ambiguous",
         [S + r"\weird.mp3"],
         {"weird.mp3": {"genre": "post-rock", "duration": 1900}},
         "ambiguous")

    case("lone 6-min pop song -> music",
         [S + r"\song.mp3"],
         {"song.mp3": {"genre": "pop", "duration": 360}},
         "music")

    case("bare untagged single mp3 -> music",
         [S + r"\track01.mp3"],
         {"track01.mp3": {"duration": 240}},
         "music")

    case("ASIN tag present -> audiobook",
         [S + r"\part1.m4a"],
         {"part1.m4a": {"duration": 300, "keys": {"----:com.apple.itunes:asin"}}},
         "audiobook")

    case("ebook in the same per-item subfolder -> audiobook",
         [S + r"\book\01.mp3"],
         {"01.mp3": {"duration": 200}},
         "audiobook", ctx=["01.mp3", "Some Book.epub"])

    case("loose album, ebook lies elsewhere in staging root -> music",
         [S + r"\Band - X - 01.mp3", S + r"\Band - X - 02.mp3"],
         {"Band - X - 01.mp3": {"genre": "rock", "duration": 200, "album": "X",
                                "artist": "Band", "albumartist": "Band", "track": 1},
          "Band - X - 02.mp3": {"genre": "rock", "duration": 200, "album": "X",
                                "artist": "Band", "albumartist": "Band", "track": 2}},
         "music", ctx=[])  # callers pass an empty context for root-level groups

    case("4 long 'Pt.' movements + classical genre -> ambiguous",
         [S + fr"\Symphony No. 5 - Pt. {i}.mp3" for i in range(1, 5)],
         {f"Symphony No. 5 - Pt. {i}.mp3": {"genre": "classical", "duration": 1200,
                                            "album": "Symphony No. 5", "artist": "BPO",
                                            "albumartist": "BPO", "track": i}
          for i in range(1, 5)},
         "ambiguous")

    case(".m4b in the group -> audiobook",
         [S + r"\thing.m4b"], {}, "audiobook")

    case("long chapters + musical genre still audiobook (2 weak? no - genre wins nothing)",
         [S + fr"\Pt {i}.mp3" for i in range(1, 4)],
         {f"Pt {i}.mp3": {"genre": "audiobook", "duration": 3000} for i in range(1, 4)},
         "audiobook")

    ok = True
    for name, expected, got, why in cases:
        flag = "ok  " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"  {flag} {name}: expected {expected}, got {got}  ({'; '.join(why)})")
    print("self-test:", "PASSED" if ok else "FAILED")
    _PROBE_HOOK = None
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    if len(sys.argv) > 1:
        verdict, why = classify(sys.argv[1:], os.getcwd())
        print(f"{verdict}: {'; '.join(why)}")
    else:
        print(__doc__)
