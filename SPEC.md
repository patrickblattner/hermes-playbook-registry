# Hermes Playbook Registry — Specification

## Zweck

Lokaler REST-API-Service als zentrale Skill-/Playbook-Registry für mehrere
Hermes-Agenten. Agenten teilen verifiziertes Wissen über funktionierende
Lösungsansätze, ohne direkt miteinander zu kommunizieren.

Use Case: Agent 1 löst ein GCP-Auth-Problem in 3s, Agent 2 hängt bei 30s. Statt sich
zu chatten, schreibt Agent 1 seinen Lösungsweg als Kandidat in die Registry. Nach
Cross-Validation wird er zum verifizierten Playbook promotet, das Agent 2 vor seinem
nächsten ähnlichen Task konsultiert.

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

Drei Container in einem gemeinsamen Docker-Network. Nur der Registry-Service spricht
mit der SQLite-Datenbank. Keine externen Abhängigkeiten (kein Git, kein Cloud-Service).

## Concurrency und Robustheit

Bewusste Design-Entscheidung: **synchroner Write-Pfad ohne zusätzliche Queue**. Agent
sendet POST → Service inserted in SQLite → 201 Antwort. Punkt. Keine Background-Worker,
keine bewegliche Teile.

### Schutzmechanismen (gestaffelt)

**Layer 1 — SQLite WAL-Mode**
Reads blockieren Writes nicht und umgekehrt. Mehrere Reader, ein Writer parallel —
ohne gegenseitige Wartezeit auf File-Ebene.

**Layer 2 — busy_timeout=5000 (in C, nativ)**
Wenn doch zwei Writes gleichzeitig kommen: SQLite serialisiert sie. Der zweite wartet
bis zu 5 Sekunden auf das Lock. Eine Write-Transaktion dauert <5ms, also könnten in
diesem Fenster theoretisch ~1000 sequenzielle Writes durchlaufen.

**Layer 3 — execute_with_retry mit Exponential Backoff**
Falls busy_timeout doch mal nicht reicht (extrem unwahrscheinlich bei 2-3 Agenten),
greift unser application-level Retry: 5 Versuche mit Backoff 50ms, 100ms, 200ms,
400ms, 800ms (+ Jitter). Insgesamt ~1.5s zusätzliche Wartezeit über alle Retries.

**Layer 4 — Idempotency-Keys**
Jeder Write-Endpoint akzeptiert ein optionales `idempotency_key`-Feld. Wird derselbe
Key zweimal gesendet → kein Doppel-Insert, sondern dieselbe Antwort wie beim ersten
Mal. Erlaubt dem Agenten, gefahrlos zu retryen, falls er ein Timeout sieht.

**Layer 5 — Persistenz nach 201**
SQLite mit synchronous=NORMAL macht fsync zu jedem WAL-Checkpoint. Wenn der Agent
einen 201 erhalten hat, ist die Information garantiert auf der Disk — auch wenn der
Container 1ms später hart abstürzt.

### Was passiert wann

| Szenario | Was passiert |
|----------|--------------|
| Normale Submits | <10ms Latenz, kein Lock-Contention sichtbar |
| Zwei Agenten submitten zeitgleich | Einer geht durch, anderer wartet ~5ms via busy_timeout, geht dann durch |
| Service-Crash während Insert | Wenn Agent kein 201 sah → Client retry mit gleichem idempotency_key. Wenn er 201 sah → Daten sind durch fsync auf der Disk. |
| Disk full | INSERT failed → Service gibt 500 → Agent kann später retryen |
| DB korrupt | Service /health wird degraded → Container restart oder manuelle Recovery |

## Technische Constraints

- Python 3.12 (NICHT slim — wir brauchen FTS5)
- FastAPI + Uvicorn
- SQLite mit WAL-Mode (essentiell)
- Ausschließlich Python's stdlib `sqlite3` — KEIN SQLAlchemy, KEIN ORM
- Pydantic v2 für Request/Response-Modelle
- Eine SQLite-Connection pro Request via FastAPI Dependency Injection
- `check_same_thread=False`, `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`
- Volltextsuche über SQLite **FTS5**
- Für den Anfang alles in einer einzigen `main.py`
- Keine externe Auth — beide Agenten sind im selben Docker-Network und vertrauenswürdig

## Datenmodell

### Tabelle `playbooks`

| Spalte               | Typ        | Notiz                                                    |
|----------------------|------------|----------------------------------------------------------|
| id                   | INTEGER PK | autoincrement                                            |
| skill_id             | TEXT       | z.B. "gcp-auth-workload-identity"                        |
| version              | INTEGER    | beginnt bei 1, increments pro skill_id                   |
| status               | TEXT       | 'candidate' \| 'verified' \| 'archived'                  |
| problem_domain       | TEXT       | z.B. "gcp-authentication"                                |
| problem_description  | TEXT       | menschenlesbare Problembeschreibung                      |
| approach             | TEXT       | kurze Beschreibung des Lösungsansatzes                   |
| content              | TEXT       | das eigentliche Playbook (Markdown/Code)                 |
| author_agent         | TEXT       | z.B. "agent-1"                                           |
| created_at           | TIMESTAMP  | DEFAULT CURRENT_TIMESTAMP                                |
| promoted_at          | TIMESTAMP  | NULL bis zur Promotion                                   |
| metadata             | JSON-TEXT  | flexibel: latency_ms, model_used, tags, etc.             |
| idempotency_key      | TEXT       | optional, vom Client                                     |

UNIQUE constraint auf (skill_id, version). Partial-UNIQUE-Index auf
idempotency_key WHERE NOT NULL.

### Tabelle `validations`

| Spalte           | Typ        | Notiz                                                    |
|------------------|------------|----------------------------------------------------------|
| id               | INTEGER PK | autoincrement                                            |
| playbook_id      | INTEGER    | FK auf playbooks.id, ON DELETE CASCADE                   |
| validator_agent  | TEXT       | z.B. "agent-2"                                           |
| success          | BOOLEAN    | hat's funktioniert?                                      |
| latency_ms       | INTEGER    | Antwortzeit in ms, NULL erlaubt                          |
| model_used       | TEXT       | welches LLM wurde verwendet, NULL erlaubt                |
| notes            | TEXT       | freitext, NULL erlaubt                                   |
| validated_at     | TIMESTAMP  | DEFAULT CURRENT_TIMESTAMP                                |
| idempotency_key  | TEXT       | optional, vom Client                                     |

### FTS5 Virtual Table `playbooks_fts`

Volltextindex über `skill_id`, `problem_domain`, `problem_description`, `approach`, `content`.
Mit Triggern (AFTER INSERT, AFTER UPDATE, AFTER DELETE) automatisch synchron mit `playbooks`.

### View `playbook_stats`

Aggregierte Validierungs-Statistiken pro Playbook:
- `validation_count`, `success_count`, `success_rate`, `avg_latency_ms`
- `external_success_count` — nur Validations mit `validator_agent != author_agent`
  (Cross-Validation-Grundlage, siehe Lifecycle-Regeln)
- `distinct_validators`

Plus die SQL-Funktion `wilson_lower(success_count, validation_count)` (per
`conn.create_function` registriert), die das untere 95%-Konfidenzintervall der
wahren Erfolgsrate liefert. Sortierungs- und Lifecycle-Schwellen gehen darüber,
nicht über die rohe `success_rate`.

## Lifecycle und Bewertung

Die Registry verspricht **Qualität**, nicht Vollständigkeit. Was hier `verified`
steht, wurde von mindestens einem unabhängigen Anwender außerhalb des Erfinders
mehrfach erfolgreich angewendet. Skills, die niemand zweitens nutzt, dürfen
dauerhaft `candidate` bleiben — das ist Design, kein Bug.

### Search-Ranking nach Wilson-Score

Sortierung in `/playbooks/search`:
```
ORDER BY fts.rank,
         wilson_lower(success_count, validation_count) DESC,
         avg_latency_ms ASC NULLS LAST
```

Wilson-Score-Lower-Bound bestraft kleine Stichproben automatisch:

| Validations | success_rate | wilson_lower |
|-------------|--------------|--------------|
| 1/1         | 1.00         | 0.207        |
| 9/10        | 0.90         | 0.596        |
| 100/100     | 1.00         | 0.964        |
| 0/3         | 0.00         | 0.000        |

→ "9/10 schlägt 1/1" — das gewünschte Verhalten.

### Auto-Promote (candidate → verified)

Beim Validation-Insert wird geprüft (SPEC-Konstanten in `main.py`):

```
external_success_count >= 2     UND
wilson_lower(success_count, validation_count) >= 0.4
```

`external_success_count` zählt nur Validations mit `validator_agent != author_agent`
— Selbst-Validierung verschiebt also nichts. Die Konfidenz-Schwelle 0.4 zündet
genau bei 3/3 erfolgreichen Validations (wilson_lower=0.439) und passt damit zum
typischen Bootstrap "Author validiert sich einmal, externer Agent zweimal".

Bei N=2 Agenten: ein fremder Agent muss zweimal erfolgreich validiert haben +
mind. 1 weitere success (vom Author oder von ihm selbst) für n=3.
Bei N=10: zwei beliebige fremde Successes plus mind. 1 weitere reichen, sofern
parallele Failures die Konfidenz nicht unter 0.4 ziehen (was sie schnell tun:
2/3 = wilson 0.094, viel zu niedrig).

Auto-Promote löst gleichzeitig **Auto-Archive** aus: alle anderen `verified`-Versionen
desselben `skill_id` werden auf `archived` gesetzt — die neueste verified gewinnt.

### Auto-Demote (candidate/verified → archived)

Auch beim Validation-Insert geprüft:

```
validation_count >= 3   UND   wilson_lower < 0.3
```

Greift symmetrisch sowohl bei candidates wie bei verified — ein verified-Skill,
der im Wild scheitert, wird nicht degradiert sondern direkt archiviert. Aus
`archived` gibt es keinen Rückweg per API: wer den Skill reparieren will,
submittet eine neue Version.

### Manueller Promote

`POST /playbooks/{id}/promote` bleibt als Eskalation: ein candidate kann manuell
verified werden, auch wenn die Auto-Schwellen nicht erfüllt sind (z.B. wenn der
Skill aus operativen Gründen schnell erforderlich ist). Auch dieser Pfad löst
Auto-Archive älterer Versionen aus.

### Robustheit der Schwellen

| Pathologie                                  | Schutz                                      |
|---------------------------------------------|---------------------------------------------|
| Agent self-validates seinen Skill aufs Promote | `external_success_count` zählt nur Fremdvalidierungen |
| Skill mit 1/1 verdrängt 9/10                | Wilson-Score statt rohe success_rate         |
| Veraltete v1 rankt vor v10                  | Auto-Archive bei Promote setzt v1 auf archived |
| Verified-Skill bricht durch Umweltänderung  | Auto-Demote bei wilson_lower<0.3 nach n≥3   |
| Zwei parallele Promotes/Validations         | Atomare UPDATEs mit Status-Guard, schon vorher |

Die Schwellen sind feste Konstanten — keine N-Adaptivität, kein Time-Decay,
kein Background-Worker. Wilson-Score skaliert von sich aus mit dem Volumen.

## API Endpoints

Alle JSON. Base URL intern: `http://playbook-registry:8000`.

### `GET /health`
Health-Check. Returns `{"status": "ok", "db": "connected", "journal_mode": "wal"}`.

### `POST /playbooks/candidate`
Reicht einen neuen Kandidaten ein. Bei bestehendem `skill_id` → version inkrementieren.
Status wird automatisch auf `candidate` gesetzt.

Optional: `idempotency_key` für sichere Client-Retries.

Request-Body:
```json
{
  "skill_id": "gcp-auth-workload-identity",
  "problem_domain": "gcp-authentication",
  "problem_description": "Service account access from container to GCP API",
  "approach": "Workload Identity Federation instead of long-lived keys",
  "content": "## Steps\n1. ...",
  "author_agent": "agent-1",
  "metadata": {
    "latency_ms": 3200,
    "model_used": "claude-sonnet-4-6",
    "tags": ["gcp", "auth", "production"]
  },
  "idempotency_key": "550e8400-e29b-41d4-a716-446655440000"
}
```

Response (201): `{"id": 42, "skill_id": "...", "version": 2, "status": "candidate", "idempotent_replay": false}`

Wenn derselbe `idempotency_key` schon existiert: `idempotent_replay: true` mit den
ursprünglichen Werten.

### `GET /playbooks/search?q=...&status=verified&limit=5`
Volltextsuche via FTS5. Default status=`verified`, limit=5.
Sortierung: FTS5-Rank, danach success_rate DESC.

Query-Parameter:
- `q` (required): Suchbegriff
- `status` (optional, default 'verified'): kann auch 'candidate' oder 'all' sein
- `limit` (optional, default 5, max 20)

### `GET /playbooks/{playbook_id}`
Holt einen einzelnen Playbook samt aller Validations. 404 wenn nicht gefunden.

### `POST /playbooks/{playbook_id}/validate`
Agent meldet zurück, ob das Playbook funktioniert hat. Optional: `idempotency_key`.

Request-Body:
```json
{
  "validator_agent": "agent-2",
  "success": true,
  "latency_ms": 2800,
  "model_used": "claude-opus-4-7",
  "notes": "worked first try",
  "idempotency_key": "..."
}
```

### `POST /playbooks/{playbook_id}/promote`
Manuelle Promotion: status candidate → verified, promoted_at = CURRENT_TIMESTAMP.
404 wenn nicht gefunden, 409 wenn schon verified oder archived.

### `GET /playbooks/by-skill/{skill_id}/versions`
Listet alle Versionen eines Skills auf — für Vergleich divergenter Lösungen.

## Connection-Handling (wichtig)

```python
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()
```

## Schema-Initialisierung

Beim Container-Start: prüfen, ob `playbooks.db` existiert und Schema vorhanden ist.
Wenn nicht → `schema.sql` ausführen. Idempotent (CREATE TABLE IF NOT EXISTS).

## Docker Setup

### Dockerfile
- Base: `python:3.12` (NICHT slim — wir brauchen FTS5)
- Workdir: `/app`
- Copy requirements.txt, pip install
- Copy source files
- Volume mount: `/data` für die DB
- Expose 8000
- Healthcheck via `/health`-Endpoint
- CMD: `uvicorn main:app --host 0.0.0.0 --port 8000`

### docker-compose.yml
Drei Services:
- `playbook-registry` (dieser Service)
- `hermes-agent-1` (Platzhalter)
- `hermes-agent-2` (Platzhalter)

Ein gemeinsames Network `hermes-net`. Volume `playbook-data` für Persistenz.

## Phasenweises Vorgehen für die Implementierung

1. **Phase 1**: schema.sql + Dockerfile + main.py mit nur `/health`. Container baut + läuft.
2. **Phase 2**: `POST /playbooks/candidate` (mit Retry + Idempotenz) + `GET /playbooks/search`. Mit curl testen.
3. **Phase 3**: `POST /playbooks/{id}/validate` + `GET /playbooks/{id}` + `POST /playbooks/{id}/promote`.
4. **Phase 4**: `GET /playbooks/by-skill/{skill_id}/versions`.
5. **Phase 5**: docker-compose.yml fertig + Network testen.
6. **Phase 6**: Lifecycle und Bewertung — Wilson-Score-Ranking, Auto-Promote (Cross-
   Validation), Auto-Demote bei Drift, Auto-Archive älterer Versionen.
7. **Phase 7**: MCP-Wrapper als zweiter Container im hermes-net. Tool-Mapping
   1:1 auf REST. Agent-Identität wird serverseitig aus `AGENT_ID` befüllt.
8. **Phase 8**: Production-Readiness. Pytest-Test-Suite (REST + Lifecycle +
   Concurrency + Wilson + STDIO-MCP). DB-Migrationen via `migrations/` mit
   `_migrations`-Tracking. Online-Backup/Restore via Skripte. Healthcheck-
   Probe. GitHub-Actions baut Multi-Arch-Images (linux/amd64,arm64) zu GHCR.
   Pre-built Images via `deploy.sh` als One-shot-Installer.

## MCP-Wrapper (Phase 7)

Der MCP-Wrapper ist ein dünner Adapter über der REST-API: er macht die Registry
für agent-native Konsumenten via MCP-Tools verfügbar, hält selbst keinen State,
und mappt jeden Tool-Call auf einen REST-Call. REST bleibt die Source of Truth.

### Tool-Mapping

| MCP-Tool                                                      | REST-Endpoint                                       |
|---------------------------------------------------------------|-----------------------------------------------------|
| `search_skills(query, status?, limit?)`                       | `GET /playbooks/search`                             |
| `get_skill(playbook_id)`                                      | `GET /playbooks/{id}`                               |
| `list_skill_versions(skill_id)`                               | `GET /playbooks/by-skill/{skill_id}/versions`       |
| `publish_skill(skill_id, ..., metadata_json?)`                | `POST /playbooks/candidate`                         |
| `rate_skill(playbook_id, success, latency_ms?, ...)`          | `POST /playbooks/{id}/validate`                     |
| `promote_skill(playbook_id)`                                  | `POST /playbooks/{id}/promote`                      |

### Identität / Trust

`author_agent` und `validator_agent` werden vom MCP-Server aus `AGENT_ID` (ENV)
befüllt — Clients können sich nicht selbst eine andere Identität geben. Pro
Agent ein Wrapper-Container mit eigener `AGENT_ID`, oder STDIO-Modus als
In-Process-Subprocess des Agenten.

### Transports

- `MCP_TRANSPORT=stdio` (Default in `server.py`): klassisch als Subprocess vom
  Agenten gestartet. Empfehlung für Hermes/Hermine-Integration via Claude-Config.
- `MCP_TRANSPORT=http`: streamable-http auf `MCP_PORT` (Default 8001). Empfohlen
  für Container-Deployment im hermes-net. Pfad: `http://playbook-registry-mcp-<agent>:8001/mcp`.

## Out of Scope (bewusst)

- Authentication (Stub vorbereiten, nicht aktivieren)
- Rate Limiting
- Embeddings/Vector Search (FTS5 reicht erstmal)
- Time-Decay über alte Validations (Skills altern bei Re-Use, nicht von selbst)
- Backup/Restore (Litestream oder cron-basiert separat)
- Web-UI
- Persistent Queue für Writes (synchroner Write-Pfad ist robust genug für 2-10 Agenten)
