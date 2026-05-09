"""
Pytest setup: jeder Test bekommt eine frische SQLite-DB und einen frischen
TestClient. main.app wird dabei mit reloaded Module-State neu hochgefahren,
damit DB_PATH pro Test sauber ist.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Repo-Root in sys.path, damit `import main` aus tests/ funktioniert.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def fresh_db(monkeypatch):
    """Frische SQLite-DB pro Test, und main neu importieren damit DB_PATH greift."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # init_db legt sie selber an

    monkeypatch.setenv("PLAYBOOK_DB_PATH", path)

    # Module neu laden, damit DB_PATH = os.environ.get(...) im Module-Body
    # mit der neuen ENV ausgewertet wird.
    if "main" in sys.modules:
        importlib.reload(sys.modules["main"])
    else:
        import main  # noqa: F401

    yield path

    # Cleanup
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.unlink(p)


@pytest.fixture
def client(fresh_db):
    """FastAPI TestClient mit hochgelaufenem app.lifespan (init_db läuft)."""
    from fastapi.testclient import TestClient

    import main as main_module

    with TestClient(main_module.app) as c:
        yield c
