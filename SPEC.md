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

Aggregierte Validierungs-Statistiken pro Playbook (validation_count, success_rate,
avg_latency_ms). Vereinfacht das Sortieren in der Search.

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

## Out of Scope (bewusst)

- Authentication (Stub vorbereiten, nicht aktivieren)
- Rate Limiting
- Embeddings/Vector Search (FTS5 reicht erstmal)
- Auto-Promotion-Regeln (manuell durch User, später automatisierbar)
- Backup/Restore (Litestream oder cron-basiert separat)
- Web-UI
- Persistent Queue für Writes (synchroner Write-Pfad ist robust genug für 2-10 Agenten)
