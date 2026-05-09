"""
Hermes Playbook Registry — FastAPI Service

Zentrale Wissens-Registry für mehrere Hermes-Agenten. Agenten reichen Lösungs-
Playbooks als Kandidaten ein, validieren die der jeweils anderen, und konsultieren
verifizierte Playbooks bevor sie neue Probleme angehen.

Backend: SQLite mit WAL-Mode (Concurrent Reads + Single Writer ohne gegenseitiges Block).
Volltextsuche: SQLite FTS5.
"""

import json
import logging
import os
import random
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Generator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from models import (
    CandidateResponse,
    CandidateSubmission,
    HealthResponse,
    PlaybookOut,
    PlaybookWithValidations,
    PromoteResponse,
    SearchResponse,
    ValidationOut,
    ValidationResponse,
    ValidationSubmission,
)

# ---------- Konfiguration ----------

DB_PATH = os.environ.get("PLAYBOOK_DB_PATH", "/data/playbooks.db")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Retry-Reihe: 50ms, 100ms, 200ms, 400ms, 800ms (+ Jitter ≤50ms).
# busy_timeout=5000 fängt schon das meiste ab — das hier ist Belt & Suspenders
# für den Fall, dass beide Agenten exakt gleichzeitig schreiben.
DB_RETRY_MAX_ATTEMPTS = 5
DB_RETRY_BASE_DELAY = 0.05

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("registry")


# ---------- DB-Initialisierung ----------

def init_db() -> None:
    """Schema beim Start anlegen (idempotent)."""
    db_dir = Path(DB_PATH).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Initialisiere DB unter {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        # WAL persistent setzen (PRAGMA journal_mode=WAL ist persistent in SQLite)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")

        schema_sql = SCHEMA_PATH.read_text()
        conn.executescript(schema_sql)
        conn.commit()

        # Verifiziere journal_mode
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        logger.info(f"DB initialisiert. journal_mode={mode}")

        # Verifiziere FTS5 verfügbar
        try:
            conn.execute("SELECT 1 FROM playbooks_fts LIMIT 1")
            logger.info("FTS5 funktional.")
        except sqlite3.OperationalError as e:
            logger.error(f"FTS5 nicht verfügbar: {e}")
            raise
    finally:
        conn.close()


# ---------- Connection Dependency ----------

def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Eine Connection pro Request. WAL ist persistent, PRAGMAs trotzdem defensiv setzen."""
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


# ---------- Retry-Helper für Writes ----------

def execute_with_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
    max_attempts: int = DB_RETRY_MAX_ATTEMPTS,
) -> sqlite3.Cursor:
    """
    Single-Statement Write mit Exponential Backoff bei SQLITE_BUSY/locked.

    busy_timeout=5000 erledigt nativ das meiste; diese Schicht greift nur, wenn
    nach 5s immer noch ein Lock anliegt. Bei 2-3 Agenten extrem unwahrscheinlich.

    Andere OperationalErrors (z.B. UNIQUE Constraint Violation kommt als
    IntegrityError) werden NICHT retryt → sofort weitergereicht.
    """
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(max_attempts):
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last_error = e
            if attempt < max_attempts - 1:
                delay = DB_RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.05)
                logger.warning(
                    f"DB locked (attempt {attempt + 1}/{max_attempts}), retry in {delay:.3f}s"
                )
                time.sleep(delay)
    assert last_error is not None
    raise last_error


# ---------- Helper: Row -> Dict für Pydantic ----------

def _row_to_playbook_dict(row: sqlite3.Row) -> dict:
    """Konvertiert eine sqlite3.Row in ein Dict, parst metadata-JSON."""
    d = dict(row)
    if d.get("metadata"):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = None
    return d


# ---------- App Lifecycle ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
    logger.info("Shutdown.")


app = FastAPI(
    title="Hermes Playbook Registry",
    description="Geteilte Wissens-Registry für Hermes-Agenten",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------- Endpoints ----------

@app.get("/health", response_model=HealthResponse)
def health(conn: sqlite3.Connection = Depends(get_db)):
    try:
        mode_row = conn.execute("PRAGMA journal_mode").fetchone()
        return HealthResponse(
            status="ok",
            db="connected",
            journal_mode=mode_row[0] if mode_row else None,
        )
    except sqlite3.Error as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "db": "disconnected", "journal_mode": None},
        )


@app.post(
    "/playbooks/candidate",
    response_model=CandidateResponse,
    status_code=201,
)
def submit_candidate(
    submission: CandidateSubmission,
    conn: sqlite3.Connection = Depends(get_db),
):
    """
    Reicht einen neuen Playbook-Kandidaten ein. Bei bestehendem skill_id → neue Version.
    Wiederholter Submit mit gleichem idempotency_key → Replay der ursprünglichen Antwort.
    """
    # 1. Idempotenz-Check (vor dem Insert): Key bekannt? → fertige Antwort zurück.
    if submission.idempotency_key:
        existing = conn.execute(
            "SELECT id, skill_id, version, status FROM playbooks WHERE idempotency_key = ?",
            (submission.idempotency_key,),
        ).fetchone()
        if existing:
            logger.info(
                f"Idempotenter Replay: key={submission.idempotency_key} → id={existing['id']}"
            )
            return CandidateResponse(
                id=existing["id"],
                skill_id=existing["skill_id"],
                version=existing["version"],
                status=existing["status"],
                idempotent_replay=True,
            )

    # 2. Nächste Version für diesen skill_id ermitteln.
    max_version = conn.execute(
        "SELECT MAX(version) FROM playbooks WHERE skill_id = ?",
        (submission.skill_id,),
    ).fetchone()[0]
    new_version = (max_version or 0) + 1

    metadata_json = json.dumps(submission.metadata) if submission.metadata else None

    # 3. Insert mit Retry-Wrapper. IntegrityError fängt die Race-Condition,
    #    falls zwei gleichzeitige Requests mit demselben Key beide den Pre-Check passieren.
    try:
        cur = execute_with_retry(
            conn,
            """
            INSERT INTO playbooks
                (skill_id, version, status, problem_domain, problem_description,
                 approach, content, author_agent, metadata, idempotency_key)
            VALUES (?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                submission.skill_id,
                new_version,
                submission.problem_domain,
                submission.problem_description,
                submission.approach,
                submission.content,
                submission.author_agent,
                metadata_json,
                submission.idempotency_key,
            ),
        )
    except sqlite3.IntegrityError as e:
        if submission.idempotency_key and "idempotency_key" in str(e).lower():
            existing = conn.execute(
                "SELECT id, skill_id, version, status FROM playbooks WHERE idempotency_key = ?",
                (submission.idempotency_key,),
            ).fetchone()
            if existing:
                return CandidateResponse(
                    id=existing["id"],
                    skill_id=existing["skill_id"],
                    version=existing["version"],
                    status=existing["status"],
                    idempotent_replay=True,
                )
        raise HTTPException(status_code=409, detail=f"Integrity error: {e}")

    new_id = cur.lastrowid
    logger.info(
        f"Kandidat eingereicht: id={new_id} skill_id={submission.skill_id} "
        f"version={new_version} author={submission.author_agent}"
    )

    return CandidateResponse(
        id=new_id,
        skill_id=submission.skill_id,
        version=new_version,
        status="candidate",
        idempotent_replay=False,
    )


@app.get("/playbooks/search", response_model=SearchResponse)
def search_playbooks(
    q: str = Query(..., min_length=1, description="FTS5-Suchbegriff"),
    status: str = Query("verified", pattern="^(verified|candidate|all)$"),
    limit: int = Query(5, ge=1, le=20),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Volltextsuche über FTS5. Sortiert nach FTS5-Rank, dann success_rate DESC."""
    status_filter = "" if status == "all" else "AND p.status = ?"
    params: list = [q]
    if status != "all":
        params.append(status)
    params.append(limit)

    sql = f"""
        SELECT
            p.*,
            COALESCE(s.validation_count, 0) AS validation_count,
            COALESCE(s.success_rate, 0.0)   AS success_rate,
            s.avg_latency_ms                AS avg_latency_ms
        FROM playbooks_fts fts
        JOIN playbooks p ON p.id = fts.rowid
        LEFT JOIN playbook_stats s ON s.playbook_id = p.id
        WHERE playbooks_fts MATCH ?
        {status_filter}
        ORDER BY fts.rank, s.success_rate DESC
        LIMIT ?
    """

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # FTS5-Syntax-Fehler in der Query (z.B. ungültige Operatoren)
        raise HTTPException(status_code=400, detail=f"Ungültige Suchanfrage: {e}")

    results = [PlaybookOut(**_row_to_playbook_dict(r)) for r in rows]
    return SearchResponse(query=q, total=len(results), results=results)


@app.get("/playbooks/{playbook_id}", response_model=PlaybookWithValidations)
def get_playbook(
    playbook_id: int,
    conn: sqlite3.Connection = Depends(get_db),
):
    """Holt einen einzelnen Playbook samt aller seiner Validierungen."""
    row = conn.execute(
        """
        SELECT
            p.*,
            COALESCE(s.validation_count, 0) AS validation_count,
            COALESCE(s.success_rate, 0.0)   AS success_rate,
            s.avg_latency_ms                AS avg_latency_ms
        FROM playbooks p
        LEFT JOIN playbook_stats s ON s.playbook_id = p.id
        WHERE p.id = ?
        """,
        (playbook_id,),
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Playbook {playbook_id} nicht gefunden")

    val_rows = conn.execute(
        "SELECT * FROM validations WHERE playbook_id = ? ORDER BY validated_at DESC",
        (playbook_id,),
    ).fetchall()
    validations = [ValidationOut(**dict(v)) for v in val_rows]

    pb_dict = _row_to_playbook_dict(row)
    pb_dict["validations"] = validations
    return PlaybookWithValidations(**pb_dict)


@app.post(
    "/playbooks/{playbook_id}/validate",
    response_model=ValidationResponse,
    status_code=201,
)
def record_validation(
    playbook_id: int,
    validation: ValidationSubmission,
    conn: sqlite3.Connection = Depends(get_db),
):
    """Ein Agent meldet zurück: das Playbook hat funktioniert / nicht funktioniert."""
    # 1. Idempotenz-Check vor allem anderen — auch vor 404, damit ein erfolgreich
    #    geschriebener Replay konsistent dieselbe Antwort liefert.
    if validation.idempotency_key:
        existing = conn.execute(
            "SELECT id, playbook_id FROM validations WHERE idempotency_key = ?",
            (validation.idempotency_key,),
        ).fetchone()
        if existing:
            return ValidationResponse(
                id=existing["id"],
                playbook_id=existing["playbook_id"],
                recorded=True,
                idempotent_replay=True,
            )

    # 2. Playbook-Existenz prüfen.
    exists = conn.execute("SELECT 1 FROM playbooks WHERE id = ?", (playbook_id,)).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail=f"Playbook {playbook_id} nicht gefunden")

    # 3. Insert mit Retry. IntegrityError fängt parallele Race auf gleichem Key.
    try:
        cur = execute_with_retry(
            conn,
            """
            INSERT INTO validations
                (playbook_id, validator_agent, success, latency_ms, model_used, notes,
                 idempotency_key)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                playbook_id,
                validation.validator_agent,
                validation.success,
                validation.latency_ms,
                validation.model_used,
                validation.notes,
                validation.idempotency_key,
            ),
        )
    except sqlite3.IntegrityError as e:
        if validation.idempotency_key and "idempotency_key" in str(e).lower():
            existing = conn.execute(
                "SELECT id, playbook_id FROM validations WHERE idempotency_key = ?",
                (validation.idempotency_key,),
            ).fetchone()
            if existing:
                return ValidationResponse(
                    id=existing["id"],
                    playbook_id=existing["playbook_id"],
                    recorded=True,
                    idempotent_replay=True,
                )
        raise HTTPException(status_code=409, detail=f"Integrity error: {e}")

    logger.info(
        f"Validation aufgezeichnet: playbook_id={playbook_id} "
        f"agent={validation.validator_agent} success={validation.success}"
    )

    return ValidationResponse(
        id=cur.lastrowid,
        playbook_id=playbook_id,
        recorded=True,
        idempotent_replay=False,
    )


@app.post("/playbooks/{playbook_id}/promote", response_model=PromoteResponse)
def promote_playbook(
    playbook_id: int,
    conn: sqlite3.Connection = Depends(get_db),
):
    """Promotion: candidate → verified. 409 wenn schon verified oder archived."""
    row = conn.execute(
        "SELECT id, skill_id, version, status FROM playbooks WHERE id = ?",
        (playbook_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Playbook {playbook_id} nicht gefunden")
    if row["status"] != "candidate":
        raise HTTPException(
            status_code=409,
            detail=f"Playbook {playbook_id} hat status='{row['status']}', kann nicht promotet werden",
        )

    execute_with_retry(
        conn,
        "UPDATE playbooks SET status='verified', promoted_at=CURRENT_TIMESTAMP WHERE id = ?",
        (playbook_id,),
    )

    promoted_row = conn.execute(
        "SELECT id, skill_id, version, status, promoted_at FROM playbooks WHERE id = ?",
        (playbook_id,),
    ).fetchone()

    logger.info(
        f"Playbook promotet: id={playbook_id} skill_id={row['skill_id']} version={row['version']}"
    )

    return PromoteResponse(**dict(promoted_row))


@app.get("/playbooks/by-skill/{skill_id}/versions", response_model=list[PlaybookOut])
def list_versions(
    skill_id: str,
    conn: sqlite3.Connection = Depends(get_db),
):
    """Listet alle Versionen eines Skills auf — für 'wie hat Agent X das anders gelöst' Vergleiche."""
    rows = conn.execute(
        """
        SELECT
            p.*,
            COALESCE(s.validation_count, 0) AS validation_count,
            COALESCE(s.success_rate, 0.0)   AS success_rate,
            s.avg_latency_ms                AS avg_latency_ms
        FROM playbooks p
        LEFT JOIN playbook_stats s ON s.playbook_id = p.id
        WHERE p.skill_id = ?
        ORDER BY p.version ASC
        """,
        (skill_id,),
    ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail=f"Keine Playbooks für skill_id='{skill_id}'")

    return [PlaybookOut(**_row_to_playbook_dict(r)) for r in rows]
