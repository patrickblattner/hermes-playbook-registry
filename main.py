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
import math
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
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Retry-Reihe: 50ms, 100ms, 200ms, 400ms, 800ms (+ Jitter ≤50ms).
# busy_timeout=5000 fängt schon das meiste ab — das hier ist Belt & Suspenders
# für den Fall, dass beide Agenten exakt gleichzeitig schreiben.
DB_RETRY_MAX_ATTEMPTS = 5
DB_RETRY_BASE_DELAY = 0.05

# Lifecycle-Schwellen (siehe SPEC: Lifecycle und Bewertung).
# Auto-Promote: candidate→verified, wenn cross-validiert UND Konfidenz reicht.
PROMOTE_MIN_EXTERNAL_SUCCESSES = 2
# 0.4 zündet bei 3/3 (wilson=0.439) — passt zum natural Bootstrap "1 self + 2 ext".
# Bleibt aber strikt: 2 ext_succ + 1 failure liegt schon bei 0.094 → kein Promote.
PROMOTE_MIN_CONFIDENCE = 0.4
# Auto-Demote: jeder Status → archived bei wiederholtem Fehlschlag.
DEMOTE_MIN_VALIDATIONS = 3
DEMOTE_CONFIDENCE_BELOW = 0.3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("registry")


# ---------- DB-Initialisierung ----------

def init_db() -> None:
    """
    Migrations beim Start anwenden. _migrations-Tabelle merkt sich, welche
    Files schon ausgeführt wurden — neue Migrationen sind einfach numerisch
    benannte SQL-Files in migrations/ (z.B. 002_add_tags.sql) und werden in
    aufsteigender Reihenfolge applied. Existing DBs: alle vorhandenen Tables
    bleiben unberührt (CREATE TABLE IF NOT EXISTS), erste Migration wird
    danach als applied markiert, künftige Migrationen laufen normal.
    """
    db_dir = Path(DB_PATH).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Initialisiere DB unter {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                name        TEXT PRIMARY KEY,
                applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()

        applied = {row[0] for row in conn.execute("SELECT name FROM _migrations")}
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            logger.warning(f"Keine Migrations in {MIGRATIONS_DIR}")
        for sql_file in files:
            if sql_file.name in applied:
                continue
            logger.info(f"Apply migration: {sql_file.name}")
            conn.executescript(sql_file.read_text())
            conn.execute(
                "INSERT INTO _migrations (name) VALUES (?)", (sql_file.name,)
            )
            conn.commit()

        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        logger.info(f"DB initialisiert. journal_mode={mode}")

        try:
            conn.execute("SELECT 1 FROM playbooks_fts LIMIT 1")
            logger.info("FTS5 funktional.")
        except sqlite3.OperationalError as e:
            logger.error(f"FTS5 nicht verfügbar: {e}")
            raise
    finally:
        conn.close()


# ---------- Wilson Score Lower Bound (als SQLite-UDF registriert) ----------

def wilson_lower(successes: int | None, total: int | None, z: float = 1.96) -> float:
    """
    Untere Grenze des 95%-Konfidenzintervalls für die wahre Erfolgsrate.
    Bestraft kleine Stichproben automatisch:
      wilson_lower(1, 1)     ≈ 0.207  (1× Erfolg ist nicht überzeugend)
      wilson_lower(9, 10)    ≈ 0.596  (9/10 schlägt 1/1 deutlich)
      wilson_lower(100, 100) ≈ 0.964
      wilson_lower(0, 0)     = 0.0
    """
    if not total:
        return 0.0
    n = float(total)
    p = float(successes or 0) / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, (centre - margin) / denom)


# ---------- Connection Dependency ----------

def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Eine Connection pro Request. WAL ist persistent, PRAGMAs trotzdem defensiv setzen."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wilson-Score in SQL verfügbar machen (Search-ORDER-BY, Lifecycle-Trigger).
    conn.create_function("wilson_lower", 2, wilson_lower, deterministic=True)
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
    for attempt in range(max_attempts):
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            if attempt == max_attempts - 1:
                raise
            delay = DB_RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.05)
            logger.warning(
                f"DB locked (attempt {attempt + 1}/{max_attempts}), retry in {delay:.3f}s"
            )
            time.sleep(delay)


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


# ---------- Lifecycle: Auto-Promote / Auto-Demote / Auto-Archive ----------

def _archive_older_verified_versions(
    conn: sqlite3.Connection,
    skill_id: str,
    except_id: int,
) -> None:
    """
    Bei Promote von vN: alle anderen verified-Versionen desselben skill_id
    auf 'archived' setzen. Default-Annahme: neueste verified gewinnt.
    """
    cur = execute_with_retry(
        conn,
        "UPDATE playbooks SET status='archived' "
        "WHERE skill_id = ? AND status = 'verified' AND id != ?",
        (skill_id, except_id),
    )
    if cur.rowcount > 0:
        logger.info(
            f"Auto-Archive: {cur.rowcount} ältere verified-Version(en) "
            f"von skill_id={skill_id} archiviert"
        )


def _apply_lifecycle_after_validation(
    conn: sqlite3.Connection,
    playbook_id: int,
) -> None:
    """
    Wird nach jedem erfolgreichen Validation-Insert aufgerufen. Bewertet aktuelle
    Statistiken und führt — wenn die Schwellen erreicht sind — Auto-Demote oder
    Auto-Promote (inkl. Auto-Archive älterer Versionen) aus. Idempotent: jedes
    UPDATE hat ein Status-Guard in der WHERE-Klausel, doppelte Aufrufe haben
    keinen Effekt.
    """
    row = conn.execute(
        """
        SELECT
            p.skill_id,
            p.author_agent,
            p.status,
            COALESCE(s.validation_count, 0)       AS validation_count,
            COALESCE(s.success_count, 0)          AS success_count,
            COALESCE(s.external_success_count, 0) AS external_success_count,
            wilson_lower(COALESCE(s.success_count, 0),
                         COALESCE(s.validation_count, 0)) AS confidence
        FROM playbooks p
        LEFT JOIN playbook_stats s ON s.playbook_id = p.id
        WHERE p.id = ?
        """,
        (playbook_id,),
    ).fetchone()
    if not row:
        return

    # Auto-Demote zuerst — wenn die Daten dafür sprechen, ist Promote ohnehin off.
    if (
        row["status"] in ("candidate", "verified")
        and row["validation_count"] >= DEMOTE_MIN_VALIDATIONS
        and row["confidence"] < DEMOTE_CONFIDENCE_BELOW
    ):
        cur = execute_with_retry(
            conn,
            "UPDATE playbooks SET status='archived' "
            "WHERE id = ? AND status IN ('candidate','verified')",
            (playbook_id,),
        )
        if cur.rowcount > 0:
            logger.info(
                f"Auto-Demote: id={playbook_id} status→archived "
                f"(n={row['validation_count']}, confidence={row['confidence']:.3f})"
            )
        return

    # Auto-Promote candidate → verified, wenn cross-validiert UND Konfidenz reicht.
    if (
        row["status"] == "candidate"
        and row["external_success_count"] >= PROMOTE_MIN_EXTERNAL_SUCCESSES
        and row["confidence"] >= PROMOTE_MIN_CONFIDENCE
    ):
        cur = execute_with_retry(
            conn,
            "UPDATE playbooks SET status='verified', promoted_at=CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'candidate'",
            (playbook_id,),
        )
        if cur.rowcount > 0:
            logger.info(
                f"Auto-Promote: id={playbook_id} candidate→verified "
                f"(external_success={row['external_success_count']}, "
                f"confidence={row['confidence']:.3f})"
            )
            _archive_older_verified_versions(conn, row["skill_id"], playbook_id)


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

    metadata_json = json.dumps(submission.metadata) if submission.metadata else None

    # 2. Auto-Version + Insert mit innerem Race-Retry. Zwei parallele Submits ohne
    #    idempotency_key zum gleichen skill_id berechnen denselben MAX(version)+1
    #    und kollidieren auf UNIQUE(skill_id, version) — wir lesen MAX dann frisch
    #    und versuchen es erneut. rollback() sorgt dafür, dass der nächste Read
    #    nicht im stale Snapshot der gescheiterten Transaktion festklebt.
    VERSION_RACE_RETRIES = 3
    for retry in range(VERSION_RACE_RETRIES):
        max_version = conn.execute(
            "SELECT MAX(version) FROM playbooks WHERE skill_id = ?",
            (submission.skill_id,),
        ).fetchone()[0]
        new_version = (max_version or 0) + 1

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
        except sqlite3.IntegrityError as e:
            conn.rollback()
            msg = str(e).lower()

            # Fall A: idempotency_key-Race — anderer Request war minimal schneller.
            if submission.idempotency_key and "idempotency_key" in msg:
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

            # Fall B: (skill_id, version)-Race — neuer MAX, neuer Versuch.
            if "skill_id" in msg and "version" in msg and retry < VERSION_RACE_RETRIES - 1:
                logger.warning(
                    f"Version race for skill_id={submission.skill_id}, "
                    f"retry {retry + 2}/{VERSION_RACE_RETRIES}"
                )
                continue

            raise HTTPException(status_code=409, detail=f"Integrity error: {e}")


@app.get("/playbooks/search", response_model=SearchResponse)
def search_playbooks(
    q: str = Query(..., min_length=1, description="FTS5-Suchbegriff"),
    status: str = Query("verified", pattern="^(verified|candidate|all)$"),
    limit: int = Query(5, ge=1, le=20),
    conn: sqlite3.Connection = Depends(get_db),
):
    """
    Volltextsuche über FTS5. Sortierung: FTS5-Rank, dann confidence (Wilson-Score-
    Lower-Bound) DESC, dann avg_latency_ms ASC als Tiebreaker. Ein Skill mit 1/1
    Erfolg verliert damit gegen einen mit 9/10 — kleine Stichproben werden bestraft.
    """
    status_filter = "" if status == "all" else "AND p.status = ?"
    params: list = [q]
    if status != "all":
        params.append(status)
    params.append(limit)

    sql = f"""
        SELECT
            p.*,
            COALESCE(s.validation_count, 0)      AS validation_count,
            COALESCE(s.success_count, 0)         AS success_count,
            COALESCE(s.success_rate, 0.0)        AS success_rate,
            s.avg_latency_ms                     AS avg_latency_ms,
            COALESCE(s.external_success_count, 0) AS external_success_count,
            COALESCE(s.distinct_validators, 0)   AS distinct_validators,
            wilson_lower(COALESCE(s.success_count, 0),
                         COALESCE(s.validation_count, 0)) AS confidence
        FROM playbooks_fts fts
        JOIN playbooks p ON p.id = fts.rowid
        LEFT JOIN playbook_stats s ON s.playbook_id = p.id
        WHERE playbooks_fts MATCH ?
        {status_filter}
        ORDER BY fts.rank,
                 wilson_lower(COALESCE(s.success_count, 0),
                              COALESCE(s.validation_count, 0)) DESC,
                 s.avg_latency_ms ASC NULLS LAST
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
            COALESCE(s.validation_count, 0)       AS validation_count,
            COALESCE(s.success_count, 0)          AS success_count,
            COALESCE(s.success_rate, 0.0)         AS success_rate,
            s.avg_latency_ms                      AS avg_latency_ms,
            COALESCE(s.external_success_count, 0) AS external_success_count,
            COALESCE(s.distinct_validators, 0)    AS distinct_validators,
            wilson_lower(COALESCE(s.success_count, 0),
                         COALESCE(s.validation_count, 0)) AS confidence
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
        # rollback bevor wir den Replay-Match suchen, sonst sieht der Read noch
        # den stale Snapshot der gescheiterten Transaktion.
        conn.rollback()
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

    # Lifecycle-Trigger: aktuelle Stats bewerten, ggf. Auto-Promote oder Auto-Demote
    # ausführen. Bewusst nach dem logger.info, damit der Insert-Effekt klar getrennt
    # vom Lifecycle-Effekt protokolliert wird.
    _apply_lifecycle_after_validation(conn, playbook_id)

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

    # Atomar: AND status='candidate' verhindert TOCTOU bei zwei parallelen
    # Promote-Requests (sonst könnten beide den Vor-Check passieren und beide
    # erfolgreich UPDATE machen). rowcount=0 zeigt: ein anderer Request war zuerst.
    cur = execute_with_retry(
        conn,
        "UPDATE playbooks SET status='verified', promoted_at=CURRENT_TIMESTAMP "
        "WHERE id = ? AND status='candidate'",
        (playbook_id,),
    )
    if cur.rowcount == 0:
        raise HTTPException(
            status_code=409,
            detail=f"Playbook {playbook_id} wurde gleichzeitig von einem anderen Request promotet",
        )

    # Auto-Archive älterer verified-Versionen desselben skill_id
    # (manueller Promote-Pfad, symmetrisch zum Auto-Promote-Pfad).
    _archive_older_verified_versions(conn, row["skill_id"], playbook_id)

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
            COALESCE(s.validation_count, 0)       AS validation_count,
            COALESCE(s.success_count, 0)          AS success_count,
            COALESCE(s.success_rate, 0.0)         AS success_rate,
            s.avg_latency_ms                      AS avg_latency_ms,
            COALESCE(s.external_success_count, 0) AS external_success_count,
            COALESCE(s.distinct_validators, 0)    AS distinct_validators,
            wilson_lower(COALESCE(s.success_count, 0),
                         COALESCE(s.validation_count, 0)) AS confidence
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
