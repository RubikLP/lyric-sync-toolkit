FROM python:3.12-slim

# Application code lives at /app; Python adds a script's own directory to
# sys.path automatically, so "from src.lyric_sync import ..." in cli.py,
# and the subprocess calls the web UI makes to cli.py, resolve without
# any extra PYTHONPATH setup.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cli.py .
COPY src/ ./src/
COPY webui/ ./webui/

# The web UI is the default long-running process, same pattern as the
# *ARR suite: one container, one port, everything else driven from the
# browser. It never modifies src/lyric_sync itself - it only runs cli.py
# as a subprocess and gives its --report output a persistent home under
# the /data volume (see webui/server.py).
EXPOSE 8420

CMD ["python", "-m", "uvicorn", "webui.server:app", "--host", "0.0.0.0", "--port", "8420"]
