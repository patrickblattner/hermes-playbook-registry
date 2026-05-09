"""Idempotency-Keys: gleicher Key → gleiche id, idempotent_replay flag."""


CANDIDATE = {
    "skill_id": "test-idem-skill",
    "problem_domain": "x",
    "problem_description": "x",
    "approach": "x",
    "content": "x",
    "author_agent": "agent-A",
}


def test_publish_without_key_creates_new(client):
    r = client.post("/playbooks/candidate", json=CANDIDATE)
    assert r.status_code == 201
    body = r.json()
    assert body["idempotent_replay"] is False
    assert body["status"] == "candidate"
    assert body["version"] == 1


def test_publish_with_same_key_returns_replay(client):
    payload = {**CANDIDATE, "idempotency_key": "key-a"}
    r1 = client.post("/playbooks/candidate", json=payload)
    assert r1.status_code == 201
    assert r1.json()["idempotent_replay"] is False

    r2 = client.post("/playbooks/candidate", json=payload)
    assert r2.status_code == 201
    body = r2.json()
    assert body["idempotent_replay"] is True
    assert body["id"] == r1.json()["id"]


def test_publish_replay_ignores_changed_payload(client):
    """Replay liefert Original-Daten zurück, nicht das neue Payload."""
    p1 = {**CANDIDATE, "idempotency_key": "key-b", "approach": "v1"}
    r1 = client.post("/playbooks/candidate", json=p1)
    p2 = {**CANDIDATE, "idempotency_key": "key-b", "approach": "v2-anders"}
    r2 = client.post("/playbooks/candidate", json=p2)
    # Beide Antworten zeigen auf id=1 (Original); approach v2-anders wird ignoriert.
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["idempotent_replay"] is True
    # Direkter GET zeigt: approach ist v1 (das Original)
    full = client.get(f"/playbooks/{r1.json()['id']}").json()
    assert full["approach"] == "v1"


def test_validate_idempotency_replay(client):
    pid = client.post("/playbooks/candidate", json=CANDIDATE).json()["id"]
    val = {"validator_agent": "agent-B", "success": True, "idempotency_key": "v-key"}
    r1 = client.post(f"/playbooks/{pid}/validate", json=val)
    assert r1.json()["idempotent_replay"] is False

    r2 = client.post(f"/playbooks/{pid}/validate", json=val)
    assert r2.json()["idempotent_replay"] is True
    assert r2.json()["id"] == r1.json()["id"]
