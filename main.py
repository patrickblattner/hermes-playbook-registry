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
import sqlite3
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
    """Reicht einen neuen Playbook-Kandidaten ein. Bei bestehendem skill_id → neue Version."""
    cur = conn.cursor()

    # Höchste vorhandene Version für diesen skill_id ermitteln
    cur.execute(
        "SELECT MAX(version) FROM playbooks WHERE skill_id = ?",
        (submission.skill_id,),
    )
    max_version = cur.fetchone()[0]
    new_version = (max_version or 0) + 1

    metadata_json = json.dumps(submission.metadata) if submission.metadata else None

    cur.execute(
        """
        INSERT INTO playbooks
            (skill_id, version, status, problem_domain, problem_description,
             approach, content, author_agent, metadata)
        VALUES (?, ?, 'candidate', ?, ?, ?, ?, ?, ?)
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
        ),
    )
    conn.commit()
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
    # Existenz prüfen
    exists = conn.execute("SELECT 1 FROM playbooks WHERE id = ?", (playbook_id,)).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail=f"Playbook {playbook_id} nicht gefunden")

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO validations
            (playbook_id, validator_agent, success, latency_ms, model_used, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            playbook_id,
            validation.validator_agent,
            validation.success,
            validation.latency_ms,
            validation.model_used,
            validation.notes,
        ),
    )
    conn.commit()

    logger.info(
        f"Validation aufgezeichnet: playbook_id={playbook_id} "
        f"agent={validation.validator_agent} success={validation.success}"
    )

    return ValidationResponse(
        id=cur.lastrowid,
        playbook_id=playbook_id,
        recorded=True,
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

    conn.execute(
        "UPDATE playbooks SET status='verified', promoted_at=CURRENT_TIMESTAMP WHERE id = ?",
        (playbook_id,),
    )
    conn.commit()

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
