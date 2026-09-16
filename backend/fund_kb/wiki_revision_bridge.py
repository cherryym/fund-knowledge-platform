"""Keep a same-title compiled candidate as a review proposal, not a new page."""
from types import SimpleNamespace

from . import services as svc


def propose(db, user, settings, space_id, resource_id, page, selected, snapshots, job_id, compilation_config, batch):
    from .wiki import _members
    from .wiki_compilation import step_markdown, page_metadata
    from .wiki_maintenance import propose_compiled_revision

    blocks = []
    notice = "模型生成修订候选，尚未核验。人工接受只创建新草稿；不改动旧版或自动发布。"
    values = [{"markdown": notice, "evidence_ids": []}, *page["blocks"]]
    used = {eid for block in page["blocks"] for eid in block["evidence_ids"]}
    if page.get("links"):
        values.append({"markdown": "相关知识：" + "、".join(f"[[{title}]]" for title in page["links"]), "evidence_ids": sorted(used)})
    for ordinal, block in enumerate(values):
        records = [row for eid in block["evidence_ids"] for row in _members(selected[eid])]
        citations = {(row["version_id"], row["block_id"]): {"version_id": row["version_id"],
            "block_id": row["block_id"], "purpose": "FACT"} for row in records}
        text = block["markdown"]
        if block.get("step"):
            text += "\n\n" + step_markdown(block["step"])
        blocks.append({"block_id": svc.uid(), "ordinal": ordinal, "block_type": "warning" if ordinal == 0 else "paragraph",
            "data": {"text": text, "text_format": "markdown"}, "citations": list(citations.values()),
            "locator": {"label": f"Wiki修订候选段落 {ordinal + 1}", "generation_job_id": job_id,
                "source_spans": [{"version_id": r["version_id"], "block_id": r["block_id"],
                    "char_start": r.get("char_start", 0), "char_end": r.get("char_end", len(r["text"]))} for r in records]}})
    request = SimpleNamespace(path_params={"id": resource_id}, headers={}, state=SimpleNamespace(trace_id=job_id),
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    ctx = svc.Context(request, db, user, {}, {}, "proposeCompiledWikiRevision")
    proposal = propose_compiled_revision(ctx, resource_id,
        {"title": page["title"], "blocks": blocks, "knowledge_type": page["knowledge_type"]}, snapshots,
        compilation_job_id=job_id)
    # Keep the compiler specification beside the proposal without making it a
    # proof of business validity or changing the old version's specification.
    from . import models as m
    policy = db.get(m.RuntimePolicy, proposal["id"])
    policy.config = {**policy.config, "compilation_metadata": page_metadata(page, compilation_config, batch)}
    db.flush()
    return proposal["id"], used
