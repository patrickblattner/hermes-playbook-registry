# python:3.12 (NICHT slim) — wir brauchen FTS5, das im slim-Image
# manchmal nicht vorhanden ist. Der Größenunterschied ist in dieser Anwendung egal.
FROM python:3.12

WORKDIR /app

# Requirements zuerst (besseres Docker-Layer-Caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY main.py models.py schema.sql ./

# Data-Verzeichnis für die SQLite-DB
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Healthcheck: nutzt den /health-Endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
