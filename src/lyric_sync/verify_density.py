"""
Flag .lrc files with "clusters" of consecutive lines timed unrealistically
close together - a sign that alignment had more lyric text left to place
than time actually remained, and had to cram the leftover lines into a
far shorter window than could plausibly have been sung that way.

This exists separately from verify_duration.py: that module only checks
the LAST timestamp against the track's real duration (overshoot/coverage).
A .lrc where the leftover text was crammed correctly INSIDE the track's
real duration - so the last timestamp looks like perfect coverage - passes
that check even though the content between timestamps is impossible to
sing at that pace.

A "suspect cluster" here means N consecutive lines (default 4) whose total
span (last - first) falls under a threshold (default 3 seconds) - well
under a second per line on average, too fast for real sung or spoken
lyrics regardless of genre.

Nothing is modified unless --delete is passed; this check is entirely
local (no audio transcription or network access needed).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TIMESTAMP_RE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")


def parse_lrc(lrc_path: Path):
    """Returns a list of (timestamp_seconds, text) for every timed line."""
    try:
        raw = lrc_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    entries = []
    for line in raw.splitlines():
        m = TIMESTAMP_RE.match(line.strip())
        if not m:
            continue
        minutes, seconds, text = m.groups()
        ts = int(minutes) * 60 + float(seconds)
        entries.append((ts, text.strip()))
    return entries


def find_dense_clusters(entries, cluster_size: int = 4, max_span: float = 3.0):
    """
    Look for windows of `cluster_size` consecutive lines whose total span
    is under `max_span` seconds. Returns a list of (start_index, span, count).
    """
    clusters = []
    n = len(entries)
    i = 0
    while i <= n - cluster_size:
        span = entries[i + cluster_size - 1][0] - entries[i][0]
        if span < max_span:
            # Extend the cluster as far as it stays under the proportional threshold.
            j = i + cluster_size
            while j < n and (entries[j][0] - entries[i][0]) < max_span * (j - i) / cluster_size:
                j += 1
            clusters.append((i, entries[j - 1][0] - entries[i][0], j - i))
            i = j
        else:
            i += 1
    return clusters


def run(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"No such directory: {root}")

    print("Scanning all .lrc files...")
    lrc_files = list(root.rglob("*.lrc"))
    print(f"Found {len(lrc_files)}. Checking density...\n")

    suspects = []
    deleted, delete_errors = 0, []
    for lrc_path in lrc_files:
        entries = parse_lrc(lrc_path)
        if len(entries) < args.cluster_size:
            continue
        clusters = find_dense_clusters(entries, args.cluster_size, args.max_span)
        if clusters:
            suspects.append((lrc_path, clusters))
            if args.delete:
                try:
                    lrc_path.unlink()
                    deleted += 1
                except Exception as e:
                    delete_errors.append(f"ERROR deleting ({e}): {lrc_path}")

    lines = [
        f"Total .lrc checked: {len(lrc_files)}",
        f"SUSPECT (dense clusters, threshold {args.max_span}s/{args.cluster_size} lines): {len(suspects)}",
    ]
    if args.delete:
        lines.append(f"Actually deleted: {deleted}")
        if delete_errors:
            lines.append(f"Deletion errors: {len(delete_errors)}")
    else:
        lines.append("DRY RUN - nothing deleted. Re-run with --delete to apply.")
    lines.append("")
    for lrc_path, clusters in suspects:
        lines.append(str(lrc_path))
        for start_idx, span, count in clusters:
            lines.append(f"    {count} lines in {span:.2f}s (starting at line #{start_idx + 1})")

    if delete_errors:
        lines.append("--- DELETION ERRORS ---")
        lines.extend(delete_errors)

    Path(args.report).write_text("\n".join(lines), encoding="utf-8")
    print(f"Suspect: {len(suspects)}")
    if args.delete:
        print(f"Deleted: {deleted} | Errors: {len(delete_errors)}")
    print(f"Details in: {args.report}")


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser(
        "verify-density",
        help="Flag .lrc files with lines timed unrealistically close together.",
    )
    parser.add_argument("root")
    parser.add_argument("--cluster-size", type=int, default=4, help="Consecutive lines checked together (default 4)")
    parser.add_argument("--max-span", type=float, default=3.0, help="Suspect threshold: total span under this many seconds (default 3.0)")
    parser.add_argument("--report", default="verify_density_report.txt")
    parser.add_argument("--delete", action="store_true",
                         help="Actually delete the suspect .lrc files found (they have a .txt next to "
                              "them, so they will be picked up again by the next align run). "
                              "Without this, dry-run only.")
    parser.set_defaults(func=run)
