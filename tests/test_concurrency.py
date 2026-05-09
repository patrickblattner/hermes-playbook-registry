"""Race-Condition-Tests: parallele Promotes + parallele Submits."""

import concurrent.futures


def _publish(client, skill_id):
    r = client.post(
        "/playbooks/candidate",
        json={
            "skill_id": skill_id,
            "problem_domain": "x",
            "problem_description": "x",
            "approach": "x",
            "content": "x",
            "author_agent": "A",
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


def test_concurrent_promote_only_one_succeeds(client):
    """5 parallele Promote-Requests: 1× 200, 4× 409."""
    pid = _publish(client, "race-promote")

    def promote(_):
        return client.post(f"/playbooks/{pid}/promote")

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(promote, range(5)))

    success = [r for r in results if r.status_code == 200]
    conflict = [r for r in results if r.status_code == 409]
    assert len(success) == 1, f"expected exactly 1 success, got {len(success)}"
    assert len(conflict) == 4


def test_concurrent_version_submits_all_succeed(client):
    """5 parallele Submits zum gleichen skill_id ohne Idem-Key — alle erfolgreich,
    Versionen 1..5 ohne Duplikate."""
    skill = "race-version"
    body = {
        "skill_id": skill,
        "problem_domain": "x",
        "problem_description": "x",
        "approach": "x",
        "content": "x",
        "author_agent": "A",
    }

    def submit(_):
        return client.post("/playbooks/candidate", json=body)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(submit, range(5)))

    assert all(r.status_code == 201 for r in results)
    versions = sorted(r.json()["version"] for r in results)
    assert versions == [1, 2, 3, 4, 5]


def test_concurrent_idempotency_one_canonical(client):
    """5 parallele Submits mit gleichem Idem-Key: alle dieselbe id, 1× replay=False."""
    body = {
        "skill_id": "race-idem",
        "problem_domain": "x",
        "problem_description": "x",
        "approach": "x",
        "content": "x",
        "author_agent": "A",
        "idempotency_key": "shared-uuid-xxx",
    }

    def submit(_):
        return client.post("/playbooks/candidate", json=body)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(submit, range(5)))

    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1, f"expected 1 canonical id, got {ids}"
    replays = [r for r in results if r.json()["idempotent_replay"]]
    originals = [r for r in results if not r.json()["idempotent_replay"]]
    assert len(originals) == 1
    assert len(replays) == 4
