"""
Fill in missing lyrics for tracks that have neither a .lrc nor a .txt file
next to them, using the `syncedlyrics` library (which aggregates LRCLIB,
NetEase, Tencent, Musixmatch and Genius).

Synced (timestamped) lyrics are always preferred. Plain text is only used
as an explicit opt-in fallback (--allow-plain), written as .txt rather than
.lrc so it never gets mistaken for something already synced - it is meant
to be picked up later by the alignment step.

Safety: this module never deletes anything, and every write goes through
write_exclusive() (see common.py) - a file that already exists is left
untouched regardless of what this run finds.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .common import AUDIO_EXTS, read_track_tags, write_exclusive


def find_missing(root: Path) -> list[Path]:
    """Find every audio file with neither a .lrc nor a .txt sibling of the same name."""
    missing = []
    for dirpath, _, filenames in os.walk(root):
        audio_files = [f for f in filenames if Path(f).suffix.lower() in AUDIO_EXTS]
        if not audio_files:
            continue
        existing_stems = {
            Path(f).stem for f in filenames
            if f.lower().endswith(".lrc") or f.lower().endswith(".txt")
        }
        for f in audio_files:
            if Path(f).stem not in existing_stems:
                missing.append(Path(dirpath) / f)
    return missing


def run(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"No such directory: {root}")

    print("Scanning library for tracks with no lyrics file (filesystem only, no network)...")
    missing = find_missing(root)
    print(f"Found {len(missing)} tracks with neither .lrc nor .txt.\n")

    if not args.fetch:
        print("This was a count only (default) - no network request was made, nothing was written.")
        print("Next step: a small test run, to see real-world speed before committing to the full library:")
        print(f"    lyric-sync fetch {args.root} --fetch --limit 50")
        return

    try:
        import syncedlyrics
    except ImportError:
        sys.exit("Missing syncedlyrics. Install with: pip install syncedlyrics --break-system-packages")

    targets = missing[: args.limit] if args.limit else missing
    print(f"Fetching for {len(targets)} tracks ({args.delay}s delay between requests)...\n")

    report_lines = []
    found, found_plain, notfound, errors, skipped_no_tags = 0, 0, 0, 0, 0
    start = time.time()

    for i, audio_path in enumerate(targets, 1):
        tags = read_track_tags(audio_path)
        if not tags.artist or not tags.title:
            skipped_no_tags += 1
            report_lines.append(f"SKIPPED (missing artist/title tags): {audio_path}")
            continue

        query = f"{tags.artist} - {tags.title}"
        try:
            lrc_text = syncedlyrics.search(query, synced_only=True, providers=["Lrclib", "NetEase", "Tencent"])
        except Exception as e:
            errors += 1
            report_lines.append(f"ERROR ({e}): {audio_path}")
            time.sleep(args.delay)
            continue

        if lrc_text:
            dest = audio_path.with_suffix(".lrc")
            if write_exclusive(dest, lrc_text):
                found += 1
                report_lines.append(f"FOUND and written: {dest}")
            else:
                report_lines.append(f"ALREADY EXISTS (skipped, not overwriting): {dest}")
        elif args.allow_plain:
            if tags.duration_seconds and tags.duration_seconds < 60:
                # Very short tracks (skits, interludes) carry a high risk of a
                # wrong plain-text match, and there is no synced timestamp to
                # cross-check against afterwards - skipping is safer than
                # risking wrong content.
                notfound += 1
                report_lines.append(f"SKIPPED plain-text fallback (track too short, high mismatch risk): {audio_path}")
                time.sleep(args.delay)
                continue

            time.sleep(args.delay)
            try:
                plain_text = syncedlyrics.search(query, plain_only=True,
                                                   providers=["Lrclib", "NetEase", "Tencent", "Genius"])
            except Exception as e:
                errors += 1
                report_lines.append(f"ERROR on plain-text fallback ({e}): {audio_path}")
                time.sleep(args.delay)
                continue

            if plain_text:
                dest = audio_path.with_suffix(".txt")
                if write_exclusive(dest, plain_text):
                    found_plain += 1
                    report_lines.append(f"FOUND plain text (unsynced) and written: {dest}")
                else:
                    report_lines.append(f"ALREADY EXISTS (skipped, not overwriting): {dest}")
            else:
                notfound += 1
                report_lines.append(f"NOT FOUND (neither synced nor plain): {audio_path}")
        else:
            notfound += 1
            report_lines.append(f"NOT FOUND (synced): {audio_path}")

        if i % 25 == 0 or i == len(targets):
            elapsed = time.time() - start
            rate = elapsed / i
            eta_min = rate * (len(targets) - i) / 60
            print(f"  [{i}/{len(targets)}] synced: {found} | plain: {found_plain} | not found: {notfound} "
                  f"| errors: {errors} | ~{rate:.1f}s/track | ETA: {eta_min:.1f} min")

        time.sleep(args.delay)

    total_min = (time.time() - start) / 60
    summary = (
        f"Done in {total_min:.1f} min.\n"
        f"Synced found and written: {found} | Plain text found and written: {found_plain} | "
        f"Not found: {notfound} | Errors: {errors} | Skipped (no tags): {skipped_no_tags}\n"
    )
    print("\n" + summary)

    Path(args.report).write_text(summary + "\n" + "\n".join(report_lines), encoding="utf-8")
    print(f"Full details in: {args.report}")


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser("fetch", help="Fetch missing lyrics via syncedlyrics.")
    parser.add_argument("root", help="Library root directory")
    parser.add_argument("--fetch", action="store_true",
                         help="Actually call syncedlyrics and write files. Without this, count only (no network).")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N missing tracks (for speed testing).")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between requests.")
    parser.add_argument("--allow-plain", action="store_true",
                         help="If no synced lyrics are found, also try plain text as a fallback, "
                              "saved as .txt (never .lrc, so it is never mistaken for synced).")
    parser.add_argument("--report", default="fetch_report.txt")
    parser.set_defaults(func=run)
