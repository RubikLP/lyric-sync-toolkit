FROM python:3.12-slim

# Application code lives at /app; Python adds a script's own directory to
# sys.path automatically, so "from src.lyric_sync import ..." in cli.py
# resolves without any extra PYTHONPATH setup.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cli.py .
COPY src/ ./src/

# The working directory is switched to /data (the mounted appdata volume)
# so that every subcommand's default --report path, which is a relative
# filename, lands in persistent storage instead of being lost when the
# container exits.
WORKDIR /data

ENTRYPOINT ["python", "/app/cli.py"]
CMD ["--help"]
