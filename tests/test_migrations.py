"""DB-Migrationen: Idempotenz, applied_at-Tracking, Schutz gegen Re-Run.

Tests laufen init_db() in isolierten Subprocesses, damit ENV-Wechsel +
Module-State sich nicht in den shared TestClient-Prozess verbeißen.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_init_db(db_path: str) -> None:
    """Frischer Python-Prozess, importiert main mit gegebenem DB_PATH, ruft init_db."""
    env = {**os.environ, "PLAYBOOK_DB_PATH": db_path, "PYTHONPATH": str(REPO_ROOT)}
    r = subprocess.run(
        [sys.executable, "-c", "import main; main.init_db()"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert r.returncode == 0, f"init_db failed:\nSTDOUT: {r.stdout}\nSTDERR: {r.stderr}"


def test_migration_recorded_with_timestamp(client, fresh_db):
    """Initial-Run: 001_initial.sql ist applied, applied_at gesetzt."""
    conn = sqlite3.connect(fresh_db)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT name, applied_at FROM _migrations"))
    assert len(rows) == 1
    assert rows[0]["name"] == "001_initial.sql"
    assert rows[0]["applied_at"] is not None
    conn.close()


def test_migration_not_applied_twice(tmp_path):
    """init_db zweimal aufrufen darf nur einen _migrations-Eintrag erzeugen."""
    db = str(tmp_path / "twice.db")
    _run_init_db(db)
    _run_init_db(db)

    conn = sqlite3.connect(db)
    rows = list(conn.execute("SELECT name FROM _migrations"))
    conn.close()
    assert len(rows) == 1, f"expected 1 migration, got {[r[0] for r in rows]}"


def test_existing_db_without_migrations_table_gets_marked(tmp_path):
    """
    Upgrade-Pfad: eine DB mit dem aktuellen Schema (z.B. aus einer früheren
    Phase, die das schema.sql noch direkt ausgeführt hat — kein _migrations
    Tracking), bekommt beim init_db die _migrations-Tabelle ergänzt und 001
    als applied markiert. Daten bleiben unverändert (alle CREATE-Statements
    IF NOT EXISTS).
    """
    db = str(tmp_path / "legacy.db")
    # Legacy = volles Schema ist drin, nur _migrations fehlt
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    sql_file = REPO_ROOT / "migrations" / "001_initial.sql"
    conn.executescript(sql_file.read_text())
    conn.execute(
        "INSERT INTO playbooks (skill_id, version, status, problem_domain, "
        "problem_description, approach, content, author_agent) "
        "VALUES (?,1,'verified','x','x','x','x','x')",
        ("legacy-skill",),
    )
    conn.commit()
    conn.close()

    _run_init_db(db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    migs = [r["name"] for r in conn.execute("SELECT name FROM _migrations")]
    skills = [r["skill_id"] for r in conn.execute("SELECT skill_id FROM playbooks")]
    conn.close()
    assert "001_initial.sql" in migs
    assert "legacy-skill" in skills
