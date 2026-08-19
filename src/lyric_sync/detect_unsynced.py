"""
Detect .lrc files that are not actually synced - either plain text saved
with a .lrc extension, or a broken export where every line carries the
same timestamp - and prepare them for re-alignment:

  1. Write the lyrics (timestamps stripped) to a new .txt next to the track.
  2. Rename the original .lrc to "<name>.lrc.bak" (never delete it).

Renaming rather than deleting matters for two reasons: it keeps a backup
in case the re-alignment turns out worse, and it changes what the
alignment step's file-matching sees - a stem still ending in ".lrc" would
be skipped as "already synced", so the rename is what actually lets the
track re-enter the pipeline.

Nothing is written unless --convert is passed; without it, this only
reports what it would do.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from .common import write_exclusive

TIMESTAMP_RE = re.compile(r"\[(\d+):(\d+)(?:\.(\d+))?\]")
# Header tags like [ar:...] [ti:...] [by:...] [length:...] - metadata, not timestamps or lyrics.
METADATA_TAG_RE = re.compile(r"^\[[a-zA-Z]+:.*\]$")


def strip_timestamps(line: str) -> str:
    return TIMESTAMP_RE.sub("", line).strip()


def analyze_lrc(lrc_path: Path):
    """
    Returns (is_unsynced, reason, plain_lines).
    plain_lines is the lyric text with timestamps stripped, ready to write to .txt.
    """
    try:
        raw = lrc_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return False, f"read error: {e}", []

    content_lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not content_lines:
        return False, "empty file", []

    all_timestamps = []
    lines_with_ts = 0
    plain_lines = []
    for line in content_lines:
        if METADATA_TAG_RE.match(line):
            continue  # header line, not a lyric
        matches = TIMESTAMP_RE.findall(line)
        if matches:
            lines_with_ts += 1
            for minutes, seconds, frac in matches:
                frac_sec = float(f"0.{frac}") if frac else 0.0
                all_timestamps.append(int(minutes) * 60 + int(seconds) + frac_sec)
        text = strip_timestamps(line)
        if text:
            plain_lines.append(text)

    if not plain_lines:
        return False, "no usable text lines (headers only?)", []

    if lines_with_ts == 0:
        return True, "no timestamps at all - plain text saved as .lrc", plain_lines

    distinct_ts = len(set(all_timestamps))
    if distinct_ts <= 1:
        return True, "every line shares the same timestamp - broken or missing sync", plain_lines

    return False, "looks properly synced", plain_lines


def find_pairs(root: Path):
    """Find every track that has a .lrc next to it (matching stem)."""
    from .common import AUDIO_EXTS

    pairs = []
    for dirpath, _, filenames in os.walk(root):
        lrc_by_stem = {Path(f).stem: Path(dirpath) / f for f in filenames if f.lower().endswith(".lrc")}
        for f in filenames:
            if Path(f).suffix.lower() not in AUDIO_EXTS:
                continue
            stem = Path(f).stem
            if stem in lrc_by_stem:
                pairs.append((Path(dirpath) / f, lrc_by_stem[stem]))
    return pairs


def run(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"No such directory: {root}")

    print("Scanning for audio+.lrc pairs...")
    pairs = find_pairs(root)
    print(f"Found {len(pairs)} tracks with a .lrc. Analyzing...\n")

    unsynced, synced_ok, converted, txt_already_existed, errors = [], 0, 0, 0, []

    for audio_path, lrc_path in pairs:
        is_unsynced, reason, plain_lines = analyze_lrc(lrc_path)
        if not is_unsynced:
            synced_ok += 1
            continue

        unsynced.append((lrc_path, reason))
        txt_path = audio_path.with_suffix(".txt")
        bak_path = Path(str(lrc_path) + ".bak")

        if not args.convert:
            continue

        try:
            content = "\n".join(plain_lines) + "\n"
            wrote = write_exclusive(txt_path, content)
            if not wrote:
                txt_already_existed += 1  # a .txt already exists - keep it, don't overwrite

            if bak_path.exists():
                errors.append(f"SKIPPED (backup already exists): {lrc_path}")
                continue

            lrc_path.rename(bak_path)
            converted += 1
        except Exception as e:
            errors.append(f"ERROR ({e}): {lrc_path}")

    lines = [
        f"Total tracks with .lrc: {len(pairs)}",
        f"Properly synced (untouched): {synced_ok}",
        f"UNSYNCED found: {len(unsynced)}",
    ]
    if args.convert:
        lines.append(f"Actually converted (.txt written + .lrc -> .lrc.bak): {converted}")
        lines.append(f"  of which .txt already existed (kept the old one, only renamed .lrc): {txt_already_existed}")
        lines.append(f"Errors: {len(errors)}")
    else:
        lines.append("DRY RUN - nothing was modified. Re-run with --convert to apply.")
    lines.append("")

    for lrc_path, reason in unsynced:
        lines.append(f"{lrc_path}\n    [{reason}]")
    if errors:
        lines.append("\n--- ERRORS ---")
        lines.extend(errors)

    Path(args.report).write_text("\n".join(lines), encoding="utf-8")

    print(f"Properly synced: {synced_ok} | Unsynced: {len(unsynced)}")
    if args.convert:
        print(f"Converted: {converted} | .txt already existed: {txt_already_existed} | Errors: {len(errors)}")
    else:
        print("Dry run - nothing modified. Add --convert to apply.")
    print(f"Details in: {args.report}")


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser(
        "detect-unsynced",
        help="Find .lrc files that are actually plain text (or broken sync) and prepare them for re-alignment.",
    )
    parser.add_argument("root", help="Library root directory (or a subfolder, for a quick test)")
    parser.add_argument("--convert", action="store_true",
                         help="Actually apply: write .txt + rename the .lrc to .lrc.bak. "
                              "Without this, dry-run only (report, nothing touched).")
    parser.add_argument("--report", default="detect_unsynced_report.txt")
    parser.set_defaults(func=run)
