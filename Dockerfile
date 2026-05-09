# python:3.12-slim. FTS5 kommt aus der distro-libsqlite3 von Debian Bookworm,
# das ist in Python's sqlite3 Modul nutzbar. Slim spart ~1.4 GB gegenüber dem
# vollen python:3.12-Image.
FROM python:3.12-slim

WORKDIR /app

# sqlite3 CLI für Online-Backups (sqlite3 ".backup ...") und Debug-Zugriff.
# bash für die scripts/, curl für interne Healthchecks im HEALTHCHECK-Block.
RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 bash curl \
 && rm -rf /var/lib/apt/lists/*

# Requirements zuerst (besseres Docker-Layer-Caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY main.py models.py ./
COPY migrations ./migrations
COPY scripts ./scripts
RUN chmod +x ./scripts/*.sh

# Data-Verzeichnis für die SQLite-DB
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Healthcheck: nutzt den /health-Endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
