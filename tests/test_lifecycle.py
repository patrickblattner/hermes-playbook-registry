"""Lifecycle: Auto-Promote, Auto-Demote, Auto-Archive älterer Versionen."""


def _publish(client, skill_id, author="agent-A", version_marker=""):
    r = client.post(
        "/playbooks/candidate",
        json={
            "skill_id": skill_id,
            "problem_domain": "x",
            "problem_description": "x",
            "approach": f"approach{version_marker}",
            "content": f"content{version_marker}",
            "author_agent": author,
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


def _validate(client, pid, agent, success):
    r = client.post(
        f"/playbooks/{pid}/validate",
        json={"validator_agent": agent, "success": success},
    )
    assert r.status_code == 201


def _status(client, pid):
    return client.get(f"/playbooks/{pid}").json()


def test_self_validation_does_not_promote(client):
    """Author allein kann sich nicht auf verified hochziehen."""
    pid = _publish(client, "self-only", author="A")
    for _ in range(5):
        _validate(client, pid, "A", True)
    assert _status(client, pid)["status"] == "candidate"


def test_auto_promote_after_cross_validation(client):
    """1 self + 2 external successes → wilson(3,3)=0.439 ≥ 0.4 → verified."""
    pid = _publish(client, "auto-promote", author="A")
    _validate(client, pid, "A", True)  # self, n=1
    _validate(client, pid, "B", True)  # ext_succ=1, n=2 — noch kein Promote
    assert _status(client, pid)["status"] == "candidate"
    _validate(client, pid, "B", True)  # ext_succ=2, n=3, conf=0.439 → Promote
    s = _status(client, pid)
    assert s["status"] == "verified"
    assert s["promoted_at"] is not None
    assert s["external_success_count"] == 2


def test_auto_demote_three_failures(client):
    """3 Failures → wilson(0,3)=0 < 0.3 → archived (auch ohne ext_succ-Pfad)."""
    pid = _publish(client, "demote-fast")
    for agent in ("B", "B", "C"):
        _validate(client, pid, agent, False)
    assert _status(client, pid)["status"] == "archived"


def test_auto_demote_keeps_promote_off(client):
    """2 ext_succ + 1 failure: wilson(2,3)=0.21 < 0.3 → archived statt verified."""
    pid = _publish(client, "mixed", author="A")
    _validate(client, pid, "B", True)
    _validate(client, pid, "B", True)
    _validate(client, pid, "C", False)
    s = _status(client, pid)
    # Demote dominiert: bei n=3 und conf<0.3 fliegt es raus.
    assert s["status"] == "archived"


def test_auto_archive_older_versions_on_promote(client):
    """v1 wird archiviert, sobald v2 desselben skill_id verified wird."""
    skill = "supersede-test"
    v1 = _publish(client, skill, author="A", version_marker="-v1")
    # v1 auf verified bringen
    _validate(client, v1, "A", True)
    _validate(client, v1, "B", True)
    _validate(client, v1, "B", True)
    assert _status(client, v1)["status"] == "verified"

    # v2 publishen + auf verified bringen
    v2 = _publish(client, skill, author="A", version_marker="-v2")
    _validate(client, v2, "A", True)
    _validate(client, v2, "C", True)
    _validate(client, v2, "C", True)

    # v2 ist jetzt verified, v1 muss archived sein
    assert _status(client, v2)["status"] == "verified"
    assert _status(client, v1)["status"] == "archived"


def test_manual_promote_also_archives_older(client):
    """Manueller Promote-Endpoint löst denselben Auto-Archive-Pfad aus."""
    skill = "manual-archive"
    v1 = _publish(client, skill, author="A", version_marker="-v1")
    # v1 auto-promote
    _validate(client, v1, "B", True)
    _validate(client, v1, "B", True)
    _validate(client, v1, "A", True)
    assert _status(client, v1)["status"] == "verified"

    v2 = _publish(client, skill, author="A", version_marker="-v2")
    # Manuell promoten
    r = client.post(f"/playbooks/{v2}/promote")
    assert r.status_code == 200

    assert _status(client, v2)["status"] == "verified"
    assert _status(client, v1)["status"] == "archived"


def test_archived_cannot_be_promoted(client):
    """Aus archived gibt's keinen API-Rückweg — 409."""
    pid = _publish(client, "no-resurrect")
    for _ in range(3):
        _validate(client, pid, "B", False)
    assert _status(client, pid)["status"] == "archived"

    r = client.post(f"/playbooks/{pid}/promote")
    assert r.status_code == 409
