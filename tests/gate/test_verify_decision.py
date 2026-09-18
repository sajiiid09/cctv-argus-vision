"""The four-way outcome, exhaustively.

The order of the checks is the design, so the tests walk it in order. The one
that matters most is the last: `no_face` and `false` are different facts and
must never collapse into each other.
"""

from __future__ import annotations

import pytest
from argus.pipelines.gate.taps import TapChannel
from argus.pipelines.gate.verify import VerifyInput, VerifyOutcome, decide, describe


def _state(**kw) -> VerifyInput:
    params = dict(
        channel=TapChannel.LIVE,
        template_present=True,
        face_found=True,
        score=0.9,
        threshold=0.3,
    )
    params.update(kw)
    return VerifyInput(**params)  # type: ignore[arg-type]


def test_a_matching_face_is_true() -> None:
    verdict = decide(_state())
    assert verdict.outcome is VerifyOutcome.TRUE and verdict.score == 0.9


def test_a_different_face_is_false_and_keeps_its_score() -> None:
    verdict = decide(_state(score=0.1))
    assert verdict.outcome is VerifyOutcome.FALSE
    assert verdict.is_mismatch and verdict.score == 0.1


def test_no_usable_face_is_no_face_not_false() -> None:
    """A camera problem, not a person problem. Collapsing them destroys the only
    distinction the gate pipeline exists to make (ARCHITECTURE.md §7.2)."""
    verdict = decide(_state(face_found=False, score=None))
    assert verdict.outcome is VerifyOutcome.NO_FACE
    assert verdict.score is None


def test_a_replayed_tap_is_decided_before_anything_else_is_read() -> None:
    """Even with a face and a score in hand: the person walked past hours ago,
    so recording anything else would claim we looked."""
    verdict = decide(_state(channel=TapChannel.REPLAY, face_found=True, score=0.95))
    assert verdict.outcome is VerifyOutcome.NOT_ATTEMPTED
    assert "reader's log" in verdict.reason


def test_no_template_is_not_attempted_whatever_the_camera_saw() -> None:
    verdict = decide(_state(template_present=False))
    assert verdict.outcome is VerifyOutcome.NOT_ATTEMPTED
    assert "no face template" in verdict.reason


def test_no_measured_threshold_is_not_attempted_rather_than_a_guess() -> None:
    """ADR-0010: thresholds come from measured data. Without one there is no
    comparison to make, and saying so is the honest answer."""
    verdict = decide(_state(threshold=None))
    assert verdict.outcome is VerifyOutcome.NOT_ATTEMPTED
    assert "threshold" in verdict.reason


def test_exactly_at_the_threshold_matches() -> None:
    assert decide(_state(score=0.3, threshold=0.3)).outcome is VerifyOutcome.TRUE


def test_every_outcome_has_a_human_description() -> None:
    for outcome in VerifyOutcome:
        assert describe(outcome)
    assert "buddy-punching" in describe(VerifyOutcome.FALSE)
    assert "camera" in describe(VerifyOutcome.NO_FACE)


def test_the_sql_vocabulary_and_the_enum_agree() -> None:
    values = {outcome.value for outcome in VerifyOutcome}
    assert values == {"true", "false", "no_face", "not_attempted"}
    assert {channel.value for channel in TapChannel} == {
        "zkt_live",
        "zkt_replay",
        "simulated",
    }


def test_the_contract_module_is_stdlib_only() -> None:
    """taps.py is what two implementations and every test share, so a
    dependency in it is a dependency everywhere.

    Import statements only, not the file text: the module has to be able to
    *explain* which dependencies it is avoiding.
    """
    import re
    from pathlib import Path

    from argus.pipelines.gate import taps

    imports = re.findall(
        r"^\s*(?:from|import)\s+([\w.]+)", Path(taps.__file__).read_text(), re.MULTILINE
    )
    third_party = [
        name
        for name in imports
        if name.split(".")[0]
        not in {
            "__future__",
            "abc",
            "collections",
            "dataclasses",
            "datetime",
            "enum",
            "typing",
        }
    ]
    assert not third_party, f"{third_party} has no business in the tap contract"


@pytest.mark.parametrize("channel", list(TapChannel))
def test_no_channel_reaches_a_verdict_without_a_comparison(channel: TapChannel) -> None:
    verdict = decide(_state(channel=channel, template_present=False))
    assert verdict.outcome is VerifyOutcome.NOT_ATTEMPTED
