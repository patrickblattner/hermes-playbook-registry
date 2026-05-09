FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py AGENT_GUIDE.md ./

# HTTP-Modus per Default — sinnvoll für Container-Deployment.
# Für In-Process-Nutzung neben einem Claude-Agent: MCP_TRANSPORT=stdio setzen
# und den Container als Subprozess (oder das Script direkt) starten.
ENV MCP_TRANSPORT=http
ENV MCP_PORT=8001

EXPOSE 8001

CMD ["python", "server.py"]
