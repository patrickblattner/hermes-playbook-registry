"""HTTP-Validation: 404, 409, 422 für alle Endpoints."""

CANDIDATE = {
    "skill_id": "x",
    "problem_domain": "x",
    "problem_description": "x",
    "approach": "x",
    "content": "x",
    "author_agent": "agent-A",
}


# --- 404: nicht-existierende Playbooks ---

def test_get_nonexistent_returns_404(client):
    r = client.get("/playbooks/9999")
    assert r.status_code == 404


def test_validate_nonexistent_returns_404(client):
    r = client.post(
        "/playbooks/9999/validate",
        json={"validator_agent": "X", "success": True},
    )
    assert r.status_code == 404


def test_promote_nonexistent_returns_404(client):
    r = client.post("/playbooks/9999/promote")
    assert r.status_code == 404


def test_versions_nonexistent_returns_404(client):
    r = client.get("/playbooks/by-skill/never-published/versions")
    assert r.status_code == 404


# --- 409: ungültige Zustandsübergänge ---

def test_promote_already_verified_returns_409(client):
    """Ein bereits verifizierter Playbook kann nicht nochmal promotet werden."""
    pid = client.post("/playbooks/candidate", json=CANDIDATE).json()["id"]
    # Manuell promoten
    assert client.post(f"/playbooks/{pid}/promote").status_code == 200
    # Nochmal → 409
    r = client.post(f"/playbooks/{pid}/promote")
    assert r.status_code == 409
    assert "verified" in r.json()["detail"].lower()


# --- 422: Schema-/Constraint-Verletzungen via Pydantic ---

def test_candidate_missing_field_returns_422(client):
    incomplete = {k: v for k, v in CANDIDATE.items() if k != "approach"}
    r = client.post("/playbooks/candidate", json=incomplete)
    assert r.status_code == 422
    assert any(e["loc"][-1] == "approach" for e in r.json()["detail"])


def test_candidate_empty_string_returns_422(client):
    bad = {**CANDIDATE, "skill_id": ""}
    r = client.post("/playbooks/candidate", json=bad)
    assert r.status_code == 422


def test_candidate_oversized_string_returns_422(client):
    """skill_id hat max_length=200 (Field-Constraint)."""
    bad = {**CANDIDATE, "skill_id": "x" * 201}
    r = client.post("/playbooks/candidate", json=bad)
    assert r.status_code == 422


def test_validate_missing_field_returns_422(client):
    pid = client.post("/playbooks/candidate", json=CANDIDATE).json()["id"]
    r = client.post(f"/playbooks/{pid}/validate", json={"success": True})
    assert r.status_code == 422
    assert any(e["loc"][-1] == "validator_agent" for e in r.json()["detail"])


def test_search_empty_query_returns_422(client):
    r = client.get("/playbooks/search", params={"q": ""})
    assert r.status_code == 422


def test_search_invalid_status_returns_422(client):
    r = client.get("/playbooks/search", params={"q": "x", "status": "bogus"})
    assert r.status_code == 422


def test_search_limit_too_high_returns_422(client):
    r = client.get("/playbooks/search", params={"q": "x", "limit": 100})
    assert r.status_code == 422


def test_search_limit_too_low_returns_422(client):
    r = client.get("/playbooks/search", params={"q": "x", "limit": 0})
    assert r.status_code == 422
