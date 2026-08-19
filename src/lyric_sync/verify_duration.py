"""
Flag suspect .lrc files by comparing their last timestamp against the
track's real duration (from audio tags). Header tags like [ar:]/[ti:] are
absent from most .lrc files in practice, so they are not a usable
cross-check on their own - duration is a much more reliably-present signal.

Two failure shapes are distinguished:
  - "overshoot": the last timestamp exceeds the real duration by more than
    a small tolerance. This is a near-certainty, not a guess - a .lrc
    cannot have lyrics timed past the end of the track it belongs to, so
    this almost always means the file is for a different track entirely.
  - "coverage": the last timestamp falls well short of the real duration.
    This is a softer signal - it could mean wrong lyrics, but could also
    just be a long instrumental outro after the last sung line - so it is
    kept separate for manual review rather than auto-deleted.

Nothing is modified by default; --delete-overshoot removes only the
certain category.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from .common import AUDIO_EXTS, read_track_tags

TIMESTAMP_RE = re.compile(r"\[(\d+):(\d+)(?:\.(\d+))?\]")

OVERSHOOT_TOLERANCE_SEC = 15
MIN_COVERAGE_RATIO = 0.55
# Below this track length (skits, short intros), duration comparisons are too noisy to be useful.
MIN_TRACK_SECONDS_TO_CHECK = 45


def last_timestamp_seconds(lrc_path: Path):
    last = 0.0
    try:
        with open(lrc_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                for m in TIMESTAMP_RE.finditer(line):
                    minutes = int(m.group(1))
                    seconds = int(m.group(2))
                    frac = m.group(3) or "0"
                    frac_sec = float(f"0.{frac}")
                    total = minutes * 60 + seconds + frac_sec
                    if total > last:
                        last = total
    except Exception:
        return None
    return last if last > 0 else None


def run(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"No such directory: {root}")

    checked, skipped_short, no_timestamps, suspects = 0, 0, 0, []

    print("Scanning all .lrc files (duration comparison)...")
    count_scanned = 0
    for dirpath, _, filenames in os.walk(root):
        audio_by_stem = {
            Path(f).stem: Path(dirpath) / f
            for f in filenames
            if Path(f).suffix.lower() in AUDIO_EXTS
        }
        for f in filenames:
            if not f.lower().endswith(".lrc"):
                continue
            stem = Path(f).stem
            audio_path = audio_by_stem.get(stem)
            if not audio_path:
                continue  # orphaned .lrc, not this module's concern

            lrc_path = Path(dirpath) / f
            real_dur = read_track_tags(audio_path).duration_seconds
            if not real_dur or real_dur < MIN_TRACK_SECONDS_TO_CHECK:
                skipped_short += 1
                continue

            last_ts = last_timestamp_seconds(lrc_path)
            if last_ts is None:
                no_timestamps += 1
                continue

            checked += 1
            count_scanned += 1
            if count_scanned % 1000 == 0:
                print(f"  ...{count_scanned} checked")

            if last_ts > real_dur + OVERSHOOT_TOLERANCE_SEC:
                suspects.append((
                    lrc_path,
                    f"last .lrc timestamp ({last_ts:.0f}s) EXCEEDS the track's real duration "
                    f"({real_dur:.0f}s) - impossible for this track, almost certainly from a different song",
                    "overshoot",
                ))
            elif last_ts < real_dur * MIN_COVERAGE_RATIO:
                suspects.append((
                    lrc_path,
                    f"lyrics stop at {last_ts:.0f}s but the track runs {real_dur:.0f}s "
                    f"(only {last_ts/real_dur*100:.0f}% coverage) - check manually, possibly wrong track",
                    "coverage",
                ))

    lines = [
        f"Total .lrc checked: {checked}",
        f"Skipped (track too short, under {MIN_TRACK_SECONDS_TO_CHECK}s - skits/intros): {skipped_short}",
        f"No timestamp found in .lrc (empty/corrupt file?): {no_timestamps}",
        f"SUSPECT (possibly wrong): {len(suspects)}\n",
    ]
    for path, reason, category in suspects:
        lines.append(str(path))
        lines.append(f"    [{category}] {reason}")
        lines.append("")

    Path(args.report).write_text("\n".join(lines), encoding="utf-8")
    print(f"\nChecked: {checked} | Skipped (too short): {skipped_short} | No timestamp: {no_timestamps} | Suspect: {len(suspects)}")
    print(f"Details in: {args.report}")

    if args.delete_overshoot:
        print("\nDeleting the OVERSHOOT category (certain)...")
        deleted, kept = 0, 0
        for path, reason, category in suspects:
            if category == "overshoot":
                try:
                    path.unlink()
                    deleted += 1
                except Exception as e:
                    print(f"Error deleting {path}: {e}")
            else:
                kept += 1
        print(f"Deleted {deleted} confirmed files. Kept {kept} in the 'low coverage' category for manual review.")


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser(
        "verify-duration",
        help="Flag .lrc files whose timing is inconsistent with the track's real duration.",
    )
    parser.add_argument("root", help="Library root directory")
    parser.add_argument("--report", default="verify_duration_report.txt")
    parser.add_argument("--delete-overshoot", action="store_true",
                         help="Automatically delete only the OVERSHOOT category (timestamp past the "
                              "real duration - a certainty, not a guess). Never touches the low-coverage "
                              "category, which is left for manual review.")
    parser.set_defaults(func=run)
