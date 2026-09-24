"""Actual worker and public read contract, synthetic only; no provider network."""
import json

from test_context_completion_job import document, final, setup
from test_reference_security_review import view
from test_wiki_reader_job import base_env  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414

from fund_kb import models as m


def test_terminal_run_exposes_measured_phases_but_not_fake_first_token(env, monkeypatch):
    source, ids = document(env, "合成计时依据", ["第一条", "完整条件与例外均以原文核对。"])
    def answer(calls, _):
        return "SEARCH 合成计时依据" if len(calls) == 1 else final(calls)
    rid, jid, calls, _, worker = setup(env, monkeypatch, source, ids[1], answer)
    try:
        worker._answer(jid, 1)
    finally:
        worker.close()
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        timing = run.model_snapshot["pipeline_timing"]
        assert run.state == "COMPLETED" and len(calls) == 2
        assert timing["phases"]["model_planning"]["calls"] == 1
        assert timing["phases"]["model_synthesis"]["calls"] == 1
        assert timing["phases"]["source_reading"]["calls"] >= 1
        assert timing["first_visible_answer_ms"] is None
        assert "合成计时依据" not in json.dumps(timing, ensure_ascii=False)
    assert view(env, rid)["model_snapshot"]["pipeline_timing"]["version"] == "answer_timing_v1"
    with env.db.begin() as db:
        db.get(m.Resource, source[0]).suspended = True
    hidden = view(env, rid)
    # Suspension invalidates present use; existing historical-answer visibility
    # follows the pre-existing permission contract, not this metrics feature.
    assert hidden["invalidated"]
    assert "pipeline_timing" not in hidden["model_snapshot"]


def test_stale_attempt_cannot_overwrite_newer_timing(env, monkeypatch):
    from fund_kb.answer_timing import AnswerTimings, persist_timings
    source, ids = document(env, "合成尝试", ["第一条", "完整原文。"])
    rid, jid, _, _, worker = setup(env, monkeypatch, source, ids[1], lambda calls, _: final(calls))
    try:
        with env.db.begin() as db:
            db.get(m.Job, jid).attempts = 2
            db.get(m.ConsultationRun, rid).model_snapshot = {"pipeline_timing": {"version": "newer"}}
        persist_timings(worker, jid, 1, AnswerTimings())
        with env.db() as db:
            assert db.get(m.ConsultationRun, rid).model_snapshot["pipeline_timing"] == {"version": "newer"}
    finally:
        worker.close()
