"""
Cross-check local lyrics (.txt/.lrc) against external sources, to catch
tracks that ended up with completely wrong lyrics (from a different song)
or tracks that are genuinely instrumental but picked up lyrics from an
unrelated source anyway.

Sources used:
  - LRCLIB (lrclib.net/api) - a clean API, no scraping involved, returning
    full lyric text (plain and/or synced) when available. This is the
    primary cross-check source: stable, and not affected by page-layout
    changes.
  - Genius (direct page scraping) - used ONLY for the "this song is an
    instrumental" signal, which has no structured equivalent on LRCLIB.
    This part is inherently fragile (Genius can change its page layout at
    any time); any failure (HTTP error, network error, parse failure) is
    reported separately as "could not verify", never treated as "wrong
    lyrics". If Genius becomes unreachable entirely, the rest of this
    module still works using LRCLIB alone, just without instrumental
    detection.

The similarity score is NOT expected to be a perfect match (different
transcriptions, minor wording, or repeat-count differences are normal).
It is a coarse sanity check that the local lyrics are, roughly, about the
same song - not a completely different one.

Two scopes:
  --mode full : check everything (.lrc AND .txt) across the whole library
                - a general cleanup pass, slower
  --mode new  : check only .txt files that do NOT yet have a matching
                .lrc - for validating freshly fetched lyrics before they
                reach the alignment step (faster, more targeted)

Without --apply, this only reports (dry run, the default). With --apply:
  - CONFIRMED instrumental tracks (clear Genius signal) get a sentinel
    .lrc written containing the literal text "This song is an
    instrumental" (via write_exclusive) - both media players and the
    rest of this toolkit then treat the track as already handled. If a
    (very likely wrong) .lrc already exists, it is replaced, since at
    this point the instrumental signal is confirmed and any prior content
    is necessarily wrong.
  - SUSPECT tracks (similarity below threshold against LRCLIB) are left
    untouched unless --rewrite-suspect or --delete-suspect is also given.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing requests. Install with: pip install requests --break-system-packages")

from .common import AUDIO_EXTS, read_track_tags, strip_diacritics, write_exclusive

INSTRUMENTAL_TEXT = "This song is an instrumental"


def normalize_text(text: str) -> str:
    text = strip_diacritics(text).lower()
    text = re.sub(r"\[[^\[\]]*\]", " ", text)  # drop section markers like [Chorus] from the comparison
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def similarity(local_text: str, remote_text: str) -> float:
    a, b = normalize_text(local_text), normalize_text(remote_text)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def local_lyric_text(path: Path) -> str:
    """Read a .txt or .lrc file's content, stripping timestamps for .lrc."""
    raw = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() == ".lrc":
        lines = []
        for line in raw.splitlines():
            line = re.sub(r"^\[\d+:\d+(?:\.\d+)?\]", "", line).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)
    return raw


def guess_artist_title(audio_path: Path):
    """Prefer real tags; fall back to filename/folder parsing if tags are missing."""
    tags = read_track_tags(audio_path)
    if tags.artist and tags.title:
        return tags.artist, tags.title
    stem = audio_path.stem
    parts = stem.split(" - ")
    title = parts[-1].strip() if parts else stem
    artist = audio_path.parent.parent.name if audio_path.parent.parent != audio_path.parent else ""
    return artist, title


def search_lrclib(artist: str, title: str, timeout: int):
    """
    Search LRCLIB. It can return several variants for the same song
    (radio/album/live/edit versions with somewhat different text) -
    picking "the first result" is arbitrary and can land on the wrong
    variant, or even a different song entirely. Rather than deciding here
    which one is "correct", return up to 5 candidates; the caller scores
    each and keeps the best match - if ANY variant matches well, that is
    good enough.

    Returns (candidates, error|None), where each candidate is
    {"text": ..., "synced": bool} - synced=True means the text is already
    in .lrc format (with timestamps), useful when writing it directly as
    a replacement (see rewrite_with_lrclib).
    """
    try:
        resp = requests.get(
            "https://lrclib.net/api/search",
            params={"artist_name": artist, "track_name": title},
            timeout=timeout,
            headers={"User-Agent": "lyric-sync-toolkit/1.0"},
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        return [], f"LRCLIB error: {e}"
    if not results:
        return [], "no LRCLIB results"
    candidates = []
    for r in results[:5]:
        synced_text = r.get("syncedLyrics")
        plain_text = r.get("plainLyrics")
        if synced_text:
            candidates.append({"text": synced_text, "synced": True})
        elif plain_text:
            candidates.append({"text": plain_text, "synced": False})
    if not candidates:
        return [], "LRCLIB results had no text"
    return candidates, None


def check_genius_instrumental(artist: str, title: str, timeout: int):
    """
    Best effort: search Genius for the track and check whether the page
    states explicitly that the song is an instrumental. Any network or
    parsing error is reported separately - a None result means "could not
    verify", NOT "not instrumental".
    Returns (is_instrumental: True/False/None, error: str|None).
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; lyric-sync-toolkit/1.0)"}
    try:
        resp = requests.get(
            "https://genius.com/api/search/song",
            params={"q": f"{artist} {title}"},
            timeout=timeout,
            headers=headers,
        )
        if resp.status_code != 200:
            return None, f"Genius search failed (HTTP {resp.status_code})"
        data = resp.json()
        hits = data.get("response", {}).get("sections", [{}])[0].get("hits", [])
        if not hits:
            return None, "no Genius results"
        song_url = hits[0]["result"]["url"]
    except Exception as e:
        return None, f"Genius search error: {e}"

    try:
        page = requests.get(song_url, timeout=timeout, headers=headers)
        if page.status_code != 200:
            return None, f"Genius page unreachable (HTTP {page.status_code})"
    except Exception as e:
        return None, f"Genius page fetch error: {e}"

    return (INSTRUMENTAL_TEXT in page.text), None


def write_instrumental_sentinel(path: Path, log_fn) -> bool:
    """
    Write the sentinel .lrc for a CONFIRMED instrumental track. If a .lrc
    already exists there (almost certainly wrong, since Genius has just
    confirmed the track is instrumental), replace it - a plain
    write_exclusive() would silently fail and leave the wrong content in
    place.
    """
    if write_exclusive(path, INSTRUMENTAL_TEXT + "\n"):
        return True
    try:
        old_content = path.read_text(encoding="utf-8", errors="ignore").strip()
        if old_content == INSTRUMENTAL_TEXT:
            return True  # already correct, nothing to do
        path.unlink()
        log_fn(f"      -> replaced wrong existing .lrc: {path.name}")
    except Exception as e:
        log_fn(f"      -> ERROR reading/deleting existing .lrc: {e}")
        return False
    return write_exclusive(path, INSTRUMENTAL_TEXT + "\n")


def rewrite_with_lrclib(audio_path: Path, winner: dict, log_fn) -> bool:
    """
    Replace local content (SUSPECT, likely lyrics from a different song)
    with what was found on LRCLIB for this track. Deletes any existing
    variant first (.lrc or .txt - either is known to be wrong at this
    point), then writes: .lrc directly if LRCLIB had a synced version
    (ready to use immediately), .txt if only plain text was available
    (left for the alignment step).
    """
    lrc_path = audio_path.with_suffix(".lrc")
    txt_path = audio_path.with_suffix(".txt")
    for p in (lrc_path, txt_path):
        if p.is_file():
            try:
                p.unlink()
            except Exception as e:
                log_fn(f"      -> ERROR deleting existing {p.name}: {e}")
                return False
    target = lrc_path if winner["synced"] else txt_path
    try:
        target.write_text(winner["text"].rstrip() + "\n", encoding="utf-8")
        log_fn(f"      -> rewritten with LRCLIB text ({'synced' if winner['synced'] else 'plain'}): {target.name}")
        return True
    except Exception as e:
        log_fn(f"      -> ERROR writing: {e}")
        return False


def find_targets(root: Path, mode: str):
    """
    mode="full": every .txt and .lrc in the library.
    mode="new" : only .txt files that do NOT yet have a matching .lrc.
    Returns a list of (audio_path, text_path).
    """
    import os

    targets = []
    for dirpath, _, filenames in os.walk(root):
        by_stem = {}
        for f in filenames:
            stem = Path(f).stem
            suf = Path(f).suffix.lower()
            by_stem.setdefault(stem, {})[suf] = Path(dirpath) / f

        for stem, files in by_stem.items():
            audio_path = next((files[e] for e in AUDIO_EXTS if e in files), None)
            if audio_path is None:
                continue
            if mode == "full":
                if ".lrc" in files:
                    targets.append((audio_path, files[".lrc"]))
                if ".txt" in files:
                    targets.append((audio_path, files[".txt"]))
            else:  # mode == "new"
                if ".txt" in files and ".lrc" not in files:
                    targets.append((audio_path, files[".txt"]))
    return targets


def find_targets_from_list(paths_file: Path):
    """
    Build targets ONLY from the paths listed in paths_file (one per line,
    pointing at a .lrc or .txt) - for targeted re-verification, e.g. just
    the tracks flagged SUSPECT in a previous run, without rescanning the
    whole library. For each path, the matching audio file (same stem, same
    folder) is located. Duplicate entries (same audio file listed more
    than once) are skipped after the first - re-verifying the same track
    twice could hit a file that the first pass already renamed or deleted.
    """
    targets = []
    seen_audio = set()
    lines = [l.strip() for l in paths_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    for line in lines:
        text_path = Path(line)
        if not text_path.is_file():
            print(f"  (skipped, not on disk): {text_path}")
            continue
        audio_path = None
        for ext in AUDIO_EXTS:
            candidate = text_path.with_suffix(ext)
            if candidate.is_file():
                audio_path = candidate
                break
        if audio_path is None:
            print(f"  (skipped, no matching audio file): {text_path}")
            continue
        if audio_path in seen_audio:
            print(f"  (skipped, duplicate entry): {text_path}")
            continue
        seen_audio.add(audio_path)
        targets.append((audio_path, text_path))
    return targets


def run(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"No such directory: {root}")

    print(f"Scanning library (mode: {args.mode})..." if not args.paths_file else f"Targeted check from: {args.paths_file}")
    if args.paths_file:
        targets = find_targets_from_list(Path(args.paths_file))
    else:
        targets = find_targets(root, args.mode)
    if args.limit:
        targets = targets[: args.limit]
    print(f"Found {len(targets)} files to check.\n")

    report_f = open(args.report, "a", encoding="utf-8")

    def log(line: str):
        print(line)
        report_f.write(line + "\n")
        report_f.flush()

    log(f"\n=== Run started: {time.strftime('%Y-%m-%d %H:%M:%S')} | mode={args.mode} | {len(targets)} files ===")

    ok, suspect, instrumental, unknown, changed = 0, 0, 0, 0, 0

    for i, (audio_path, text_path) in enumerate(targets, 1):
        if not text_path.is_file():
            log(f"  [{i}/{len(targets)}] SKIPPED (file disappeared in the meantime, likely already processed): {text_path}")
            continue

        artist, title = guess_artist_title(audio_path)
        local_text = local_lyric_text(text_path)

        is_instr, genius_err = (None, "skipped (--skip-genius)") if args.skip_genius else check_genius_instrumental(artist, title, args.timeout)
        time.sleep(args.delay)

        if is_instr is True:
            instrumental += 1
            log(f"  [{i}/{len(targets)}] INSTRUMENTAL (confirmed via Genius): {text_path}")
            if args.apply:
                sentinel_path = audio_path.with_suffix(".lrc")
                wrote = write_instrumental_sentinel(sentinel_path, log)
                if wrote:
                    log(f"      -> wrote sentinel .lrc: {sentinel_path.name}")
                else:
                    log("      -> could not write sentinel (see error above)")
                try:
                    if text_path.suffix.lower() == ".txt":
                        text_path.unlink()
                        log(f"      -> deleted old .txt (wrong lyrics for an instrumental): {text_path.name}")
                except Exception as e:
                    log(f"      -> ERROR deleting old .txt: {e}")
            continue

        remote_candidates, lrclib_err = search_lrclib(artist, title, args.timeout)
        time.sleep(args.delay)

        if not remote_candidates:
            unknown += 1
            log(f"  [{i}/{len(targets)}] UNKNOWN (could not verify - {lrclib_err}"
                f"{'; ' + genius_err if genius_err else ''}): {text_path}")
            continue

        scored = sorted(
            ((similarity(local_text, c["text"]), c) for c in remote_candidates),
            key=lambda x: x[0], reverse=True,
        )
        score, winner = scored[0]

        if score < args.min_similarity:
            suspect += 1
            log(f"  [{i}/{len(targets)}] SUSPECT (similarity {score:.0%} against LRCLIB): {text_path}")
            if args.apply and args.rewrite_suspect:
                if rewrite_with_lrclib(audio_path, winner, log):
                    changed += 1
            elif args.apply and args.delete_suspect:
                try:
                    text_path.unlink()
                    changed += 1
                    log(f"      -> deleted: {text_path.name}")
                except Exception as e:
                    log(f"      -> ERROR deleting: {e}")
        else:
            ok += 1
            log(f"  [{i}/{len(targets)}] OK (similarity {score:.0%}): {text_path.name}")

    summary = (f"Done. OK: {ok} | SUSPECT: {suspect} | INSTRUMENTAL: {instrumental} | "
               f"UNKNOWN: {unknown}" +
               (f" | Rewritten: {changed}" if args.rewrite_suspect else
                f" | Deleted: {changed}" if args.delete_suspect else ""))
    log(f"\n{summary}")
    report_f.close()
    print(f"Full details in: {args.report}")


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser(
        "verify-source",
        help="Cross-check local lyrics against LRCLIB and detect instrumental tracks via Genius.",
    )
    parser.add_argument("root")
    parser.add_argument("--mode", choices=["full", "new"], default="new",
                         help="'full' = everything (.lrc and .txt); 'new' = only .txt without a .lrc (default). "
                              "Ignored if --paths-file is given.")
    parser.add_argument("--paths-file", default=None,
                         help="Path to a text file listing .lrc/.txt paths to re-check, one per line "
                              "(e.g. extracted from SUSPECT lines in a previous report). If given, "
                              "--mode is ignored and ONLY these paths are checked, not the whole library.")
    parser.add_argument("--apply", action="store_true",
                         help="Actually write sentinel files for confirmed instrumentals. Without this, report only.")
    parser.add_argument("--delete-suspect", action="store_true",
                         help="Delete local files flagged SUSPECT (score below threshold). Requires --apply. "
                              "Ignored if --rewrite-suspect is given.")
    parser.add_argument("--rewrite-suspect", action="store_true",
                         help="For SUSPECT files, REWRITE locally with the best LRCLIB candidate found (even if "
                              "its score is still below threshold) instead of just deleting. Requires --apply. "
                              "Takes priority over --delete-suspect.")
    parser.add_argument("--min-similarity", type=float, default=0.35,
                         help="Below this score (0-1) against LRCLIB, flag as SUSPECT (default 0.35).")
    parser.add_argument("--skip-genius", action="store_true", help="Skip instrumental detection (LRCLIB only).")
    parser.add_argument("--delay", type=float, default=0.5, help="Pause between requests to external sources (seconds).")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--report", default="verify_source_report.txt")
    parser.set_defaults(func=run)
