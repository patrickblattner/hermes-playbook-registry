-- Hermes Playbook Registry — Schema
-- Idempotent: kann bei jedem Container-Start ausgeführt werden

-- WAL einmal persistent setzen (überdauert Connections)
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- Hauptabelle: die Playbooks
CREATE TABLE IF NOT EXISTS playbooks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id            TEXT NOT NULL,
    version             INTEGER NOT NULL,
    status              TEXT NOT NULL CHECK(status IN ('candidate', 'verified', 'archived')),
    problem_domain      TEXT NOT NULL,
    problem_description TEXT NOT NULL,
    approach            TEXT NOT NULL,
    content             TEXT NOT NULL,
    author_agent        TEXT NOT NULL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    promoted_at         TIMESTAMP,
    metadata            TEXT,  -- JSON als String gespeichert
    UNIQUE(skill_id, version)
);

CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks(status);
CREATE INDEX IF NOT EXISTS idx_playbooks_skill_id ON playbooks(skill_id);
CREATE INDEX IF NOT EXISTS idx_playbooks_domain ON playbooks(problem_domain);

-- Validierungs-Events
CREATE TABLE IF NOT EXISTS validations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    playbook_id     INTEGER NOT NULL,
    validator_agent TEXT NOT NULL,
    success         BOOLEAN NOT NULL,
    latency_ms      INTEGER,
    model_used      TEXT,
    notes           TEXT,
    validated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (playbook_id) REFERENCES playbooks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_validations_playbook ON validations(playbook_id);
CREATE INDEX IF NOT EXISTS idx_validations_agent ON validations(validator_agent);

-- Volltextsuche via FTS5
-- content=''playbooks'' macht es zu einer "external content" Tabelle:
-- der eigentliche Inhalt wird nicht doppelt gespeichert, FTS verweist nur
CREATE VIRTUAL TABLE IF NOT EXISTS playbooks_fts USING fts5(
    skill_id,
    problem_domain,
    problem_description,
    approach,
    content,
    content='playbooks',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Trigger zur automatischen Synchronisation FTS <-> playbooks
CREATE TRIGGER IF NOT EXISTS playbooks_ai AFTER INSERT ON playbooks BEGIN
    INSERT INTO playbooks_fts(rowid, skill_id, problem_domain, problem_description, approach, content)
    VALUES (new.id, new.skill_id, new.problem_domain, new.problem_description, new.approach, new.content);
END;

CREATE TRIGGER IF NOT EXISTS playbooks_ad AFTER DELETE ON playbooks BEGIN
    INSERT INTO playbooks_fts(playbooks_fts, rowid, skill_id, problem_domain, problem_description, approach, content)
    VALUES('delete', old.id, old.skill_id, old.problem_domain, old.problem_description, old.approach, old.content);
END;

CREATE TRIGGER IF NOT EXISTS playbooks_au AFTER UPDATE ON playbooks BEGIN
    INSERT INTO playbooks_fts(playbooks_fts, rowid, skill_id, problem_domain, problem_description, approach, content)
    VALUES('delete', old.id, old.skill_id, old.problem_domain, old.problem_description, old.approach, old.content);
    INSERT INTO playbooks_fts(rowid, skill_id, problem_domain, problem_description, approach, content)
    VALUES (new.id, new.skill_id, new.problem_domain, new.problem_description, new.approach, new.content);
END;

-- View für aggregierte Validierungs-Statistiken pro Playbook
-- vereinfacht das Sortieren in der Search nach success_rate
CREATE VIEW IF NOT EXISTS playbook_stats AS
SELECT
    p.id AS playbook_id,
    COUNT(v.id) AS validation_count,
    COALESCE(AVG(CASE WHEN v.success THEN 1.0 ELSE 0.0 END), 0.0) AS success_rate,
    AVG(v.latency_ms) AS avg_latency_ms
FROM playbooks p
LEFT JOIN validations v ON v.playbook_id = p.id
GROUP BY p.id;
