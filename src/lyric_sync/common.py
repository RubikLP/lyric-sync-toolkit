"""
Shared utilities used across lyric_sync modules.

Kept deliberately small: only things that were duplicated, near-identically,
across three or more of the original standalone scripts. Module-specific
logic (filename parsing, section-marker cleanup, similarity scoring, etc.)
stays in its own module rather than being forced in here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None


AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".wma", ".aac", ".ape", ".wv"}


def write_exclusive(path: Path, content: str) -> bool:
    """
    Write a file only if it does not already exist, using the operating
    system's O_EXCL flag rather than a separate "does it exist" check.
    This is the safety mechanism the whole toolkit relies on: every write
    in every module goes through this function, so a bug anywhere else in
    the code can cause a skipped write at worst, never a silent overwrite
    of existing lyrics or timing data.

    Returns True if the file was written, False if it already existed.
    """
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except FileExistsError:
        return False


# Romanian has two Unicode conventions for the letters normally written
# with a comma-below diacritic (ș, ț): the modern standard (U+0219, U+021B)
# and an older cedilla form (ş, ţ at U+015F, U+0163) that is visually
# almost identical but a different codepoint. Sources scraped from
# different sites, at different times, are inconsistent about which one
# they use - and some sources drop the diacritics entirely. Comparing text
# across sources (or against a speech-to-text transcript, which will use
# the modern form) requires normalizing all of these to the same base
# letter, not just reconciling the two "correct" forms with each other.
_DIACRITIC_STRIP_TABLE = str.maketrans({
    "ă": "a", "â": "a", "Ă": "a", "Â": "a",
    "î": "i", "Î": "i",
    "ș": "s", "ş": "s", "Ș": "s", "Ş": "s",
    "ț": "t", "ţ": "t", "Ț": "t", "Ţ": "t",
})


def strip_diacritics(text: str) -> str:
    """Reduce Romanian diacritics to their base ASCII letter, for comparison purposes only."""
    return text.translate(_DIACRITIC_STRIP_TABLE)


@dataclass
class TrackTags:
    artist: Optional[str] = None
    title: Optional[str] = None
    album: Optional[str] = None
    duration_seconds: Optional[float] = None


def read_track_tags(path: Path) -> TrackTags:
    """
    Read artist/title/album/duration from an audio file's tags via mutagen.
    Never raises: any read failure (missing tags, unreadable file, mutagen
    not installed) yields a TrackTags with the relevant fields left as
    None, so callers can decide how to handle missing data themselves.
    """
    if MutagenFile is None:
        return TrackTags()
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return TrackTags()
        artist = (audio.get("artist") or [None])[0]
        title = (audio.get("title") or [None])[0]
        album = (audio.get("album") or [None])[0]
        duration = audio.info.length if getattr(audio, "info", None) else None
        return TrackTags(artist=artist, title=title, album=album, duration_seconds=duration)
    except Exception:
        return TrackTags()
