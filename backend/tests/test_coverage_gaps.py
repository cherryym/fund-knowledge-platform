"""Model-declared GAP aggregation: editors see the space, readers only their own runs."""
from test_wiki import env  # noqa: F401

from fund_kb import models as m
from fund_kb import services as svc


def _run(space_env, owner, gaps, *, state="COMPLETED"):
    tid, rid = svc.uid(), svc.uid()
    with space_env.db.begin() as db:
        db.add(m.ConsultationThread(id=tid, owner_id=owner, space_id=space_env.space, title="私密问题标题"))
        db.flush()
        done = state == "COMPLETED"
        db.add(m.ConsultationRun(id=rid, thread_id=tid, state=state, mode="answer",
            request={"question": "私密问题正文", "mode": "answer", "context": {}},
            response={"narrative_markdown": "合成答复"} if done else None,
            completed_at=svc.now() if done else None, error_code=None if done else "SYNTHETIC_FAILURE",
            model_snapshot={"coverage_gaps": {"planning": gaps[:1], "answer": gaps[1:], "source": "model_declared_unverified"}}))
    return rid


def test_editor_aggregate_reader_own_only_and_no_question_text(env):  # noqa: F811
    mine = _run(env, env.owner, ["财税〔2016〕140号原文｜确认增值税计税依据", "产品合同估值条款｜确认估值方法"])
    _run(env, env.owner, ["财税〔2016〕140号原文｜核对附加税"])
    theirs = _run(env, env.reader, ["托管协议复核约定｜确认复核分工"])
    _run(env, env.reader, ["不应计入的失败运行｜x"], state="FAILED")
    response = env.call("GET", f"/coverage-gaps?space_id={env.space}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "space" and body["runs_scanned"] == 3
    top = body["items"][0]
    assert top["gap"] == "财税〔2016〕140号原文" and top["runs"] == 2
    assert set(top["purposes"]) == {"确认增值税计税依据", "核对附加税"} and mine in top["own_run_ids"]
    other = next(item for item in body["items"] if item["gap"] == "托管协议复核约定")
    assert other["own_run_ids"] == [] and theirs not in response.text
    assert "私密问题" not in response.text and "不应计入" not in response.text
    env.login(env.reader)
    own = env.call("GET", f"/coverage-gaps?space_id={env.space}").json()
    assert own["scope"] == "own_runs" and [item["gap"] for item in own["items"]] == ["托管协议复核约定"]
