# Hermes Playbook Registry

Lokaler REST-API-Service als geteilte Wissens-/Skill-Registry für mehrere Hermes-Agenten.
Backend: SQLite mit WAL-Mode und FTS5-Volltextsuche. Keine externen Abhängigkeiten.

## Installation

### Empfohlen: Pre-built Images via Deploy-Skript

Pre-built Multi-Arch-Images (linux/amd64, linux/arm64) liegen auf
[ghcr.io](https://github.com/patrickblattner?tab=packages). Ein einziges
Skript zieht die Images, schreibt das `docker-compose.yml`, legt das
gemeinsame Network `hermes-net` an und startet alles:

```bash
# Wechsle vorher in das Verzeichnis, in dem das Projekt-Unterverzeichnis
# entstehen soll (z.B. ~/docker), dann:
cd ~/docker
curl -fsSL https://raw.githubusercontent.com/patrickblattner/hermes-playbook-registry/main/deploy.sh | bash
# → ~/docker/hermes-playbook-registry/  wird angelegt
```

Das ist der bevorzugte Weg — kein Source-Klon, kein lokaler Build, kein
Image-Bau. Idempotent: nochmal laufen aktualisiert auf den neuesten
`:latest`-Tag (`pull && up -d` recreated nur geänderte Container).

**Konfigurations-ENV (alle optional):**

| Variable        | Default                                | Zweck                                                                       |
|-----------------|----------------------------------------|-----------------------------------------------------------------------------|
| `INSTALL_DIR`   | `$PWD/hermes-playbook-registry`        | Wo `docker-compose.yml` und Daten landen (Default: Unterverzeichnis im aktuellen Pfad) |
| `REGISTRY_TAG`  | `latest`                               | Welches Image-Tag pullen (z.B. `v1.0`, sha)                                |
| `GHCR_USER`     | —                                      | GitHub-Username (nur falls Images privat)                                  |
| `GHCR_TOKEN`    | —                                      | PAT mit `read:packages` (nur falls privat)                                 |

```bash
# Beispiel: festes absolutes Ziel + fixiertes Tag
INSTALL_DIR=/opt/hermes-playbook-registry REGISTRY_TAG=latest bash deploy.sh
```

**Stop / Update / Cleanup:**

```bash
cd <dein-INSTALL_DIR>                                   # z.B. ~/docker/hermes-playbook-registry
docker compose logs -f                                  # Logs
docker compose down                                     # stoppen, Daten bleiben
docker compose down -v                                  # stoppen + Daten weg
INSTALL_DIR=$(pwd) bash deploy.sh                       # update auf :latest
```

**Wichtige Hinweise:**

- `deploy.sh` legt das Bridge-Network `hermes-net` einmal pro Host an. Externe
  Agent-Stacks hängen sich danach via `external: true` ins selbe Network —
  siehe [Architektur / Network-Setup](#network-setup-für-mehrere-agent-stacks)
  und `examples/hermes-agent-stack.yml`.
- Beide GHCR-Packages müssen einmalig nach dem ersten Build auf `public`
  gestellt werden (sonst braucht `deploy.sh` die `GHCR_*`-Variablen). Direktlink:
  `https://github.com/users/patrickblattner/packages/container/hermes-playbook-registry/settings`

### Alternative: aus dem Source-Tree bauen

Nur sinnvoll, wenn du Code-Änderungen lokal testen willst, ohne erst zu
pushen und auf den CI-Build zu warten.

```bash
git clone https://github.com/patrickblattner/hermes-playbook-registry
cd hermes-playbook-registry
docker network create hermes-net 2>/dev/null || true
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Tests gegen den lokalen Code (59 Tests, ~5s):

```bash
docker run --rm -v "$(pwd)":/app -w /app python:3.12 bash -c "
  apt-get update -qq && apt-get install -y -qq sqlite3 >/dev/null
  pip install -q -r requirements.txt -r mcp-server/requirements.txt -r tests/requirements.txt
  python -m pytest tests/ -v
"
```

Coverage:

| Datei                          | Was abgedeckt ist                                                |
|--------------------------------|------------------------------------------------------------------|
| `test_health.py`               | `/health` + Migrations-Marker                                    |
| `test_wilson.py`               | Score-Werte (1/1, 9/10, 100/100, …) + Threshold-Konsistenz       |
| `test_idempotency.py`          | Replay mit Original-Daten auch bei verändertem Body              |
| `test_lifecycle.py`            | Auto-Promote / Auto-Demote / Auto-Archive älterer Versionen      |
| `test_search.py`               | FTS5 OR/AND/Phrase, Wilson-Ranking, metadata-Roundtrip           |
| `test_concurrency.py`          | TOCTOU-Promote, parallele Version-Submits, parallele Idem        |
| `test_validation_errors.py`    | 404 / 409 / 422 für jeden Endpoint                               |
| `test_migrations.py`           | Idempotenz + Upgrade-Pfad gegen Legacy-DB                        |
| `test_scripts.py`              | `backup.sh` / `restore.sh` / `healthcheck.sh` als Subprocess     |
| `test_mcp_stdio.py`            | STDIO Tool-Discovery + Argument-Schema                           |
| `test_mcp_e2e.py`              | STDIO + HTTP roundtrip alle 6 Tools, Cross-Agent-Auto-Promote    |

## Architektur

```
        ┌───────────────────── Docker host ───────────────────────┐
        │                                                         │
        │  Registry-Stack (deploy.sh)                              │
        │  ┌──────────────────────────────────────────────┐       │
        │  │ playbook-registry      :8000  (REST/FTS5)    │       │
        │  │ playbook-registry-mcp  :8001  (MCP)          │       │
        │  └─────────────┬────────────────────────────────┘       │
        │                │                                        │
        │          hermes-net (bridge, kein Port-Mapping nach außen)
        │                │                                        │
        │  Agent-Stack (deine Compose, externer Stack)            │
        │  ┌─────────────┴────────────────────────────────┐       │
        │  │ hermes-agent-1  ruft mit as_agent="hermes"   │       │
        │  │ hermes-agent-2  ruft mit as_agent="hermine"  │       │
        │  └──────────────────────────────────────────────┘       │
        │                                                         │
        └─────────────────────────────────────────────────────────┘
                       ▼
        ┌──────────────────────┐
        │ /data/playbooks.db   │  (Docker Volume, WAL+FTS5)
        └──────────────────────┘
```

Zwei Container im Registry-Stack: **REST-Service** und **ein MCP-Wrapper**,
der von allen Agenten im `hermes-net` gemeinsam genutzt wird. Identität wird
pro Tool-Call übergeben (`as_agent`-Parameter, z.B. `"hermes"` oder
`"hermine"`); Trust kommt aus der Network-Isolation — niemand außerhalb von
`hermes-net` erreicht Registry oder MCP.

Promotion-Pipeline: Agent reicht **Kandidat** ein → andere Agenten
**validieren** → automatische **Promotion** zu *verified* (Cross-Validation
+ Wilson-Score-Schwelle, siehe Lifecycle-Sektion). Erst `verified`-Playbooks
werden in der Default-Search zurückgegeben.

### Network-Setup für mehrere Agent-Stacks

Die Registry und die Agenten laufen typischerweise als **getrennte
Compose-Stacks** auf demselben Docker-Host (Agenten haben oft eigene
Bauch-Konfiguration). Verbunden werden sie über ein gemeinsames Bridge-
Network mit festem Namen `hermes-net`:

- `deploy.sh` legt das Network einmalig an (`docker network create hermes-net`).
- Der Registry-Compose deklariert es als `external: true; name: hermes-net`.
- Jeder Agent-Stack deklariert es identisch und hängt seine Container rein.
- **Kein Port-Mapping nach außen** — die Registry ist ausschließlich aus
  Containern im selben Network erreichbar.

Hinweis zu `external: true`: das heißt **nicht** "von außen erreichbar",
sondern nur "wird außerhalb dieses Compose-Stacks gemanaged" (vom Host vor-
angelegt statt von Compose selbst). Die Isolation ist exakt die gleiche wie
bei einem stack-internen Netzwerk:

| Wer kann auf REST/MCP zugreifen?     | Erlaubt? |
|--------------------------------------|----------|
| Container im `hermes-net`            | ✅       |
| Andere Container auf demselben Host  | ❌       |
| Der Host selbst (`localhost:8000`)   | ❌ (kein `ports:`-Mapping) |
| Andere Maschinen im LAN              | ❌       |
| Internet                             | ❌       |

Konkretes Beispiel siehe `examples/hermes-agent-stack.yml` und
`examples/README.md`. Die Reihenfolge ist immer:

```bash
# 1. Registry-Stack (legt hermes-net an)
curl -fsSL https://raw.githubusercontent.com/patrickblattner/hermes-playbook-registry/main/deploy.sh | bash

# 2. Agent-Stack (referenziert hermes-net als external)
cd ~/my-hermes-agents/
docker compose up -d
```

`hermes-net` bleibt nach `docker compose down` bestehen — Stacks lassen
sich unabhängig stoppen/starten ohne dass die Verbindung kaputtgeht.

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
┌──────────────┐  MCP   │   shared)        │         │                 │
│ Hermine      ├───────▶└──────────────────┘         └─────────────────┘
└──────────────┘
```

Tool-Liste: `search_skills`, `get_skill`, `list_skill_versions`,
`publish_skill`, `rate_skill`, `promote_skill`.

Die Schreib-Tools (`publish_skill`, `rate_skill`) nehmen einen optionalen
`as_agent`-Parameter, der zur `author_agent` bzw. `validator_agent` im
Registry-Datenmodell wird. Wenn `as_agent` leer bleibt, fällt der Server auf
die ENV `DEFAULT_AGENT_ID` zurück (sinnvoll für Single-Agent-Setups).
Trust kommt aus der Netzwerk-Isolation: nur Container im `hermes-net` reden
mit dem MCP-Endpunkt.

### Modi

- **STDIO** (für In-Process-Nutzung neben einem Claude-Agent): in der
  Claude-Config registrieren mit `command="python", args=["mcp-server/server.py"]`,
  optional `env={DEFAULT_AGENT_ID, PLAYBOOK_REGISTRY_URL}`.
- **HTTP** (für Container-Deployment, Default in `docker-compose.yml`): MCP-Wrapper
  läuft als eigener Service im `hermes-net` und exponiert `streamable-http` auf
  Port 8001. Alle Agenten im Network reden über `http://playbook-registry-mcp:8001/mcp`.

### Aktivieren

```bash
docker compose up -d --build  # bringt registry + mcp hoch
```

Für einen zweiten Agent (Hermine): den auskommentierten Block in
`docker-compose.yml` aktivieren oder einen weiteren Container mit anderer
`AGENT_ID` starten.

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

### Variante: gleicher Workflow über den MCP-Wrapper

Statt `http://playbook-registry:8000` direkt anzusprechen, kann jeder Agent
auch über `http://playbook-registry-mcp:8001/mcp` reden — Tool-Schemas werden
von MCP-Clients automatisch entdeckt, und die Agent-Identität wird mit jedem
Tool-Call als `as_agent` mitgegeben. Beispiel mit dem Python-MCP-SDK:

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession

async with streamablehttp_client("http://playbook-registry-mcp:8001/mcp") as (r, w, _):
    async with ClientSession(r, w) as s:
        await s.initialize()
        await s.call_tool("publish_skill", {
            "skill_id": "gcp-auth-wif",
            "problem_domain": "gcp-authentication",
            "problem_description": "...",
            "approach": "WIF",
            "content": "## Steps\n1. ...",
            "as_agent": "hermes",
        })
        # ... rate_skill, search_skills, get_skill, list_skill_versions, promote_skill
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

### Registry-Service (`playbook-registry`)

| Variable           | Default              | Zweck                                       |
|--------------------|----------------------|---------------------------------------------|
| `PLAYBOOK_DB_PATH` | `/data/playbooks.db` | Pfad zur SQLite-Datei (Volume-mounted)      |

### MCP-Wrapper (`playbook-registry-mcp`)

| Variable                | Default                            | Zweck                                                                    |
|-------------------------|------------------------------------|--------------------------------------------------------------------------|
| `PLAYBOOK_REGISTRY_URL` | `http://playbook-registry:8000`    | Wo der MCP-Wrapper das REST-Backend findet                               |
| `MCP_TRANSPORT`         | `stdio`                            | `stdio` oder `http` (Container-Default im Image: `http`)                 |
| `MCP_PORT`              | `8001`                             | Port für streamable-http-Modus                                           |
| `DEFAULT_AGENT_ID`      | `anonymous`                        | Fallback-Identität wenn `as_agent` im Tool-Call leer ist (Single-Agent)  |

### Deploy-Skript (`deploy.sh`)

| Variable        | Default                                | Zweck                                                       |
|-----------------|----------------------------------------|-------------------------------------------------------------|
| `INSTALL_DIR`   | `$PWD/hermes-playbook-registry`        | Wo `docker-compose.yml` und Daten landen (Unterverzeichnis im aktuellen Pfad) |
| `REGISTRY_TAG`  | `latest`                               | Image-Tag (z.B. `v1.0`, sha)                                |
| `GHCR_USER`     | —                                      | GitHub-Username (nur falls Images privat)                   |
| `GHCR_TOKEN`    | —                                      | PAT mit `read:packages` (nur falls privat)                  |

### Operations-Skripte (`scripts/`)

| Variable           | Default                  | Skript                  | Zweck                                          |
|--------------------|--------------------------|-------------------------|------------------------------------------------|
| `BACKUP_DIR`       | `/data/backups`          | `backup.sh`             | Wo Backups hingelegt werden                    |
| `RETAIN`           | `24`                     | `backup.sh`             | Wieviele Backups behalten (Rotation)           |
| `HEALTH_URL`       | `http://localhost:8000/health` | `healthcheck.sh`  | Endpoint, der gepingt wird                     |
| `TIMEOUT`          | `5`                      | `healthcheck.sh`        | Sekunden bis curl aufgibt                      |

### Agent-seitige ENV (Convention)

Agents setzen typischerweise:

| Variable                | Beispiel                                         | Zweck                                      |
|-------------------------|--------------------------------------------------|--------------------------------------------|
| `PLAYBOOK_REGISTRY_URL` | `http://playbook-registry:8000`                  | für REST-Direkt-Calls                      |
| `MCP_REGISTRY_URL`      | `http://playbook-registry-mcp:8001/mcp`          | für MCP-Calls                              |
| `AGENT_ID`              | `hermes`                                         | wird vom Agent-Code als `as_agent`/Body-Feld weitergereicht |

`AGENT_ID` ist agent-internal — die Registry akzeptiert die Identität als `as_agent`-Tool-Parameter (MCP) bzw. `author_agent`/`validator_agent`-Body-Feld (REST).

## Empfehlung für Agent-seitige Implementierung

### REST direkt

Wenn der Agent den HTTP-Client schon hat: REST direkt verwenden, mit drei
einfachen Conventions:

1. **Idempotency-Key generieren** (UUIDv4) bevor der Request gesendet wird
2. **Bei Timeout/Connection-Error retryen** (max 3×, exponential backoff) mit *demselben* Key
3. **Client-Timeout setzen** (z.B. 10s) damit der Agent nicht endlos blockiert

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

### Via MCP-Wrapper (empfohlen für Claude-Agenten)

Wenn der Agent ohnehin MCP-Tools spricht (Hermes/Hermine), den Wrapper als
Tool-Server registrieren — dann gibt's Tool-Discovery automatisch und der
Agent muss keine REST-Schemata pflegen:

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession

async def consult_registry(query: str):
    async with streamablehttp_client(MCP_REGISTRY_URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await s.call_tool("search_skills", {"query": query})
```

Idempotency-Key wird vom Wrapper für jeden Schreib-Call automatisch
generiert. `as_agent` muss der Aufrufer pro Tool-Call setzen (oder im
Wrapper-Container `DEFAULT_AGENT_ID` als Fallback).

## Entwicklungs-Historie

Phasen-Reihenfolge, in der die Komponenten gebaut wurden — als Referenz für
neue Mitleser:

1. **Phase 1**: `Dockerfile` + `main.py` mit nur `/health`. Container baut + läuft.
2. **Phase 2**: `POST /playbooks/candidate` + `GET /playbooks/search` mit Retry + Idempotenz.
3. **Phase 3**: `POST /playbooks/{id}/validate` + `GET /playbooks/{id}` + `POST /playbooks/{id}/promote`.
4. **Phase 4**: `GET /playbooks/by-skill/{skill_id}/versions`.
5. **Phase 5**: `docker-compose.yml` finalisieren + Network testen.
6. **Phase 6**: Lifecycle und Bewertung — Wilson-Score-Ranking, Auto-Promote, Auto-Demote, Auto-Archive älterer Versionen.
7. **Phase 7**: MCP-Wrapper über die REST-API — agent-natives Interface.
8. **Phase 8**: Production-Readiness — pytest-Suite, Migrations-Tracking, Backup/Restore/Healthcheck-Skripte, GitHub-Actions-Build, pre-built Images via GHCR, `deploy.sh` als One-shot-Installer.
9. **Phase 9**: Single MCP-Container statt einer pro Agent (`as_agent`-Parameter pro Tool-Call), `python:3.12-slim` als Base (Image 1.67 GB → 290 MB), `.dockerignore`, GHA-paths-Whitelist, Test-Suite auf 59 Tests inkl. MCP-E2E ausgebaut.
10. **Phase 10** (separat, nicht Teil dieses Repos): Hermes-Skills `consult-playbook-registry` und `submit-playbook-candidate` in den Agenten implementieren.

## Bewusst ausgeklammert (für später)

- **Authentication** (Stub-Vorbereitung im Code, jetzt nicht aktiv — Trust kommt aus dem `hermes-net`-Network)
- **Time-Decay** über alte Validations (Skills altern bei Re-Use, nicht von selbst)
- **Embeddings / Vector Search** (FTS5 reicht erstmal)
- **Web-UI** (`/docs` Swagger reicht für Debugging)
- **Persistent Queue** (synchroner Write-Pfad mit Idempotency + Retry ist robust genug für 2–10 Agenten)
- **Multi-Replica / HA** (eine Instanz, ein Volume — für den geplanten Einsatzbereich ausreichend)

Für continuous backup → [Litestream](https://litestream.io/) als Sidecar.
