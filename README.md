# Lyric Sync Toolkit

A self-hosted toolkit for fetching, aligning, and validating synced lyrics (`.lrc`) for a music library. Runs as a single Docker container with a small web UI on top of a seven-step command-line pipeline.

![status](https://img.shields.io/badge/status-active-brightgreen)

## Features

- **Web UI** — browse the library, run each pipeline step, watch live output, and open the full report, all from the browser. No terminal required for day-to-day use.
- **Whisper connection test** — a one-click check that the configured `whisper-asr-webservice` endpoint is reachable before running alignment, similar to the connection tests in the *ARR suite.
- **Visual folder picker** — navigate the mounted library and scope a run to a single artist or the whole collection, without typing paths.
- **Seven independent pipeline steps** — each one is a standalone subcommand with its own flags; run them individually, in any order, on any subfolder.
- **Safe by default** — every step that can modify or delete files requires an explicit flag (`--apply`, `--convert`, `--delete`, `--fetch`, etc.); without it, the step only writes a report describing what it would do.
- **CLI still available** — the web UI is a thin layer over `cli.py`; anything it can do can also be run directly inside the container.

## Pipeline

The steps are designed to run in this order, though each can be run on its own:

| Step | Command | Purpose |
|---|---|---|
| 1 | `fetch` | Downloads missing lyrics (synced preferred) for tracks with neither a `.lrc` nor a `.txt` file. |
| 2 | `relocate` | Moves and organizes lyrics files to match the toolkit's naming rules. |
| 3 | `detect-unsynced` | Finds `.lrc` files that are actually plain text or broken sync, and prepares them for re-alignment. |
| 4 | `verify-source` | Cross-checks lyrics against source metadata to catch mismatched or wrong-track lyrics. |
| 5 | `align` | Aligns plain-text lyrics to audio using a Whisper transcription endpoint. |
| 6 | `verify-duration` | Flags `.lrc` files whose last timestamp doesn't match the track's real duration. |
| 7 | `verify-density` | Flags `.lrc` files with clusters of lines timed unrealistically close together. |

## Quick start (Docker Compose)

```yaml
services:
  lyric-sync:
    image: ghcr.io/rubiklp/lyric-sync-toolkit:latest
    container_name: lyric-sync-toolkit
    restart: unless-stopped
    ports:
      - "${WEBUI_PORT:-8420}:8420"
    volumes:
      - ${MUSIC_PATH}:/music
      - ${APPDATA_PATH}:/data
```

```env
MUSIC_PATH=/path/to/your/music/library
APPDATA_PATH=/path/to/persistent/storage
WEBUI_PORT=8420
```

```
docker compose up -d
```

Then open `http://<host>:8420` in a browser.

## Quick start (Unraid)

Works well as a stack under the Compose Manager plugin, the same way as other self-hosted apps:

1. Docker → Compose → Add New Stack.
2. Paste the `docker-compose.yml` above into the compose editor.
3. Fill in `MUSIC_PATH`, `APPDATA_PATH`, and `WEBUI_PORT` in the stack's env file with real values for your system.
4. Compose Up.

## Configuration

| Variable | Description | Default |
|---|---|---|
| `MUSIC_PATH` | Host path to the music library. Mounted read-write — the toolkit tags, renames, and deletes `.lrc`/`.txt` files inside it. | *(required)* |
| `APPDATA_PATH` | Host path for persistent storage: `settings.json` and generated reports. | *(required)* |
| `WEBUI_PORT` | Port the web UI is exposed on. | `8420` |

The Whisper endpoint URL is not an environment variable — it's set from the Settings page in the UI and stored in `settings.json` under `APPDATA_PATH`, so it can be changed without restarting the container.

## Whisper endpoint

The `align` step needs a running [`whisper-asr-webservice`](https://github.com/ahmetoner/whisper-asr-webservice) instance reachable from the container. Enter its URL in Settings and use **Test** to confirm it responds before running alignment.

## Using the CLI directly

The web UI is an optional layer; every step is also a plain subcommand inside the container:

```
docker exec -it lyric-sync-toolkit python cli.py verify-density /music/SomeArtist --delete
docker exec -it lyric-sync-toolkit python cli.py --help
```

## Building from source

```
git clone https://github.com/RubikLP/lyric-sync-toolkit.git
cd lyric-sync-toolkit
docker build -t lyric-sync-toolkit .
```
