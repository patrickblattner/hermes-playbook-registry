# Hermes Playbook Registry

Lokaler REST-API-Service als geteilte Wissens-/Skill-Registry für mehrere Hermes-Agenten.
Backend: SQLite mit WAL-Mode und FTS5-Volltextsuche. Keine externen Abhängigkeiten.

## Architektur

```
┌─────────────┐     HTTP      ┌──────────────────────┐
│  Agent 1    │ ─────────────▶│                      │
└─────────────┘               │  Registry Service    │
                              │  (FastAPI + SQLite)  │
┌─────────────┐     HTTP      │                      │
│  Agent 2    │ ─────────────▶│                      │
└─────────────┘               └──────────────────────┘
                                       │
                                       ▼
                              ┌──────────────────────┐
                              │ /data/playbooks.db   │
                              │ (Docker Volume)      │
                              └──────────────────────┘
```

Promotion-Pipeline: Agent reicht **Kandidat** ein → andere Agenten **validieren** →
manuelle/automatische **Promotion** zu *verified*. Erst `verified` Playbooks werden
in der `consult`-Phase eines Agenten standardmäßig zurückgegeben.

## Robustheit

Synchroner Write-Pfad mit fünf gestaffelten Schutzmechanismen:

1. **WAL-Mode**: Reads und Writes blockieren sich nicht gegenseitig
2. **busy_timeout=5000**: SQLite wartet bis zu 5s nativ bei Lock-Contention
3. **Application-Level Retry mit Exponential Backoff**: 5 Versuche mit Jitter, falls
   das busy_timeout doch mal nicht reicht
4. **Idempotency-Keys**: Client kann gefahrlos retryen, kein Doppel-Insert
5. **fsync nach commit**: nach 201 Response sind Daten garantiert auf der Disk

Keine Background-Worker, keine zusätzliche Queue. Bewusst minimalistisch.

## Schneller Start

```bash
docker compose up -d --build playbook-registry

# Health-Check (von einem anderen Container im hermes-net):
curl http://playbook-registry:8000/health

# Logs:
docker compose logs -f playbook-registry
```

Zum Debuggen vom Host aus: in `docker-compose.yml` das `ports`-Mapping einkommentieren,
dann `curl http://localhost:8080/health`.

## API-Übersicht

| Methode | Pfad                                            | Zweck                                    |
|---------|-------------------------------------------------|------------------------------------------|
| GET     | `/health`                                       | Health-Check                             |
| POST    | `/playbooks/candidate`                          | Neuen Kandidaten einreichen              |
| GET     | `/playbooks/search?q=...&status=...&limit=...`  | Volltextsuche (FTS5)                     |
| GET     | `/playbooks/{id}`                               | Einzelner Playbook + Validierungen       |
| POST    | `/playbooks/{id}/validate`                      | Validierungs-Ergebnis melden             |
| POST    | `/playbooks/{id}/promote`                       | candidate → verified                     |
| GET     | `/playbooks/by-skill/{skill_id}/versions`       | Alle Versionen eines Skills              |

OpenAPI-Doku unter `/docs` (Swagger UI) und `/redoc`.

## Test-Workflow mit curl

```bash
REG=http://playbook-registry:8000

# 1. Kandidat einreichen (mit Idempotenz-Key — empfohlen!)
curl -X POST $REG/playbooks/candidate \
  -H "Content-Type: application/json" \
  -d '{
    "skill_id": "gcp-auth-workload-identity",
    "problem_domain": "gcp-authentication",
    "problem_description": "Service account access from container to GCP API",
    "approach": "Workload Identity Federation instead of long-lived keys",
    "content": "## Steps\n1. Configure WIF pool...\n2. ...",
    "author_agent": "agent-1",
    "metadata": {"latency_ms": 3200, "model_used": "claude-sonnet-4-6"},
    "idempotency_key": "550e8400-e29b-41d4-a716-446655440000"
  }'
# → {"id":1,"skill_id":"...","version":1,"status":"candidate","idempotent_replay":false}

# 2. Gleichen Request nochmal (z.B. nach Timeout) — kein Duplikat:
# selbe Antwort, idempotent_replay=true

# 3. Suchen
curl "$REG/playbooks/search?q=gcp+authentication&status=all&limit=5"

# 4. Validieren
curl -X POST $REG/playbooks/1/validate \
  -H "Content-Type: application/json" \
  -d '{
    "validator_agent": "agent-2",
    "success": true,
    "latency_ms": 2800,
    "model_used": "claude-opus-4-7",
    "notes": "worked first try",
    "idempotency_key": "660e8400-e29b-41d4-a716-446655440001"
  }'

# 5. Promoten
curl -X POST $REG/playbooks/1/promote

# 6. Vollansicht inkl. Validierungen
curl $REG/playbooks/1

# 7. Verschiedene Versionen vergleichen (= unterschiedliche Lösungen für dasselbe Problem)
curl $REG/playbooks/by-skill/gcp-auth-workload-identity/versions
```

## Direkter DB-Zugriff (Debug)

```bash
docker compose exec playbook-registry sqlite3 /data/playbooks.db
```

Nützliche Queries:
```sql
.headers on
.mode column

-- Was ist drin?
SELECT id, skill_id, version, status, author_agent FROM playbooks;

-- Welche Skills haben mehrere Versionen (= divergente Lösungen)?
SELECT skill_id, COUNT(*) AS n FROM playbooks GROUP BY skill_id HAVING n > 1;

-- Validierungs-Statistiken
SELECT * FROM playbook_stats;

-- WAL-Mode bestätigen
PRAGMA journal_mode;
```

## Konfiguration

### Registry-Service

| Variable           | Default              | Zweck                       |
|--------------------|----------------------|-----------------------------|
| `PLAYBOOK_DB_PATH` | `/data/playbooks.db` | Pfad zur SQLite-Datei       |

### Agenten

| Variable                | Beispiel                          | Zweck                  |
|-------------------------|-----------------------------------|------------------------|
| `PLAYBOOK_REGISTRY_URL` | `http://playbook-registry:8000`   | Registry-Service URL   |
| `AGENT_ID`              | `agent-1`                         | Eindeutige Agent-ID    |

## Empfehlung für Agent-seitige Implementierung

Auf der Agent-Seite sollte der HTTP-Client:

1. **Idempotency-Key generieren** (UUIDv4) bevor der Request gesendet wird
2. **Bei Timeout/Connection-Error retryen** (max 3x, exponential backoff) mit *demselben* Key
3. **Client-Timeout setzen** (z.B. 10s) damit der Agent nicht endlos blockiert

Beispiel (Python):
```python
import httpx, uuid, time

def submit_candidate(payload, max_retries=3):
    idem_key = str(uuid.uuid4())
    payload = {**payload, "idempotency_key": idem_key}
    for attempt in range(max_retries):
        try:
            r = httpx.post(f"{REGISTRY_URL}/playbooks/candidate",
                          json=payload, timeout=10.0)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException):
            if attempt == max_retries - 1: raise
            time.sleep(2 ** attempt)
```

## Phasen für die Implementierung mit Claude Code

1. **Phase 1**: `schema.sql` + `Dockerfile` + `main.py` mit nur `/health`.
2. **Phase 2**: `POST /playbooks/candidate` + `GET /playbooks/search` mit Retry + Idempotenz.
3. **Phase 3**: `POST /playbooks/{id}/validate` + `GET /playbooks/{id}` + `POST /playbooks/{id}/promote`.
4. **Phase 4**: `GET /playbooks/by-skill/{skill_id}/versions`.
5. **Phase 5**: `docker-compose.yml` finalisieren + Network testen.
6. **Phase 6** (separat): Hermes-Skills `consult-playbook-registry` und
   `submit-playbook-candidate` in den Agenten implementieren.

## Bewusst ausgeklammert (für später)

- Authentication (Stub-Vorbereitung im Code)
- Auto-Promotion-Regeln (z.B. nach N erfolgreichen Cross-Validierungen)
- Embeddings/Vector Search (FTS5 reicht erstmal)
- Backup/Restore (Litestream oder Cron-Job separat einrichten)
- Web-UI
- Persistent Queue (synchroner Write-Pfad ist robust genug)

## Backup-Empfehlung

Online-Backup ohne Service-Stop:

```bash
docker compose exec playbook-registry \
  sqlite3 /data/playbooks.db ".backup /data/backup-$(date +%Y%m%d).db"
```

Für continuous backup → [Litestream](https://litestream.io/) als Sidecar.
