"""Search: FTS5 + Wilson-basiertes Ranking."""


def _publish(client, skill_id, content, author="agent-A"):
    r = client.post(
        "/playbooks/candidate",
        json={
            "skill_id": skill_id,
            "problem_domain": "test",
            "problem_description": content,
            "approach": "x",
            "content": content,
            "author_agent": author,
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


def test_search_default_returns_only_verified(client):
    _publish(client, "default-test", "hello world")
    r = client.get("/playbooks/search", params={"q": "hello"})
    assert r.status_code == 200
    assert r.json()["total"] == 0  # candidate, nicht in default search


def test_search_status_all_finds_candidate(client):
    _publish(client, "search-all", "hello world")
    r = client.get("/playbooks/search", params={"q": "hello", "status": "all"})
    assert r.json()["total"] == 1


def test_search_invalid_fts_returns_400(client):
    r = client.get("/playbooks/search", params={"q": "AND OR"})
    assert r.status_code == 400


def test_search_ranks_by_wilson_lower(client):
    """1/1=100% Erfolg verliert gegen 9/10 (Wilson-Score-Ranking)."""
    a = _publish(client, "rank-A", "ranktest A")
    b = _publish(client, "rank-B", "ranktest B")

    # A: 1× success
    client.post(
        f"/playbooks/{a}/validate",
        json={"validator_agent": "X", "success": True},
    )
    # B: 9× success, 1× failure
    for _ in range(9):
        client.post(
            f"/playbooks/{b}/validate",
            json={"validator_agent": "X", "success": True},
        )
    client.post(
        f"/playbooks/{b}/validate",
        json={"validator_agent": "X", "success": False},
    )

    r = client.get(
        "/playbooks/search",
        params={"q": "ranktest", "status": "all", "limit": 5},
    )
    results = r.json()["results"]
    assert len(results) == 2

    # B (id=2) sollte VOR A (id=1) stehen, weil confidence höher
    a_idx = next(i for i, x in enumerate(results) if x["skill_id"] == "rank-A")
    b_idx = next(i for i, x in enumerate(results) if x["skill_id"] == "rank-B")
    assert b_idx < a_idx
    assert results[b_idx]["confidence"] > results[a_idx]["confidence"]


def test_versions_endpoint_returns_oldest_first(client):
    skill = "versions-test"
    _publish(client, skill, "v1 content")
    _publish(client, skill, "v2 content")
    r = client.get(f"/playbooks/by-skill/{skill}/versions")
    versions = [r["version"] for r in r.json()]
    assert versions == sorted(versions)
    assert len(versions) == 2


def test_fts_or_operator(client):
    _publish(client, "skill-alpha", "alpha document")
    _publish(client, "skill-beta", "beta paragraph")
    r = client.get(
        "/playbooks/search",
        params={"q": "alpha OR beta", "status": "all", "limit": 5},
    )
    skills = sorted(r["skill_id"] for r in r.json()["results"])
    assert skills == ["skill-alpha", "skill-beta"]


def test_fts_and_operator(client):
    _publish(client, "skill-both", "alpha and beta together")
    _publish(client, "skill-alpha-only", "alpha only here")
    r = client.get(
        "/playbooks/search",
        params={"q": "alpha AND beta", "status": "all"},
    )
    skills = [r["skill_id"] for r in r.json()["results"]]
    assert skills == ["skill-both"]


def test_fts_phrase_match(client):
    _publish(client, "skill-phrase", "exact phrase here")
    _publish(client, "skill-words", "phrase exact here")
    r = client.get(
        "/playbooks/search",
        params={"q": '"exact phrase"', "status": "all"},
    )
    skills = [r["skill_id"] for r in r.json()["results"]]
    assert skills == ["skill-phrase"]


def test_search_limit_caps_results(client):
    """Default limit=5; mit limit=1 soll genau 1 Treffer kommen."""
    for i in range(3):
        _publish(client, f"skill-limit-{i}", f"limittest {i}")
    r = client.get(
        "/playbooks/search",
        params={"q": "limittest", "status": "all", "limit": 1},
    )
    assert r.json()["total"] == 1


def test_search_metadata_roundtrip(client):
    """metadata wird als JSON-String gespeichert und beim Read zurückkonvertiert."""
    body = {
        "skill_id": "meta-test",
        "problem_domain": "x",
        "problem_description": "metadata roundtrip",
        "approach": "x",
        "content": "x",
        "author_agent": "A",
        "metadata": {"latency_ms": 1500, "tags": ["foo", "bar"]},
    }
    pid = client.post("/playbooks/candidate", json=body).json()["id"]
    r = client.get(f"/playbooks/{pid}").json()
    assert r["metadata"]["latency_ms"] == 1500
    assert r["metadata"]["tags"] == ["foo", "bar"]
