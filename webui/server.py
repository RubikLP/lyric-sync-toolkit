"""
Web UI backend for the lyric-sync toolkit.

This module never imports or modifies anything in src/lyric_sync. It only
orchestrates the existing cli.py as a subprocess, the same way a person
would from a terminal. That keeps the already-verified pipeline logic
completely untouched - the web UI is a thin, replaceable layer on top.

Paths are configurable via environment variables so this can be tested
outside of the real container:
  MUSIC_ROOT  - library root the folder browser and jobs are scoped to (default /music)
  DATA_ROOT   - persistent storage for settings.json and reports/ (default /data)
  CLI_PATH    - path to cli.py (default /app/cli.py)
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

MUSIC_ROOT = Path(os.environ.get("MUSIC_ROOT", "/music")).resolve()
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data")).resolve()
CLI_PATH = os.environ.get("CLI_PATH", "/app/cli.py")

SETTINGS_PATH = DATA_ROOT / "settings.json"
REPORTS_DIR = DATA_ROOT / "reports"

app = FastAPI(title="Lyric Sync Toolkit")


# ---------------------------------------------------------------------------
# Step definitions - the single source of truth for which flags exist on
# each cli.py subcommand. The frontend renders forms purely from GET
# /api/steps, so there is no second place where a flag name could drift
# out of sync with what cli.py actually accepts.
# ---------------------------------------------------------------------------

STEPS = [
    {
        "id": "fetch",
        "label": "Fetch Lyrics",
        "description": "Downloads missing lyrics (synced preferred) for tracks that have neither a .lrc nor a .txt file.",
        "requires_whisper": False,
        "fields": [
            {"name": "fetch", "type": "bool_flag", "flag": "--fetch", "label": "Actually fetch (unchecked = count only, no network call)", "default": False},
            {"name": "allow_plain", "type": "bool_flag", "flag": "--allow-plain", "label": "Allow plain-text fallback if no synced lyrics are found", "default": False},
            {"name": "limit", "type": "int", "flag": "--limit", "label": "Limit to first N tracks (leave empty for all)", "default": None},
            {"name": "delay", "type": "float", "flag": "--delay", "label": "Delay between requests (seconds)", "default": 1.0},
        ],
    },
    {
        "id": "relocate",
        "label": "Relocate",
        "description": "Moves and organizes tracks/lyrics files according to the toolkit's naming rules.",
        "requires_whisper": False,
        "fields": [
            {"name": "apply", "type": "bool_flag", "flag": "--apply", "label": "Apply changes (unchecked = dry run)", "default": False},
            {"name": "delete_safe_leftovers", "type": "bool_flag", "flag": "--delete-safe-leftovers", "label": "Delete safe leftovers", "default": False},
        ],
    },
    {
        "id": "detect-unsynced",
        "label": "Detect Unsynced",
        "description": "Finds .lrc files that are actually plain text or broken sync, and prepares them for re-alignment.",
        "requires_whisper": False,
        "fields": [
            {"name": "convert", "type": "bool_flag", "flag": "--convert", "label": "Convert (write .txt + rename .lrc to .lrc.bak). Unchecked = dry run", "default": False},
        ],
    },
    {
        "id": "verify-source",
        "label": "Verify Source",
        "description": "Cross-checks lyrics against source metadata to catch mismatched or wrong-track lyrics.",
        "requires_whisper": False,
        "fields": [
            {"name": "mode", "type": "select", "flag": "--mode", "options": ["new", "full"], "default": "new", "label": "Mode"},
            {"name": "apply", "type": "bool_flag", "flag": "--apply", "label": "Apply changes", "default": False},
            {"name": "delete_suspect", "type": "bool_flag", "flag": "--delete-suspect", "label": "Delete suspect files", "default": False},
            {"name": "rewrite_suspect", "type": "bool_flag", "flag": "--rewrite-suspect", "label": "Rewrite suspect files", "default": False},
            {"name": "skip_genius", "type": "bool_flag", "flag": "--skip-genius", "label": "Skip Genius (instrumental detection)", "default": False},
            {"name": "min_similarity", "type": "float", "flag": "--min-similarity", "label": "Min similarity", "default": 0.35},
            {"name": "delay", "type": "float", "flag": "--delay", "label": "Delay between requests (seconds)", "default": 0.5},
            {"name": "timeout", "type": "int", "flag": "--timeout", "label": "Timeout (seconds)", "default": 10},
            {"name": "limit", "type": "int", "flag": "--limit", "label": "Limit to first N tracks (leave empty for all)", "default": None},
        ],
    },
    {
        "id": "align",
        "label": "Align",
        "description": "Aligns plain-text lyrics to audio using the Whisper endpoint configured in Settings.",
        "requires_whisper": True,
        "fields": [
            {"name": "limit", "type": "int", "flag": "--limit", "label": "Limit to first N tracks (leave empty for all)", "default": None},
            {"name": "timeout", "type": "int", "flag": "--timeout", "label": "Timeout per track (seconds)", "default": 600},
            {"name": "min_match_ratio", "type": "float", "flag": "--min-match-ratio", "label": "Min match ratio", "default": 0.35},
        ],
    },
    {
        "id": "verify-duration",
        "label": "Verify Duration",
        "description": "Flags .lrc files whose last timestamp doesn't match the track's real duration.",
        "requires_whisper": False,
        "fields": [
            {"name": "delete_overshoot", "type": "bool_flag", "flag": "--delete-overshoot", "label": "Delete the overshoot (certain) category automatically", "default": False},
        ],
    },
    {
        "id": "verify-density",
        "label": "Verify Density",
        "description": "Flags .lrc files with clusters of lines timed unrealistically close together.",
        "requires_whisper": False,
        "fields": [
            {"name": "cluster_size", "type": "int", "flag": "--cluster-size", "label": "Cluster size", "default": 4},
            {"name": "max_span", "type": "float", "flag": "--max-span", "label": "Max span (seconds)", "default": 3.0},
            {"name": "delete", "type": "bool_flag", "flag": "--delete", "label": "Delete suspect files", "default": False},
        ],
    },
]
STEPS_BY_ID = {s["id"]: s for s in STEPS}


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

def get_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_settings(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


class SettingsRequest(BaseModel):
    whisper_url: str


@app.get("/api/settings")
def read_settings():
    s = get_settings()
    return {"whisper_url": s.get("whisper_url", "")}


@app.post("/api/settings")
def write_settings(req: SettingsRequest):
    save_settings({"whisper_url": req.whisper_url.strip()})
    return {"whisper_url": req.whisper_url.strip()}


# ---------------------------------------------------------------------------
# Whisper "Test Connection"
# ---------------------------------------------------------------------------

class TestWhisperRequest(BaseModel):
    url: str


@app.post("/api/test-whisper")
def test_whisper(req: TestWhisperRequest):
    url = req.url.strip().rstrip("/")
    if not url:
        return {"ok": False, "detail": "URL is empty."}
    start = time.time()
    try:
        resp = requests.get(f"{url}/docs", timeout=5)
        elapsed = time.time() - start
        if resp.status_code < 500:
            return {"ok": True, "detail": f"Reachable (HTTP {resp.status_code}) in {elapsed:.2f}s"}
        return {"ok": False, "detail": f"Server responded with error {resp.status_code}"}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "detail": "Connection refused or host unreachable."}
    except requests.exceptions.Timeout:
        return {"ok": False, "detail": "Timed out after 5s."}
    except Exception as e:
        return {"ok": False, "detail": f"Error: {e}"}


# ---------------------------------------------------------------------------
# Folder browser, scoped to MUSIC_ROOT
# ---------------------------------------------------------------------------

def resolve_music_path(relative: str) -> Path:
    relative = (relative or "").strip().lstrip("/")
    candidate = (MUSIC_ROOT / relative).resolve()
    try:
        candidate.relative_to(MUSIC_ROOT)
    except ValueError:
        raise HTTPException(400, "Path escapes the music library root.")
    return candidate


@app.get("/api/browse")
def browse(path: str = ""):
    if not MUSIC_ROOT.exists():
        return {"path": "", "parent": None, "entries": [], "error": f"{MUSIC_ROOT} is not mounted."}

    target = resolve_music_path(path)
    if not target.is_dir():
        raise HTTPException(400, f"Not a directory: {target}")

    entries = sorted(
        (d.name for d in target.iterdir() if d.is_dir() and not d.name.startswith(".")),
        key=str.lower,
    )
    rel = "" if target == MUSIC_ROOT else str(target.relative_to(MUSIC_ROOT))
    parent = None
    if target != MUSIC_ROOT:
        parent_dir = target.parent
        parent = "" if parent_dir == MUSIC_ROOT else str(parent_dir.relative_to(MUSIC_ROOT))
    return {"path": rel, "parent": parent, "entries": entries}


# ---------------------------------------------------------------------------
# Job execution - runs cli.py as a subprocess, one job at a time system-wide
# (several steps touch the same files, so overlapping runs are refused
# rather than risking a race between two scripts editing the same track).
# ---------------------------------------------------------------------------

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
CURRENT_JOB_ID: Optional[str] = None


def build_argv(subcommand: str, root_abs: str, values: dict, report_abs: str) -> list[str]:
    step = STEPS_BY_ID[subcommand]
    argv = ["python", CLI_PATH, subcommand, root_abs]
    for field in step["fields"]:
        val = values.get(field["name"])
        if field["type"] == "bool_flag":
            if val:
                argv.append(field["flag"])
        elif field["type"] in ("int", "float"):
            if val is not None and val != "":
                argv += [field["flag"], str(val)]
        elif field["type"] == "select":
            if val:
                argv += [field["flag"], str(val)]
    if step["requires_whisper"]:
        argv += ["--whisper-url", get_settings().get("whisper_url", "")]
    argv += ["--report", report_abs]
    return argv


def run_job(job_id: str, argv: list[str]) -> None:
    global CURRENT_JOB_ID
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
    try:
        proc = subprocess.Popen(
            argv, cwd=str(DATA_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            with JOBS_LOCK:
                out = JOBS[job_id]["output"]
                out.append(line.rstrip("\n"))
                if len(out) > 1000:
                    JOBS[job_id]["output"] = out[-1000:]
        proc.wait()
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done" if proc.returncode == 0 else "failed"
            JOBS[job_id]["returncode"] = proc.returncode
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["output"].append(f"ERROR launching process: {e}")
    finally:
        with JOBS_LOCK:
            CURRENT_JOB_ID = None


class JobRequest(BaseModel):
    subcommand: str
    root: str = ""
    values: dict = {}


@app.get("/api/steps")
def list_steps():
    return STEPS


@app.get("/api/current-job")
def current_job():
    with JOBS_LOCK:
        return {"job_id": CURRENT_JOB_ID}


@app.post("/api/jobs")
def create_job(req: JobRequest):
    global CURRENT_JOB_ID
    with JOBS_LOCK:
        if CURRENT_JOB_ID is not None:
            raise HTTPException(409, "A pipeline step is already running. Wait for it to finish.")

    step = STEPS_BY_ID.get(req.subcommand)
    if not step:
        raise HTTPException(404, f"Unknown step: {req.subcommand}")

    target = resolve_music_path(req.root)
    if not target.is_dir():
        raise HTTPException(400, f"Not a directory: {target}")

    if step["requires_whisper"] and not get_settings().get("whisper_url"):
        raise HTTPException(400, "Set the Whisper URL in Settings first.")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:8]
    report_name = f"{req.subcommand}_{job_id}.txt"
    report_abs = str(REPORTS_DIR / report_name)
    argv = build_argv(req.subcommand, str(target), req.values or {}, report_abs)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "subcommand": req.subcommand, "status": "starting", "output": [],
            "returncode": None, "report_name": report_name,
        }
        CURRENT_JOB_ID = job_id

    threading.Thread(target=run_job, args=(job_id, argv), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        return {
            "subcommand": job["subcommand"],
            "status": job["status"],
            "output": "\n".join(job["output"]),
            "returncode": job["returncode"],
            "report_name": job["report_name"],
        }


@app.get("/api/reports/{name}")
def get_report(name: str):
    safe_name = Path(name).name  # strip any path components, defends against traversal
    path = REPORTS_DIR / safe_name
    if not path.exists():
        raise HTTPException(404, "Report not found.")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="ignore"))


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
