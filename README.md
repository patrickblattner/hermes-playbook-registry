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

## Schneller Start

```bash
# 1. Service starten (nur den Registry-Container)
docker compose up -d --build playbook-registry

# 2. Health-Check
docker compose exec playbook-registry curl -s http://localhost:8000/health
# Erwartet: {"status":"ok","db":"connected","journal_mode":"wal"}

# 3. Logs prüfen
docker compose logs -f playbook-registry
```

Vom Host aus testen: in `docker-compose.yml` das `ports`-Mapping einkommentieren,
dann `curl http://localhost:8080/health`.

## API-Übersicht

| Methode | Pfad                                            | Zweck                                    |
|---------|-------------------------------------------------|------------------------------------------|
| GET     | `/health`                                       | Health-Check                             |
| POST    | `/playbooks/candidate`                          | Neuen Kandidaten einreichen              |
| GET     | `/playbooks/search?q=...&status=...&limit=...`  | Volltextsuche (FTS5)                     |
| GET     | `/playbooks/{id}`                               | Einzelnen Playbook + Validierungen lesen |
| POST    | `/playbooks/{id}/validate`                      | Validierungs-Ergebnis melden             |
| POST    | `/playbooks/{id}/promote`                       | candidate → verified                     |
| GET     | `/playbooks/by-skill/{skill_id}/versions`       | Alle Versionen eines Skills              |

OpenAPI-Doku verfügbar unter `/docs` (Swagger UI) und `/redoc`.

## Test-Workflow mit curl

```bash
# Innerhalb eines anderen Containers im hermes-net:
REG=http://playbook-registry:8000

# 1. Kandidat einreichen
curl -X POST $REG/playbooks/candidate \
  -H "Content-Type: application/json" \
  -d '{
    "skill_id": "gcp-auth-workload-identity",
    "problem_domain": "gcp-authentication",
    "problem_description": "Service account access from container to GCP API",
    "approach": "Workload Identity Federation instead of long-lived keys",
    "content": "## Steps\n1. Configure WIF pool...\n2. ...",
    "author_agent": "agent-1",
    "metadata": {"latency_ms": 3200, "model_used": "claude-sonnet-4-6"}
  }'
# → {"id":1,"skill_id":"...","version":1,"status":"candidate"}

# 2. Suchen
curl "$REG/playbooks/search?q=gcp+authentication&status=all&limit=5"

# 3. Validieren (Agent 2 hat es getestet)
curl -X POST $REG/playbooks/1/validate \
  -H "Content-Type: application/json" \
  -d '{
    "validator_agent": "agent-2",
    "success": true,
    "latency_ms": 2800,
    "model_used": "claude-opus-4-7",
    "notes": "worked first try"
  }'

# 4. Promoten
curl -X POST $REG/playbooks/1/promote

# 5. Vollansicht inkl. Validierungen
curl $REG/playbooks/1
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

Environment-Variablen für den Registry-Service:

| Variable           | Default              | Zweck                       |
|--------------------|----------------------|-----------------------------|
| `PLAYBOOK_DB_PATH` | `/data/playbooks.db` | Pfad zur SQLite-Datei       |

Für die Agenten:

| Variable                | Beispiel                          | Zweck                  |
|-------------------------|-----------------------------------|------------------------|
| `PLAYBOOK_REGISTRY_URL` | `http://playbook-registry:8000`   | Registry-Service URL   |
| `AGENT_ID`              | `agent-1`                         | Eindeutige Agent-ID    |

## Phasen für die Implementierung mit Claude Code

Wenn das hier einem Coding-Agent als Briefing übergeben wird, in dieser Reihenfolge bauen:

1. **Phase 1**: `schema.sql` + `Dockerfile` + `main.py` mit nur `/health`. Container baut + läuft.
2. **Phase 2**: `POST /playbooks/candidate` + `GET /playbooks/search`. Mit curl testen.
3. **Phase 3**: `POST /playbooks/{id}/validate` + `GET /playbooks/{id}` + `POST /playbooks/{id}/promote`.
4. **Phase 4**: `GET /playbooks/by-skill/{skill_id}/versions`.
5. **Phase 5**: `docker-compose.yml` finalisieren + Network testen.
6. **Phase 6** (separat, nicht Teil dieses Repos): Hermes-Skills `consult-playbook-registry`
   und `submit-playbook-candidate` in den Agenten implementieren.

## Bewusst ausgeklammert (für später)

- Authentication (Stub-Vorbereitung im Code)
- Auto-Promotion-Regeln (z.B. nach N erfolgreichen Cross-Validierungen)
- Embeddings/Vector Search (FTS5 reicht erstmal)
- Backup/Restore (Litestream oder Cron-Job separat einrichten)
- Web-UI

## Backup-Empfehlung

SQLite-Backup ohne Service-Stop:

```bash
docker compose exec playbook-registry \
  sqlite3 /data/playbooks.db ".backup /data/backup-$(date +%Y%m%d).db"
```

Für continuous backup → [Litestream](https://litestream.io/) als Sidecar empfehlenswert.
