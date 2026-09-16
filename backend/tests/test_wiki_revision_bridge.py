"""Full queued compiler -> proposal preview -> new draft acceptance chain."""
from sqlalchemy import select

from test_wiki_compilation import env, provider, request, queue, finish, page, isolated_configuration_and_network
from fund_kb import models as m, services as svc


def test_same_title_compiler_proposes_complete_revision_not_silent_skip_or_overwrite(env, provider):
    source = page(env, "已发布合成来源", kind="document")
    original = page(env, "已存在的合成规则", text="原规则完整正文，不能覆盖。", cites=[source])
    with env.db() as db:
        frozen = svc.version_dict(db, db.get(m.ResourceVersion, original[1]))
        released = db.get(m.Resource, original[0]).active_release_id
    def rename(output, data):
        output["pages"][0]["title"] = "已存在的合成规则"
        output["pages"][0]["blocks"][0]["markdown"] += "\n\n" + "完整规则及边界条件不能截断。" * 2000
        return output
    provider.output_transform = rename
    result = finish(env, queue(env, request(env, [source], "atomic_rule", source_mode="published")))
    assert result["created_version_ids"] == [] and len(result["revision_proposal_ids"]) == 1
    assert result["batch"]["counts"]["REVIEW_REQUIRED"] > 0
    pid = result["revision_proposal_ids"][0]
    path = f"/wiki/maintenance/proposals/{pid}"
    proposal = env.call("GET", path + "?include_candidate=true")
    assert proposal.status_code == 200, proposal.text
    data = proposal.json()
    assert data["status"] == "PROPOSED" and data["has_compiled_candidate"]
    candidate = data["compiled_revision"]["candidate"]
    assert len(candidate["blocks"]) == 7  # warning plus six complete rule sections
    assert all(b["citations"] for b in candidate["blocks"][1:])
    with env.db() as db:
        assert svc.version_dict(db, db.get(m.ResourceVersion, original[1])) == frozen
    accepted = env.call("POST", path + "/review", {"decision":"ACCEPT", "comment":"合成验收只接受为待核验修订稿"},
        etag=proposal.headers["etag"])
    assert accepted.status_code == 200, accepted.text
    new_id = accepted.json()["result"]["new_version_id"]
    with env.db() as db:
        new = db.get(m.ResourceVersion, new_id)
        assert new.resource_id == original[0] and new.state == "DRAFT" and new.base_version_id == original[1]
        assert svc.version_dict(db, db.get(m.ResourceVersion, original[1])) == frozen
        assert db.get(m.Resource, original[0]).active_release_id == released
        text = "\n".join(db.scalars(select(m.ContentBlock.search_text).where(m.ContentBlock.version_id == new_id)))
        assert "## applicability" in text and "## exceptions" in text and "## review_and_sources" in text
        assert new_id != original[1]
        assert len(text) > 20000
        assert new.origin == "AI_DRAFT" and new.legal_status == "UNKNOWN" and not new.source_verified
        metadata = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == f"wiki-compilation:{new_id}"))
        assert metadata.config["compiled_content_sha256"] == new.content_sha256
    assert provider.calls == 1
