"""
Recover orphaned .lrc files: ones with no audio file of the same name in
the same folder, typically left behind by a tagger plugin that wrote the
lyrics file next to the wrong track (or under a filename that no longer
matches after a rename/retag elsewhere).

Each orphan's filename is parsed for an artist/title (and, where present,
an album) guess, matched against the audio library by tag content, and -
when a confident match is found - moved next to the correct track,
renamed to share its exact base name.

Album disambiguation matters because artist+title alone is not always
unique: the same song can exist multiple times in a library (studio,
live, a compilation), and the orphan's filename often carries the album
name precisely to distinguish between them.

Dry run by default; nothing is moved until --apply is passed.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from .common import AUDIO_EXTS, read_track_tags

# Thresholds for album-based disambiguation when several candidates share
# the same artist+title (duplicate versions: studio/live/compilation).
ALBUM_MATCH_MIN_SCORE = 0.55
ALBUM_MATCH_MIN_MARGIN = 0.12


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace - for comparison purposes only."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_album(text: str) -> str:
    """Like normalize(), but also drops a trailing year in parentheses, e.g. '(2013)'."""
    text = re.sub(r"\(\d{4}\)", "", text)
    return normalize(text)


def build_indexes(root: Path):
    """
    Build two indexes:
    - triple_index: (artist_norm, album_norm, title_norm) -> Path (exact match, fast)
    - title_index:  (artist_norm, title_norm) -> list of (album_norm, Path) (for disambiguation)
    """
    triple_index = {}
    title_index = defaultdict(list)
    print("Indexing the audio library (can take a few minutes on a large collection)...")
    count = 0
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            if Path(fname).suffix.lower() not in AUDIO_EXTS:
                continue
            fpath = Path(dirpath) / fname
            tags = read_track_tags(fpath)
            if not tags.artist or not tags.title:
                continue
            a_norm = normalize(tags.artist)
            t_norm = normalize(tags.title)
            al_norm = normalize_album(tags.album) if tags.album else ""
            triple_index[(a_norm, al_norm, t_norm)] = fpath
            title_index[(a_norm, t_norm)].append((al_norm, fpath))
            count += 1
            if count % 1000 == 0:
                print(f"  ...{count} tracks indexed")
    print(f"Index built: {count} tracks with usable artist+title tags.\n")
    return triple_index, title_index


# A few common filename shapes seen for orphaned .lrc files in practice.
# Tried in order; the first match wins. The ones that capture an album
# allow disambiguating between duplicate versions of the same song.
FILENAME_PATTERNS = [
    # Artist - Album - 03 - Title.lrc   (has an album -> disambiguation possible)
    re.compile(r"^(?P<artist>.+?) - (?P<album>.+?) - \d{1,3} - (?P<title>.+)$"),
    # 03. Artist - Title.lrc  /  03 - Artist - Title.lrc  (no album)
    re.compile(r"^\d{1,3}[.\-]\s*(?P<artist>.+?) - (?P<title>.+)$"),
    # Artist - Title.lrc  (no album)
    re.compile(r"^(?P<artist>.+?) - (?P<title>.+)$"),
]


def guess_candidates(stem: str):
    """Extract (artist, album_or_None, title) guesses from a .lrc filename, trying several patterns."""
    candidates = []
    for pattern in FILENAME_PATTERNS:
        m = pattern.match(stem)
        if m:
            groups = m.groupdict()
            candidates.append((
                groups["artist"].strip(),
                groups.get("album", "").strip() if groups.get("album") else None,
                groups["title"].strip(),
            ))
    return candidates


def find_orphaned_lrc(root: Path):
    orphans = []
    for dirpath, _, filenames in os.walk(root):
        lrc_files = [f for f in filenames if f.lower().endswith(".lrc")]
        if not lrc_files:
            continue
        audio_stems = {Path(f).stem for f in filenames if Path(f).suffix.lower() in AUDIO_EXTS}
        for lrc in lrc_files:
            if Path(lrc).stem not in audio_stems:
                orphans.append(Path(dirpath) / lrc)
    return orphans


def fuzzy_match_corpus(artist: str, title: str, title_index, threshold: float = 0.82):
    """
    Approximate search across the whole corpus, used only when title_index
    has no entry at all for the guessed (artist, title) - a last resort
    for orphans with an unusually irregular filename.
    """
    a_norm, t_norm = normalize(artist), normalize(title)
    best, best_score = None, 0.0
    for (ea, et), entries in title_index.items():
        title_score = difflib.SequenceMatcher(None, t_norm, et).ratio()
        if title_score < threshold:
            continue
        artist_score = difflib.SequenceMatcher(None, a_norm, ea).ratio()
        score = title_score * 0.7 + artist_score * 0.3
        if score > best_score:
            best_score = score
            best = entries[0][1] if len(entries) == 1 else None  # only if unique, otherwise ambiguous
    return best if best_score >= threshold else None


def resolve(artist: str, album, title: str, triple_index, title_index):
    """
    Try to find the correct audio file for a guessed (artist, album, title).
    Returns (path_or_None, match_type) where match_type is "exact",
    "album-disambiguated", "fuzzy", "collision" or None.
    """
    a_norm, t_norm = normalize(artist), normalize(title)

    # 1. Exact triple match, if an album guess is available.
    if album:
        al_norm = normalize_album(album)
        key = (a_norm, al_norm, t_norm)
        if key in triple_index:
            return triple_index[key], "exact"

    # 2. How many real variants exist for this artist+title?
    entries = title_index.get((a_norm, t_norm), [])
    if len(entries) == 1:
        # Only one variant in the library - no album needed to be confident.
        return entries[0][1], "exact"

    if len(entries) > 1 and album:
        al_norm = normalize_album(album)
        scored = sorted(
            ((difflib.SequenceMatcher(None, al_norm, ea).ratio(), path) for ea, path in entries),
            key=lambda x: x[0],
            reverse=True,
        )
        top_score, top_path = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        if top_score >= ALBUM_MATCH_MIN_SCORE and (top_score - second_score) >= ALBUM_MATCH_MIN_MARGIN:
            return top_path, "album-disambiguated"
        return None, "collision"  # still ambiguous even with the album name

    if len(entries) > 1:
        return None, "collision"  # several variants, no album name to choose between them

    # 3. Nothing exact - fuzzy search across the whole corpus (last resort).
    target = fuzzy_match_corpus(artist, title, title_index)
    if target:
        return target, "fuzzy"

    return None, None


def audit_leftover(artist: str, title: str, title_index):
    """
    For a .lrc that could not be moved automatically (collision or no
    match), check whether anything close exists in the library and, if
    so, whether it already has a .lrc next to it. Answers "is it safe to
    delete this?" without moving anything - informational only.
    """
    a_norm, t_norm = normalize(artist), normalize(title)
    entries = title_index.get((a_norm, t_norm), [])

    if not entries:
        # Nothing exact - try a looser search, but restricted to the same
        # artist, to avoid guessing wildly.
        best_entries, best_score = [], 0.0
        for (ea, et), cand_entries in title_index.items():
            if ea != a_norm:
                continue
            score = difflib.SequenceMatcher(None, t_norm, et).ratio()
            if score > best_score:
                best_score = score
                best_entries = cand_entries
        if best_score >= 0.78:
            entries = best_entries
        else:
            return "NOT IN LIBRARY (nothing close found) -> safe to delete"

    has_lrc = [e for e in entries if e[1].with_suffix(".lrc").exists()]
    if has_lrc:
        dests = "; ".join(str(e[1].with_suffix(".lrc")) for e in has_lrc)
        return f"likely DUPLICATE (a .lrc already exists at: {dests}) -> safe to delete"

    dests = "; ".join(str(e[1]) for e in entries)
    return f"GENUINELY MISSING - the track exists but still has no .lrc: {dests} -> do NOT delete, check manually"


def run(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"No such directory: {root}")

    triple_index, title_index = build_indexes(root)
    orphans = find_orphaned_lrc(root)
    print(f"Found {len(orphans)} orphaned .lrc files (no matching audio in the same folder).\n")

    matched_exact, matched_album, matched_fuzzy = [], [], []
    collisions, unmatched = [], []

    for lrc_path in orphans:
        stem = lrc_path.stem
        candidates = guess_candidates(stem)
        best_guess = (candidates[0][0], candidates[0][2]) if candidates else (stem, stem)

        result_path, result_type = None, None
        collision_note = None
        for artist, album, title in candidates:
            path, rtype = resolve(artist, album, title, triple_index, title_index)
            if path:
                result_path, result_type = path, rtype
                break
            if rtype == "collision" and collision_note is None:
                collision_note = (artist, album, title)

        if result_path is None:
            audit = audit_leftover(best_guess[0], best_guess[1], title_index)
            if collision_note:
                collisions.append((lrc_path, collision_note, audit))
            else:
                unmatched.append((lrc_path, audit))
            continue

        dest = result_path.with_suffix(".lrc")
        if dest.exists() and dest != lrc_path:
            audit = f"confirmed DUPLICATE (a .lrc already exists at: {dest}) -> safe to delete"
            collisions.append((lrc_path, ("(destination already has a .lrc)", "", str(dest)), audit))
            continue

        if result_type == "exact":
            matched_exact.append((lrc_path, dest))
        elif result_type == "album-disambiguated":
            matched_album.append((lrc_path, dest))
        else:
            matched_fuzzy.append((lrc_path, dest))

    leftover_dup_count = sum(1 for _, _, audit in collisions if audit.startswith("confirmed DUPLICATE")) + \
        sum(1 for _, audit in unmatched if audit.startswith("likely DUPLICATE") or audit.startswith("confirmed DUPLICATE"))
    leftover_missing_count = sum(1 for _, _, audit in collisions if audit.startswith("GENUINELY MISSING")) + \
        sum(1 for _, audit in unmatched if audit.startswith("GENUINELY MISSING"))
    leftover_gone_count = sum(1 for _, _, audit in collisions if audit.startswith("NOT IN LIBRARY")) + \
        sum(1 for _, audit in unmatched if audit.startswith("NOT IN LIBRARY"))

    lines = []
    lines.append(f"Total orphans found: {len(orphans)}")
    lines.append(f"Matched exactly (safe): {len(matched_exact)}")
    lines.append(f"Matched via album disambiguation (spot-check): {len(matched_album)}")
    lines.append(f"Matched via fuzzy corpus search (check these first): {len(matched_fuzzy)}")
    lines.append(f"Remaining collisions (could not disambiguate, NOT applied automatically): {len(collisions)}")
    lines.append(f"Unmatched: {len(unmatched)}")
    lines.append(f"  of which, after audit: {leftover_dup_count} confirmed duplicates safe to delete, "
                 f"{leftover_missing_count} genuinely missing (needs manual review), "
                 f"{leftover_gone_count} tracks that do not appear to exist in the library at all\n")

    lines.append("=== EXACT MATCHES (applied automatically with --apply) ===")
    for src, dest in matched_exact:
        lines.append(f"{src}  ->  {dest}")

    lines.append("\n=== ALBUM-DISAMBIGUATED MATCHES (applied automatically with --apply) ===")
    for src, dest in matched_album:
        lines.append(f"{src}  ->  {dest}")

    lines.append("\n=== FUZZY MATCHES (applied automatically with --apply, review these first) ===")
    for src, dest in matched_fuzzy:
        lines.append(f"{src}  ->  {dest}")

    lines.append("\n=== COLLISIONS + UNMATCHED, with audit (NOT applied automatically - see verdict) ===")
    for src, note, audit in collisions:
        lines.append(f"{src}\n   context: {note}\n   audit: {audit}\n")
    for src, audit in unmatched:
        lines.append(f"{src}\n   audit: {audit}\n")

    Path(args.report).write_text("\n".join(lines), encoding="utf-8")
    print(f"Full report written to: {args.report}\n")
    print(
        f"Exact: {len(matched_exact)} | Album: {len(matched_album)} | Fuzzy: {len(matched_fuzzy)} "
        f"| Collisions: {len(collisions)} | Unmatched: {len(unmatched)}"
    )

    if args.apply:
        print("\nApplying moves (exact + album + fuzzy)...")
        moved = 0
        for src, dest in matched_exact + matched_album + matched_fuzzy:
            try:
                src.rename(dest)
                moved += 1
            except Exception as e:
                print(f"Error moving {src}: {e}")
        print(f"Moved {moved} .lrc files.")
    else:
        print("\nThis was a DRY RUN - nothing was moved.")
        print("Check the report, then re-run with --apply.")

    if args.delete_safe_leftovers:
        print("\nDeleting leftovers marked DUPLICATE or NOT IN LIBRARY...")
        deleted, kept = 0, 0
        for src, note, audit in collisions:
            if audit.startswith("confirmed DUPLICATE") or audit.startswith("NOT IN LIBRARY"):
                try:
                    src.unlink()
                    deleted += 1
                except Exception as e:
                    print(f"Error deleting {src}: {e}")
            else:
                kept += 1
        for src, audit in unmatched:
            if audit.startswith("likely DUPLICATE") or audit.startswith("NOT IN LIBRARY"):
                try:
                    src.unlink()
                    deleted += 1
                except Exception as e:
                    print(f"Error deleting {src}: {e}")
            else:
                kept += 1
        print(f"Deleted {deleted} .lrc files. Kept (GENUINELY MISSING) {kept} files for manual review.")


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser(
        "relocate",
        help="Recover orphaned .lrc files by matching them to the correct track and moving them there.",
    )
    parser.add_argument("root", help="Library root directory")
    parser.add_argument("--apply", action="store_true", help="Actually apply the moves. Without this, dry-run only.")
    parser.add_argument("--delete-safe-leftovers", action="store_true",
                         help="Delete leftover .lrc files (collisions + unmatched) audited as DUPLICATE or "
                              "NOT IN LIBRARY. Never touches files audited as GENUINELY MISSING.")
    parser.add_argument("--report", default="relocate_report.txt")
    parser.set_defaults(func=run)
