import json

import pytest

from fund_kb.answer_timing import AnswerTimings


def test_monotonic_phase_measurement_survives_exception():
    now = [0.0]
    clock = AnswerTimings(lambda: now[0])
    with pytest.raises(RuntimeError), clock.measure("model_planning"):
        now[0] = 1.25
        raise RuntimeError("SENSITIVE_SHOULD_NOT_BE_RECORDED")
    now[0] = 2.0
    value = clock.snapshot()
    assert value["phases"]["model_planning"] == {"elapsed_ms": 1250.0, "calls": 1}
    assert value["execution_elapsed_ms"] == 2000
    assert value["first_visible_answer_ms"] is None
    assert value["first_visible_answer_status"] == "NOT_MEASURED"
    assert "SENSITIVE" not in json.dumps(value)


def test_phase_names_cannot_be_raw_question_text():
    with pytest.raises(ValueError):
        AnswerTimings().add("private source text", 1)


def test_multiple_calls_sum_within_named_phase_only():
    clock = AnswerTimings()
    clock.add("source_reading", 1)
    clock.add("source_reading", 2)
    assert clock.snapshot()["phases"]["source_reading"] == {"elapsed_ms": 3000.0, "calls": 2}
    assert clock.snapshot()["durations_additive"] is False
