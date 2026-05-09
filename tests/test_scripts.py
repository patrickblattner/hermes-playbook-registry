"""
Operations-Skripte: backup.sh, restore.sh, healthcheck.sh.

Vorausgesetzt: das `sqlite3` Binary ist installiert (im Image und in CI).
Lokal ohne sqlite3 → werden die Tests übersprungen.
"""

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

requires_sqlite3 = pytest.mark.skipif(
    shutil.which("sqlite3") is None,
    reason="sqlite3 CLI not installed",
)


def _seed_db(path: Path) -> None:
    """Minimale DB anlegen, die die Skripte erwarten (PRAGMA + 1 Tabelle)."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE playbooks (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO playbooks DEFAULT VALUES")
    conn.commit()
    conn.close()


# ---------- backup.sh ----------

@requires_sqlite3
def test_backup_creates_file_and_passes_integrity(tmp_path):
    db = tmp_path / "playbooks.db"
    _seed_db(db)
    backup_dir = tmp_path / "backups"

    env = {
        **os.environ,
        "PLAYBOOK_DB_PATH": str(db),
        "BACKUP_DIR": str(backup_dir),
        "RETAIN": "5",
    }
    r = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "backup.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"backup.sh failed:\n{r.stderr}"

    backups = list(backup_dir.glob("playbooks-*.db"))
    assert len(backups) == 1, f"expected 1 backup, got {[b.name for b in backups]}"

    # Integrity-Check muss 'ok' liefern (steht auch im Skript selbst, aber wir
    # double-checken). Daten sind gleich.
    conn = sqlite3.connect(str(backups[0]))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM playbooks").fetchone()[0] == 1
    conn.close()


@requires_sqlite3
def test_backup_rotation_keeps_only_RETAIN(tmp_path):
    """Mit RETAIN=2 und 4 Backups bleiben nur die 2 neuesten."""
    db = tmp_path / "playbooks.db"
    _seed_db(db)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    # Vorhandene "alte" Backups simulieren — Datei-mtime spiegelt Reihenfolge.
    for i, ts in enumerate(["20260101-000000Z", "20260102-000000Z", "20260103-000000Z"]):
        p = backup_dir / f"playbooks-{ts}.db"
        p.write_bytes(b"dummy")
        # mtime in aufsteigender Reihenfolge
        os.utime(p, (1700000000 + i, 1700000000 + i))

    env = {
        **os.environ,
        "PLAYBOOK_DB_PATH": str(db),
        "BACKUP_DIR": str(backup_dir),
        "RETAIN": "2",
    }
    r = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "backup.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0

    remaining = sorted(p.name for p in backup_dir.glob("playbooks-*.db"))
    # Erwartet: das gerade erstellte Backup + das jüngste der vorherigen ("03")
    assert len(remaining) == 2
    assert any("20260103" in n for n in remaining)


# ---------- restore.sh ----------

@requires_sqlite3
def test_restore_replaces_db_and_keeps_safety_copy(tmp_path):
    db = tmp_path / "playbooks.db"
    _seed_db(db)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    # Backup erstellen
    backup_file = backup_dir / "playbooks-snapshot.db"
    src_conn = sqlite3.connect(str(db))
    dst_conn = sqlite3.connect(str(backup_file))
    src_conn.backup(dst_conn)
    src_conn.close()
    dst_conn.close()

    # Live-DB modifizieren — wir wollen sicherstellen, dass restore die
    # Modifikation überschreibt und dabei eine safety copy schreibt.
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO playbooks DEFAULT VALUES")
    conn.commit()
    conn.close()
    assert sqlite3.connect(str(db)).execute("SELECT COUNT(*) FROM playbooks").fetchone()[0] == 2

    env = {**os.environ, "PLAYBOOK_DB_PATH": str(db)}
    r = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "restore.sh"), str(backup_file)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"restore.sh failed:\n{r.stderr}"

    # Restore brachte uns zum 1-Row-Snapshot zurück
    rows = sqlite3.connect(str(db)).execute("SELECT COUNT(*) FROM playbooks").fetchone()[0]
    assert rows == 1

    # Safety copy existiert (mit Timestamp-Suffix). WAL/SHM-Begleitfiles
    # werden mitkopiert; uns interessiert genau eine `.db`-Hauptdatei.
    safety_main = [
        p for p in tmp_path.glob("playbooks.db.pre-restore.*")
        if not p.name.endswith(("-wal", "-shm"))
    ]
    assert len(safety_main) == 1


@requires_sqlite3
def test_restore_refuses_corrupt_backup(tmp_path):
    db = tmp_path / "playbooks.db"
    _seed_db(db)
    bad_backup = tmp_path / "bad.db"
    bad_backup.write_bytes(b"this is not a sqlite database")

    env = {**os.environ, "PLAYBOOK_DB_PATH": str(db)}
    r = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "restore.sh"), str(bad_backup)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode != 0
    assert "integrity check failed" in r.stderr.lower()


# ---------- healthcheck.sh ----------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_health_url(tmp_path):
    """uvicorn-Subprocess der den /health-Endpoint exponiert."""
    port = _free_port()
    env = {
        **os.environ,
        "PLAYBOOK_DB_PATH": str(tmp_path / "hc.db"),
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "main:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    deadline = time.time() + 10
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=0.5).status_code == 200:
                break
        except httpx.RequestError:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("uvicorn for healthcheck not ready")

    yield url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_healthcheck_exit_0_on_ok(live_health_url):
    env = {**os.environ, "HEALTH_URL": live_health_url}
    r = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "healthcheck.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0
    assert "OK" in r.stdout


def test_healthcheck_exit_2_on_unreachable(tmp_path):
    """HEALTH_URL zeigt auf nicht-bedienten Port → exit 2."""
    env = {**os.environ, "HEALTH_URL": "http://127.0.0.1:1/health", "TIMEOUT": "1"}
    r = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "healthcheck.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 2
