"""Real authenticated temporary app/API, no runtime DB or external provider."""
from test_wiki import env, page


def test_catalog_http_full_metadata_and_content_endpoint_remain_separate(env):
    source = page(env, "目录读取验收原文", kind="document", text="正文唯一标记不得出现在目录里。")
    knowledge = page(env, "目录读取验收规则", cites=[source])
    hidden = page(env, "隐藏目录不得泄露", restricted=True)
    response = env.call("GET", f"/wiki/catalog?space_id={env.space}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["body_blocks_loaded"] == 0 and body["truncated"] is False
    assert {p["resource_id"] for p in body["items"]} == {source[0], knowledge[0]}
    assert "正文唯一标记" not in response.text and hidden[0] not in response.text
    assert all("records" not in p and not p["body_loaded"] for p in body["items"])
    assert response.headers["etag"].strip('"') == body["snapshot"]
    content = env.call("GET", f"/versions/{source[1]}")
    assert content.status_code == 200 and "正文唯一标记" in content.text


def save_metadata(env, rid, name, aliases):
    path = f"/wiki/entries/{rid}/maintenance"
    current = env.call("GET", path)
    assert current.status_code == 200, current.text
    updated = env.call("PUT", path, {"canonical_key": name, "aliases": aliases, "reason": "合成测试统一名称"},
        etag=current.headers["etag"])
    assert updated.status_code == 200, updated.text
    return updated.json()


def test_saved_alias_resolves_in_catalog_workspace_graph_and_full_cached_readers(env):
    source = page(env, "合成主条目")
    link = page(env, "合成引用页", text="需核对[[合成新别名]]。")
    save_metadata(env, source[0], "合成稳定业务标识", ["合成新别名"])
    resolved = env.call("GET", f"/wiki/resolve?space_id={env.space}&title=合成新别名")
    assert resolved.status_code == 200 and resolved.json()["resource_id"] == source[0]
    atlas = env.call("GET", f"/wiki/workspace?space_id={env.space}&hydrate=true")
    assert atlas.status_code == 200, atlas.text
    body = atlas.json()
    assert body["maintenance"][source[0]]["aliases"] == ["合成新别名"]
    assert set(body["readers"]) == {source[0], link[0]}
    assert body["readers"][link[0]]["links"]["unresolved"] == []
    assert any(e["target"] == source[0] and e["source"] == link[0] for e in body["graph"]["edges"])
    filtered = env.call("GET", f"/wiki/workspace?space_id={env.space}&q=合成新别名")
    assert source[0] in {p["id"] for p in filtered.json()["pages"]}
    catalog = env.call("GET", f"/wiki/catalog?space_id={env.space}")
    assert any("合成新别名" in p["aliases"] for p in catalog.json()["items"])


def test_removing_legacy_alias_does_not_resurrect_it_from_historical_tags(env):
    source = page(env, "当前主条目", tags=["alias:旧简称"])
    linked = page(env, "引用旧简称的页面", text="[[旧简称]]")
    assert env.call("GET", f"/wiki/resolve?space_id={env.space}&title=旧简称").status_code == 200
    save_metadata(env, source[0], "稳定业务标识", [])
    assert env.call("GET", f"/wiki/resolve?space_id={env.space}&title=旧简称").status_code == 404
    atlas = env.call("GET", f"/wiki/workspace?space_id={env.space}&hydrate=true").json()
    assert atlas["maintenance"][source[0]]["aliases"] == []
    assert atlas["readers"][linked[0]]["links"]["unresolved"] == [{"title":"旧简称"}]
    catalog = env.call("GET", f"/wiki/catalog?space_id={env.space}").json()
    item = next(p for p in catalog["items"] if p["resource_id"] == source[0])
    assert "旧简称" not in item["aliases"] and "稳定业务标识" in item["aliases"]


def test_approved_navigation_consolidation_does_not_delete_or_hide_original_pages(env):
    first, second = page(env, "主知识条目"), page(env, "次知识条目")
    link = page(env, "双链页", text="阅读[[次知识条目]]。")
    response = env.call("POST", "/wiki/maintenance/proposals", {"space_id": env.space, "kind": "CONSOLIDATION",
        "resource_ids": [first[0], second[0]], "target_resource_id": first[0], "reason": "合成测试仅统一导航，不合并正文"})
    assert response.status_code == 201, response.text
    pid = response.json()["id"]
    accepted = env.call("POST", f"/wiki/maintenance/proposals/{pid}/review",
        {"decision": "ACCEPT", "comment": "核对后保留两个原文，只统一入口"}, etag=response.headers["etag"])
    assert accepted.status_code == 200, accepted.text
    resolved = env.call("GET", f"/wiki/resolve?space_id={env.space}&title=次知识条目")
    assert resolved.status_code == 200 and resolved.json()["resource_id"] == first[0]
    atlas = env.call("GET", f"/wiki/workspace?space_id={env.space}&hydrate=true").json()
    assert len(atlas["pages"]) == 3 and set(atlas["readers"]) == {first[0], second[0], link[0]}
    assert atlas["readers"][second[0]]["version"]["id"] == second[1]
    assert any(e["id"] == first[0] for e in atlas["readers"][link[0]]["links"]["outgoing"])
