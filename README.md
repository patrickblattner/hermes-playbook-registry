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

## Lifecycle und Bewertung

Search-Ranking läuft nicht über die rohe `success_rate`, sondern über den
**Wilson-Score-Lower-Bound** (95% Konfidenz). Das bestraft kleine Stichproben:
9/10 schlägt 1/1, 100/100 schlägt 9/10. Plus `avg_latency_ms` als Tiebreaker.

Lifecycle-Übergänge passieren als Side-Effect beim Validation-Insert:

- **Auto-Promote** (candidate → verified): wenn `external_success_count ≥ 2` UND
  `wilson_lower ≥ 0.4` (zündet bei 3/3 successes). `external_success_count` zählt
  nur Validations mit `validator_agent != author_agent` — niemand promotet sich selbst.
- **Auto-Demote** (* → archived): wenn `validation_count ≥ 3` UND `wilson_lower < 0.3`.
  Aus archived gibt's keinen Rückweg per API; wer reparieren will, submittet eine neue Version.
- **Auto-Archive supersedeter Versionen**: bei jedem Promote (manuell oder auto)
  werden alle anderen `verified`-Versionen desselben `skill_id` auf `archived` gesetzt.

Default-Annahme: die neueste verified-Version gewinnt. Skills, die niemand zweitens
nutzt, bleiben dauerhaft `candidate` — die Registry verspricht Qualität, nicht
Vollständigkeit.

## MCP-Wrapper

Zusätzlich zur REST-API gibt es einen dünnen MCP-Adapter (`mcp-server/`), der
die Registry agent-nativ verfügbar macht. REST bleibt Source of Truth.

```
┌──────────────┐  MCP   ┌──────────────────┐  HTTP   ┌─────────────────┐
│ Hermes-Agent ├───────▶│  mcp-wrapper     ├────────▶│ playbook-       │
└──────────────┘        │  (FastMCP,       │         │ registry (REST) │
┌──────────────┐  MCP   │   AGENT_ID env)  │         │                 │
│ Hermine      ├───────▶└──────────────────┘         └─────────────────┘
└──────────────┘
```

Tool-Liste: `search_skills`, `get_skill`, `list_skill_versions`,
`publish_skill`, `rate_skill`, `promote_skill`.

`author_agent` und `validator_agent` kommen serverseitig aus der ENV `AGENT_ID`
des MCP-Containers — der Client kann seine Identität nicht fälschen.

### Modi

- **STDIO** (Empfehlung für In-Process-Nutzung): in der Claude-Config des Agenten
  registrieren mit `command="python", args=["mcp-server/server.py"], env={AGENT_ID, PLAYBOOK_REGISTRY_URL}`.
- **HTTP** (für Container-Deployment, Default in `docker-compose.yml`): MCP-Wrapper
  läuft als eigener Service im `hermes-net` und exponiert `streamable-http` auf
  Port 8001. Andere Container reden via `http://playbook-registry-mcp-<agent>:8001/mcp`.

### Aktivieren

```bash
docker compose up -d --build  # bringt registry + mcp-hermes hoch
```

Für einen zweiten Agent (Hermine): den auskommentierten Block in
`docker-compose.yml` aktivieren oder einen weiteren Container mit anderer
`AGENT_ID` starten.

## Schneller Start (Production)

Pre-built Images sind als ghcr.io-Pakete verfügbar. Für ein neues Setup
genügt das Setup-Skript:

```bash
curl -fsSL https://raw.githubusercontent.com/patrickblattner/hermes-playbook-registry/main/setup.sh | bash
```

Default-Install-Pfad: `~/hermes-playbook-registry`. Anpassen via:

```bash
INSTALL_DIR=/opt/hermes-playbook-registry bash setup.sh
```

Das Skript ist idempotent — nochmal laufen aktualisiert auf den neuesten
Image-Tag (`pull && up -d` nur recreated geänderte Container).

Stop / Update:

```bash
cd ~/hermes-playbook-registry
docker compose logs -f                                  # Logs
docker compose down                                     # stoppen, Daten bleiben
docker compose down -v                                  # stoppen + Daten weg
INSTALL_DIR=~/hermes-playbook-registry bash setup.sh    # update
```

## Schneller Start (Development, aus Source)

Wer Code-Änderungen testen will, ohne erst pushen zu müssen, lädt zusätzlich
`docker-compose.dev.yml` als Override:

```bash
git clone https://github.com/patrickblattner/hermes-playbook-registry
cd hermes-playbook-registry
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Tests gegen den lokalen Code:

```bash
docker run --rm -v "$(pwd)":/app -w /app python:3.12 bash -c "
  pip install -q -r requirements.txt -r mcp-server/requirements.txt -r tests/requirements.txt
  python -m pytest tests/ -v
"
```

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

## Operations

### Backup (online, ohne Service-Stop)

```bash
docker exec playbook-registry /app/scripts/backup.sh
# Backups landen in /data/backups/playbooks-<UTC-timestamp>.db, Rotation behält
# die letzten RETAIN (Default 24) Stück. Override via ENV beim Aufruf:
#   docker exec -e RETAIN=168 playbook-registry /app/scripts/backup.sh
```

Stündlich automatisch via Host-Cron:

```bash
0 * * * * docker exec playbook-registry /app/scripts/backup.sh >> /var/log/playbook-backup.log 2>&1
```

### Restore (Service muss gestoppt sein)

```bash
docker compose stop playbook-registry
docker run --rm -v hermes-playbook-registry_playbook-data:/data \
  ghcr.io/patrickblattner/hermes-playbook-registry:latest \
  /app/scripts/restore.sh /data/backups/playbooks-20260509-120000Z.db
docker compose start playbook-registry
```

`restore.sh` legt vor jedem Restore eine Safety-Copy der aktuellen DB an, falls
das Backup doch nicht funktioniert.

### Health-Probe (für externes Monitoring)

```bash
docker exec playbook-registry /app/scripts/healthcheck.sh
# Exit 0 = ok, 1 = degraded (DB tot), 2 = HTTP unerreichbar
```

Beispiel als Cron-Alert:

```bash
*/5 * * * * docker exec playbook-registry /app/scripts/healthcheck.sh \
            || mail -s "playbook-registry health failed" ops@example.com
```

### DB-Migrationen

Schema-Änderungen kommen als nummerierte SQL-Files in `migrations/`
(z.B. `002_add_tags.sql`). Beim Container-Start werden alle Files in
aufsteigender Reihenfolge gegen die `_migrations`-Tracking-Tabelle abgeglichen
und nur die neuen ausgeführt — idempotent.

Existing-DBs sind sicher: alle bestehenden Tables bleiben (CREATE IF NOT
EXISTS), die erste Migration wird beim ersten Upgrade als applied markiert,
künftige Migrationen laufen normal.

### Direkter DB-Zugriff (Debug)

```bash
docker exec -it playbook-registry sqlite3 /data/playbooks.db
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
6. **Phase 6**: Lifecycle und Bewertung — Wilson-Score-Ranking, Auto-Promote,
   Auto-Demote, Auto-Archive älterer Versionen.
7. **Phase 7**: MCP-Wrapper über die REST-API — agent-natives Interface.
8. **Phase 8**: Production-Readiness — Tests (pytest), Migrations, Backup-Skripte,
   GitHub-Actions-Build, pre-built Images via GHCR, `setup.sh` als One-shot-Installer.
9. **Phase 9** (separat): Hermes-Skills `consult-playbook-registry` und
   `submit-playbook-candidate` in den Agenten implementieren.

## Bewusst ausgeklammert (für später)

- Authentication (Stub-Vorbereitung im Code)
- Time-Decay über alte Validations (Skills altern bei Re-Use, nicht von selbst)
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
