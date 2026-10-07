"""A knowledge page's creation-time warning notice is platform metadata, never read or cited as page knowledge."""
from sqlalchemy import select
from test_wiki_reader_job import base_env  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.wiki_catalog import build_catalog
from fund_kb.wiki_compilation import instruction, resolve
from fund_kb.wiki_section_reader import read_scoped_pages

NOTICE = "模型生成修订候选，尚未核验。人工接受只创建新草稿；不改动旧版或自动发布。"


def test_answer_reading_skips_a_knowledge_pages_warning_notice(env):
    with env.db.begin() as db:
        for block in db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == env.wiki)):
            block.ordinal += 1000
        db.flush()
        data = {"text": NOTICE, "text_format": "markdown"}
        canonical = block_text({"block_type": "warning", "data": data})
        db.add(m.ContentBlock(version_id=env.wiki, block_id=svc.uid(), ordinal=0, block_type="warning", data=data,
                              locator={}, search_text=canonical, content_sha256=text_sha256(canonical)))
        db.flush()
        version = db.get(m.ResourceVersion, env.wiki)
        version.content_sha256 = svc.check_frozen_hash(db, version)
    with env.db() as db:
        pages = build_catalog(db, db.get(m.User, env.owner), env.space, {}, scope="reference")
        wiki_id = next(pid for pid, page in pages.items() if page["version_id"] == env.wiki)
        assert pages[wiki_id]["block_count"] >= 2
        records, missing, _, _ = read_scoped_pages(db, env.owner, env.space, pages, [wiki_id])
    assert not missing
    texts = [row["text"] for row in records if row["version_id"] == env.wiki]
    assert texts and all(NOTICE not in text for text in texts)


def test_compile_spec_keeps_platform_review_metadata_out_of_page_prose():
    text = instruction(resolve("topic"))
    assert "review_notice只是平台对来源的管理信息" in text and "不写“法律状态未知”" in text
    assert "未说明/待核验" not in text
