# Hermes Playbook Registry — Specification

## Zweck

Ein lokaler REST-API-Service, der als zentrale Skill-/Playbook-Registry für mehrere
Hermes-Agenten dient. Die Agenten teilen verifiziertes Wissen über funktionierende
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

## Technische Constraints

- Python 3.12
- FastAPI + Uvicorn
- SQLite mit **WAL-Mode** (essentiell — Concurrent Reads + Single Writer ohne Block)
- Ausschließlich Python's stdlib `sqlite3` — KEIN SQLAlchemy, KEIN ORM
- Pydantic v2 für Request/Response-Modelle
- Eine SQLite-Connection pro Request via FastAPI Dependency Injection
- `check_same_thread=False`, `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`
- Für den Anfang alles in einer einzigen `main.py` — keine voreilige Modularisierung
- Keine externe Auth — beide Agenten sind im selben Docker-Network und vertrauenswürdig
  (API-Key-Header als Stub vorsehen, aber nicht aktivieren)
- Volltextsuche über SQLite **FTS5** (im Standard-Python-Image enthalten, ggf. `python:3.12`
  statt `python:3.12-slim` nutzen falls FTS5 fehlt)

## Datenmodell

### Tabelle `playbooks`

Speichert die Skills/Playbooks selbst.

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

UNIQUE constraint auf (skill_id, version).

### Tabelle `validations`

Zeichnet auf, welcher Agent ein Playbook getestet hat und wie es lief.

| Spalte           | Typ        | Notiz                                                    |
|------------------|------------|----------------------------------------------------------|
| id               | INTEGER PK | autoincrement                                            |
| playbook_id      | INTEGER    | FK auf playbooks.id                                      |
| validator_agent  | TEXT       | z.B. "agent-2"                                           |
| success          | BOOLEAN    | hat's funktioniert?                                      |
| latency_ms       | INTEGER    | Antwortzeit in ms, NULL erlaubt                          |
| model_used       | TEXT       | welches LLM wurde verwendet, NULL erlaubt                |
| notes            | TEXT       | freitext, NULL erlaubt                                   |
| validated_at     | TIMESTAMP  | DEFAULT CURRENT_TIMESTAMP                                |

### FTS5 Virtual Table `playbooks_fts`

Volltextindex über `skill_id`, `problem_domain`, `problem_description`, `approach`, `content`.
Mit Triggern (AFTER INSERT, AFTER UPDATE, AFTER DELETE) automatisch synchron mit `playbooks`.

## API Endpoints

Alle JSON. Base URL intern: `http://playbook-registry:8000`.

### `GET /health`
Health-Check. Returns `{"status": "ok", "db": "connected"}`.

### `POST /playbooks/candidate`
Reicht einen neuen Kandidaten ein (oder eine neue Version eines bestehenden Skills).
Wenn `skill_id` schon existiert → version inkrementieren.
Status wird automatisch auf `candidate` gesetzt.

Request-Body:
```json
{
  "skill_id": "gcp-auth-workload-identity",
  "problem_domain": "gcp-authentication",
  "problem_description": "Service account access from container to GCP API",
  "approach": "Workload Identity Federation instead of long-lived keys",
  "content": "## Steps\n1. ...\n2. ...",
  "author_agent": "agent-1",
  "metadata": {
    "latency_ms": 3200,
    "model_used": "claude-sonnet-4-6",
    "tags": ["gcp", "auth", "production"]
  }
}
```

Response: `{"id": 42, "skill_id": "...", "version": 2, "status": "candidate"}`

### `GET /playbooks/search?q=...&status=verified&limit=5`
Volltextsuche via FTS5. Default status=`verified`, limit=5.
Sortierung: FTS5-Rank, danach success_rate (aus validations aggregiert) DESC.

Query-Parameter:
- `q` (required): Suchbegriff
- `status` (optional, default 'verified'): kann auch 'candidate' oder 'all' sein
- `limit` (optional, default 5, max 20)

Response: Liste von Playbook-Objekten mit allen Feldern + zusätzlich `success_rate`,
`validation_count`.

### `GET /playbooks/{playbook_id}`
Holt einen einzelnen Playbook samt aller Validations. 404 wenn nicht gefunden.

### `POST /playbooks/{playbook_id}/validate`
Agent meldet zurück, ob das Playbook funktioniert hat.

Request-Body:
```json
{
  "validator_agent": "agent-2",
  "success": true,
  "latency_ms": 2800,
  "model_used": "claude-opus-4-7",
  "notes": "worked first try"
}
```

Response: `{"id": 17, "playbook_id": 42, "recorded": true}`

### `POST /playbooks/{playbook_id}/promote`
Manuelle Promotion: status candidate → verified, promoted_at = CURRENT_TIMESTAMP.
404 wenn nicht gefunden, 409 wenn schon verified oder archived.

### `GET /playbooks/by-skill/{skill_id}/versions`
Listet alle Versionen eines Skills auf — nützlich für den "warum löst Agent 1 das anders
als Agent 2"-Vergleich.

Response: Array von Playbooks (alle Versionen, ältest zuerst).

## Connection-Handling

Wichtig (sonst gibt's Probleme):

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

Verwendung pro Endpoint via `Depends(get_db)`.

WAL-Mode wird einmal bei Schema-Init gesetzt und bleibt persistent (PRAGMA persist).
Trotzdem in jeder Connection nochmal aktivieren ist defensiv und schadet nicht.

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
- CMD: `uvicorn main:app --host 0.0.0.0 --port 8000`

### docker-compose.yml
Drei Services:
- `playbook-registry` (dieser Service)
- `hermes-agent-1` (Platzhalter — User hat seine eigenen Container)
- `hermes-agent-2` (Platzhalter)

Ein gemeinsames Network `hermes-net`. Volume `playbook-data` für Persistenz.
Registry exponiert keine Ports nach außen (interne Kommunikation reicht), aber
optional `127.0.0.1:8080:8000` als kommentierte Zeile für Host-Debugging.

## Phasenweises Vorgehen für die Implementierung

1. **Phase 1**: schema.sql + Dockerfile + main.py mit nur `/health`. Container baut + läuft.
2. **Phase 2**: `POST /playbooks/candidate` + `GET /playbooks/search`. Mit curl testen.
3. **Phase 3**: `POST /playbooks/{id}/validate` + `GET /playbooks/{id}` + `POST /playbooks/{id}/promote`.
4. **Phase 4**: `GET /playbooks/by-skill/{skill_id}/versions`.
5. **Phase 5**: docker-compose.yml fertig + Network testen.

## Out of Scope (bewusst)

- Authentication (kommt später, Stub vorbereiten)
- Rate Limiting
- Embeddings/Vector Search (FTS5 reicht erstmal — Erweiterung später möglich)
- Auto-Promotion-Regeln (manuell durch User, später automatisierbar)
- Backup/Restore (Litestream oder cron-basiert separat aufsetzen)
- Web-UI (curl + sqlite3 CLI reichen am Anfang)
