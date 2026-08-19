"""
Add real timestamps to plain-text lyrics (.txt, no timing information) by
transcribing the audio with a speech-to-text service (a whisper-asr-webservice
instance) and aligning the transcript to the known lyric text word by word.

This is deliberately NOT a simple "transcribe and hope it matches" approach.
The alignment is a global longest-common-subsequence match between the full
lyric word sequence and the full transcript word sequence, which means one
bad or ambiguous match somewhere in the middle cannot cascade and corrupt
everything that follows - each line gets the earliest matching word it can
find, and any line that finds nothing is interpolated between its nearest
matched neighbours.

Several failure modes were found and fixed empirically while running this
against a large, varied library, and are worth understanding because they
recur with different lyric sources:

  - Source text can repeat an entire song (or a whole section) verbatim,
    a scraping artifact rather than something actually sung twice. Left
    unhandled, the extra unmatched lines get crammed into whatever time
    is left before the track ends, producing lines a fraction of a
    second apart - technically "within the track's duration" and
    therefore invisible to a duration-only sanity check.
  - Conversely, "[Chorus] x2" style repeat markers describe a repetition
    that is often printed only once in the text but genuinely sung twice
    (or more) in the audio - the opposite problem, needing lines to be
    duplicated, not removed.
  - Section markers ("[Chorus]", "[Verse 1: Artist]") sometimes end up
    glued to the end of the previous line with no separating whitespace,
    a scraping artifact that leaves literal marker text as if it were
    sung lyrics.
  - Diacritics are a recurring source of false negatives for any language
    that has them: the same letter can arrive as different Unicode
    codepoints (visually identical, byte-for-byte different), or be
    dropped entirely by some sources - either way, direct string
    comparison against a transcript will miss real matches.
  - A source can simply be wrong - lyrics for a different song, or a
    version far removed from the actual recording (e.g. a live cut with
    a lot of unscripted content). Rather than writing a misleading result
    in these cases, alignment computes a confidence score (fraction of
    lyric words that found any real match at all) and refuses to write a
    .lrc below a configurable threshold.
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

SECTION_MARKER_RE = re.compile(r"^\[.*\]$")
REPEAT_MARKER_RE = re.compile(r"^\[?([^\[\]]*?)\]?\s*[x×]\s*(\d+)\s*$", re.IGNORECASE)
CONTRIBUTORS_RE = re.compile(r"^\d+\s+Contributors?$", re.IGNORECASE)
EMBEDDED_MARKER_RE = re.compile(r"\[[^\[\]]*\]")

# Chorus/hook blocks are typically short (3-6 lines). A block found after a
# repeat marker that exceeds this is more likely an unrelated verse written
# without brackets around its own heading than an actual short repeated
# chorus - the risk of tripling it by mistake outweighs the benefit, so
# blocks over this length are left as a single copy (see clean_lyric_lines).
MAX_REPEATABLE_BLOCK_LINES = 8


def normalize_word(w: str) -> str:
    w = strip_diacritics(w)
    return re.sub(r"[^\w]", "", w.lower())


def strip_embedded_markers(line: str) -> str:
    """
    Remove any "[...]" fragment left stuck to the rest of a line (no space
    or newline between them in the source) - e.g. "...last word[Chorus]"
    or "...last word; [Chorus] x2". SECTION_MARKER_RE / REPEAT_MARKER_RE
    only catch markers that make up an entire line; this covers the case
    where a marker ends up glued onto the previous line's last real word.
    Without this, the literal marker text would enter the word comparison
    and could never match anything in the transcript.
    """
    return EMBEDDED_MARKER_RE.sub("", line).strip(" ;,\t")


def dedupe_whole_song_repeat(raw_lines: list[str], min_ratio: float = 0.85) -> list[str]:
    """
    Some scraped lyric sources contain the entire song's lyrics twice in a
    row - an extraction artifact (the source page rendered twice), not a
    real repeat in the song. If the second half of the raw text is nearly
    identical to the first half, keep only the first half. This must run
    BEFORE any other cleanup or repeat-marker expansion, or a genuinely
    duplicated source would get doubled again.
    """
    n = len(raw_lines)
    if n < 8:
        return raw_lines
    half = n // 2
    first = "\n".join(raw_lines[:half]).lower()
    second = "\n".join(raw_lines[half : half * 2]).lower()
    ratio = difflib.SequenceMatcher(None, first, second).ratio()
    if ratio >= min_ratio:
        return raw_lines[:half] + raw_lines[half * 2 :]
    return raw_lines


def clean_lyric_lines(raw_lines: list[str]) -> list[str]:
    """
    Remove lines that are not actually sung: contributor counts and page
    titles left over from scraping, and section markers ("[Intro]",
    "[Chorus]", "[Verse 1]") that never appear in the audio.

    Special case: repeat markers ("[Chorus] x2", "Hook x3") show the text
    once but the song genuinely repeats that block N times in the audio.
    Treating the marker as simple noise to strip would leave a single
    copy of the lyrics for N real repeats in the transcript, causing an
    ambiguous match against whichever repeat happens to align best and
    leaving the rest unmatched (visible downstream as skipped/frozen
    lines in a karaoke-style display). The fix: read N from the marker
    and duplicate the following block of lines N times before alignment,
    so each real repeat gets its own line and its own timestamp.

    Safety net: some sources write the marker with NO text following it
    at all (a bare reference to a chorus shown earlier in the page), or
    the block that follows is unusually long. In either case, duplicating
    it is not safe - it could triple an unrelated verse that happens to
    lack brackets around its own heading - so nothing is duplicated in
    those cases, and the block is kept exactly as found (the pre-fix
    behaviour).
    """
    cleaned = []
    i = 0
    n = len(raw_lines)
    while i < n:
        line = raw_lines[i]

        if CONTRIBUTORS_RE.match(line):
            i += 1
            continue

        if i < 2 and line.endswith("Lyrics") and len(line.split()) <= 8:
            i += 1
            continue  # page title, e.g. "Artist - Song Lyrics"

        repeat_match = REPEAT_MARKER_RE.match(line)
        if repeat_match:
            count = max(int(repeat_match.group(2)), 1)
            i += 1
            block = []
            while i < n and not SECTION_MARKER_RE.match(raw_lines[i]) and not REPEAT_MARKER_RE.match(raw_lines[i]):
                cleaned_line = strip_embedded_markers(raw_lines[i])
                if cleaned_line:
                    block.append(cleaned_line)
                i += 1

            if block and len(block) <= MAX_REPEATABLE_BLOCK_LINES:
                for _ in range(count):
                    cleaned.extend(block)
            else:
                cleaned.extend(block)
            continue

        if SECTION_MARKER_RE.match(line):
            i += 1
            continue

        line = strip_embedded_markers(line)
        if line:
            cleaned.append(line)
        i += 1

    return cleaned


def transcribe_via_api(whisper_url: str, audio_path: Path, timeout: int) -> dict:
    """Send the audio file to whisper-asr-webservice and request word-level timestamps."""
    with open(audio_path, "rb") as f:
        resp = requests.post(
            f"{whisper_url.rstrip('/')}/asr",
            params={"output": "json", "word_timestamps": "true", "task": "transcribe"},
            files={"audio_file": (audio_path.name, f, "application/octet-stream")},
            timeout=timeout,
        )
    resp.raise_for_status()
    return resp.json()


def extract_word_segments(result: dict) -> list[dict]:
    """
    Flatten the API response into [{"word": ..., "start": ...}, ...].
    whisper-asr-webservice's exact response shape varies by configuration
    (a flat "segments" list of words, or "segments" containing nested
    "words"), so both are handled.
    """
    segments = result.get("segments", [])
    if not segments:
        return []
    if "words" in segments[0]:
        words = []
        for seg in segments:
            words.extend(seg.get("words", []))
        return words
    return segments


def align_lyrics_to_words(lyric_lines: list[str], word_segments: list[dict], real_duration: float | None = None):
    """
    Global (not greedy) alignment between the full lyric word sequence and
    the full transcript word sequence, via the longest common subsequence
    (difflib.SequenceMatcher over the whole sequence at once). This avoids
    a cascading failure mode: because matching does not require searching
    only after the previous match's position, one wrong or ambiguous match
    cannot corrupt everything that follows.

    Lines with no matched word are interpolated linearly between the
    nearest matched lines before and after them, rather than freezing on
    the previous timestamp.

    real_duration (seconds, optional): a hard ceiling - no timestamp is
    ever allowed to exceed the track's real duration. Without this, a gap
    at the very end of the lyrics (very common: a scraped source repeating
    a fade-out chorus more times than actually fit in the recording) would
    "invent" minutes of runtime past the track's real end.

    Returns (list of (line, timestamp) pairs, confidence score), where the
    confidence score is the fraction of lyric words that found any real
    match in the transcript at all - low values indicate wrong lyrics or a
    version too different from the actual recording to trust.
    """
    flat_lyric_words = []
    word_to_line = []
    for line_idx, line in enumerate(lyric_lines):
        for w in line.split():
            nw = normalize_word(w)
            if nw:
                flat_lyric_words.append(nw)
                word_to_line.append(line_idx)

    transcript_words = [normalize_word(w["word"]) for w in word_segments]

    matcher = difflib.SequenceMatcher(None, flat_lyric_words, transcript_words, autojunk=False)
    matching_blocks = matcher.get_matching_blocks()

    line_timestamps = {i: [] for i in range(len(lyric_lines))}
    for block in matching_blocks:
        if block.size == 0:
            continue
        for offset in range(block.size):
            lyric_word_idx = block.a + offset
            transcript_word_idx = block.b + offset
            line_idx = word_to_line[lyric_word_idx]
            ts = word_segments[transcript_word_idx].get("start")
            if ts is not None:
                line_timestamps[line_idx].append(ts)

    raw_ts = []
    for i in range(len(lyric_lines)):
        times = line_timestamps[i]
        raw_ts.append(min(times) if times else None)

    final_ts = list(raw_ts)
    n = len(final_ts)
    i = 0
    while i < n:
        if final_ts[i] is not None:
            i += 1
            continue
        start_gap = i
        while i < n and final_ts[i] is None:
            i += 1
        end_gap = i

        prev_ts = final_ts[start_gap - 1] if start_gap > 0 else 0.0
        if end_gap < n:
            next_ts = final_ts[end_gap]
        else:
            # Gap runs to the last line - nothing after it matched anything
            # real. Assume roughly 2.5s/line, but never past the track's
            # real duration (see docstring above).
            estimated = prev_ts + (end_gap - start_gap + 1) * 2.5
            next_ts = min(estimated, real_duration) if real_duration else estimated
        gap_len = end_gap - start_gap
        for j in range(gap_len):
            fraction = (j + 1) / (gap_len + 1)
            final_ts[start_gap + j] = prev_ts + (next_ts - prev_ts) * fraction

    for i in range(1, n):
        if final_ts[i] < final_ts[i - 1]:
            final_ts[i] = final_ts[i - 1]

    if real_duration:
        final_ts = [min(t, real_duration) for t in final_ts]

    matched_words = sum(block.size for block in matching_blocks)
    match_ratio = matched_words / len(flat_lyric_words) if flat_lyric_words else 0.0

    return list(zip(lyric_lines, final_ts)), match_ratio


def format_timestamp(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    return f"[{minutes:02d}:{secs:05.2f}]"


def find_targets(root: Path) -> list[Path]:
    """Every audio file that has a .txt sibling but no .lrc yet."""
    import os

    targets = []
    for dirpath, _, filenames in os.walk(root):
        stems_lrc = {Path(f).stem for f in filenames if f.lower().endswith(".lrc")}
        stems_txt = {Path(f).stem for f in filenames if f.lower().endswith(".txt")}
        for f in filenames:
            if Path(f).suffix.lower() not in AUDIO_EXTS:
                continue
            stem = Path(f).stem
            if stem in stems_txt and stem not in stems_lrc:
                targets.append(Path(dirpath) / f)
    return targets


def run(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"No such directory: {root}")

    print("Scanning for tracks with .txt but no .lrc...")
    targets = find_targets(root)
    if args.limit:
        targets = targets[: args.limit]
    print(f"Found {len(targets)} tracks to process.\n")

    # Report is opened in append mode and flushed after every line: if the
    # process dies partway through (crash, restart, container killed), the
    # progress made so far stays on disk instead of being lost along with
    # a summary that never got written. On a fresh run (e.g. after a
    # container restart), new lines are appended under a new run marker -
    # history from previous runs is kept, not overwritten.
    report_f = open(args.report, "a", encoding="utf-8")

    def log(line: str):
        print(line)
        report_f.write(line + "\n")
        report_f.flush()

    log(f"\n=== Run started: {time.strftime('%Y-%m-%d %H:%M:%S')} | {len(targets)} tracks found ===")

    found, failed = 0, 0
    start_time = time.time()

    for i, audio_path in enumerate(targets, 1):
        txt_path = audio_path.with_suffix(".txt")
        lrc_path = audio_path.with_suffix(".lrc")

        try:
            lyric_text = txt_path.read_text(encoding="utf-8", errors="ignore")
            lyric_lines = [l.strip() for l in lyric_text.splitlines() if l.strip()]
            lyric_lines = dedupe_whole_song_repeat(lyric_lines)
            lyric_lines = clean_lyric_lines(lyric_lines)
            if not lyric_lines:
                failed += 1
                log(f"  [{i}/{len(targets)}] FAILED (empty .txt after cleanup): {audio_path}")
                continue

            result = transcribe_via_api(args.whisper_url, audio_path, args.timeout)
            word_segments = extract_word_segments(result)
            if not word_segments:
                failed += 1
                log(f"  [{i}/{len(targets)}] FAILED (no words transcribed): {audio_path}")
                continue

            real_duration = read_track_tags(audio_path).duration_seconds
            aligned, match_ratio = align_lyrics_to_words(lyric_lines, word_segments, real_duration)

            if match_ratio < args.min_match_ratio:
                failed += 1
                log(f"  [{i}/{len(targets)}] FAILED (low confidence {match_ratio:.0%}, "
                    f"possibly wrong lyrics or a very different version): {audio_path}")
                continue

            lrc_lines = []
            last_ts = 0.0
            for line, ts in aligned:
                use_ts = ts if ts is not None else last_ts
                lrc_lines.append(f"{format_timestamp(use_ts)}{line}")
                last_ts = use_ts

            lrc_content = "\n".join(lrc_lines) + "\n"
            if write_exclusive(lrc_path, lrc_content):
                found += 1
                log(f"  [{i}/{len(targets)}] OK (confidence {match_ratio:.0%}): {lrc_path.name}")
            else:
                log(f"  [{i}/{len(targets)}] already exists, skipped: {lrc_path.name}")

        except Exception as e:
            failed += 1
            log(f"  [{i}/{len(targets)}] ERROR ({e}): {audio_path.name}")

        if i % 10 == 0:
            elapsed_min = (time.time() - start_time) / 60
            rate = elapsed_min / i
            eta_min = rate * (len(targets) - i)
            log(f"    ...progress: {found} ok, {failed} failed | ~{rate:.1f} min/track | ETA: {eta_min:.0f} min")

    total_min = (time.time() - start_time) / 60
    summary = f"Done in {total_min:.1f} min. Aligned successfully: {found} | Failed: {failed}"
    log(f"\n{summary}")
    report_f.close()
    print(f"Full details in: {args.report}")


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser(
        "align",
        help="Align plain-text lyrics to audio via a speech-to-text service.",
    )
    parser.add_argument("root", help="Library root directory")
    parser.add_argument("--whisper-url", required=True, help="URL of the whisper-asr-webservice instance, e.g. http://host:9000")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N tracks (for testing).")
    parser.add_argument("--timeout", type=int, default=600, help="Timeout in seconds per track (default 600).")
    parser.add_argument("--report", default="align_report.txt")
    parser.add_argument("--min-match-ratio", type=float, default=0.35,
                         help="Below this fraction (0-1) of lyric words actually matched, do NOT write a "
                              ".lrc - flagged as failed instead (default 0.35). Catches wrong lyrics or a "
                              "version too different from the actual recording.")
    parser.set_defaults(func=run)
