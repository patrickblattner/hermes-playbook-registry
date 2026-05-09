"""Wilson Score Lower Bound — bestätigt die Werte aus der SPEC-Tabelle."""

import math


def _wilson_lower():
    # Local import damit die Funktion nicht beim Test-Collection geladen wird
    # (sie hat keine Abhängigkeit zu DB_PATH, aber main.py hat — siehe conftest).
    from main import wilson_lower
    return wilson_lower


def test_zero_total_returns_zero():
    """n=0 hat keine Daten → 0.0 (kein Vertrauen)."""
    assert _wilson_lower()(0, 0) == 0.0


def test_one_one_is_low():
    """1/1=100% rohe Rate, aber wilson_lower ≈ 0.207 wegen kleiner Stichprobe."""
    assert math.isclose(_wilson_lower()(1, 1), 0.2065, abs_tol=0.01)


def test_nine_ten_beats_one_one():
    """Das gewünschte Verhalten: 9/10 schlägt 1/1."""
    assert _wilson_lower()(9, 10) > _wilson_lower()(1, 1)


def test_hundred_hundred_is_high():
    """100/100 muss deutlich über 0.95 liegen."""
    assert _wilson_lower()(100, 100) > 0.95


def test_zero_three_is_zero():
    """3 Failures, 0 Success → 0.0."""
    assert _wilson_lower()(0, 3) == 0.0


def test_thresholds_match_spec():
    """SPEC-Tabelle: konkrete Werte aus der Doku."""
    wl = _wilson_lower()
    # Schwelle 0.4 zündet bei 3/3
    assert wl(3, 3) >= 0.4
    # Aber nicht bei 2/2
    assert wl(2, 2) < 0.4
    # Schwelle 0.3 demoted bei 2/3
    assert wl(2, 3) < 0.3
    # Aber nicht bei 3/3
    assert wl(3, 3) >= 0.3
