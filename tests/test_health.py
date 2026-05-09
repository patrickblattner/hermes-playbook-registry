"""Health-Endpoint smoke test."""


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "connected"
    assert body["journal_mode"] == "wal"


def test_migrations_applied_on_startup(client, fresh_db):
    """
    Nach Startup: _migrations-Tabelle hat 001_initial drin.
    client-fixture hat den App-lifespan gefahren → init_db ist gelaufen.
    """
    import sqlite3

    conn = sqlite3.connect(fresh_db)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT name FROM _migrations ORDER BY name"))
    assert any(r["name"] == "001_initial.sql" for r in rows)
    conn.close()
