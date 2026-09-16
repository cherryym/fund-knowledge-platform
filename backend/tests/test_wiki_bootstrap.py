"""Complete initial workspace hydration; synthetic SQLite and HTTP only."""
from types import SimpleNamespace

from sqlalchemy import select
from test_wiki import env as env, page  # noqa: F401

from fund_kb import models as m, services as svc, wiki


def test_hydration_returns_all_pages_graph_and_selected_reader_without_old_caps(env):
    records = [page(env, f"完整知识{i:03d}") for i in range(205)]
    hub = page(env, "全量阅读入口", text=" ".join(f"[[完整知识{i:03d}]]" for i in range(205)))
    response = env.call("GET", f"/wiki/workspace?space_id={env.space}&hydrate=true")
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["pages"]) == data["stats"]["total_pages"] == 206
    assert data["truncated"] is False
    assert {p["id"] for p in data["pages"]} == {r[0] for r in records} | {hub[0]}
    assert len(data["graph"]["nodes"]) == 206
    assert len(data["graph"]["edges"]) == 205
    assert data["graph"]["truncated"] is False
    first = data["pages"][0]
    reader = data["reader"]
    assert reader["resource"]["id"] == reader["version"]["resource_id"] == first["id"]
    assert reader["version"]["id"] == first["version_id"]
    assert reader["version"] == env.call("GET", "/versions/" + first["version_id"]).json()
    assert reader["links"] == env.call("GET", f"/wiki/pages/{first['id']}/links").json()
    assert data["graph"] == env.call("GET", f"/wiki/graph?space_id={env.space}").json()
    # All outgoing links are returned even if the default reader changes order.
    links = env.call("GET", f"/wiki/pages/{hub[0]}/links").json()
    assert len(links["outgoing"]) == 205 and links["truncated"] is False


def test_hydration_does_not_expose_hidden_sources_or_manuscripts(env):
    public = page(env, "可见知识")
    hidden = page(env, "不可见知识")
    with env.db.begin() as db:
        db.get(m.Resource, hidden[0]).restricted = True
    result = env.call("GET", f"/wiki/workspace?space_id={env.space}&hydrate=true")
    assert result.status_code == 200, result.text
    assert hidden[0] not in result.text and hidden[1] not in result.text
    assert [p["id"] for p in result.json()["pages"]] == [public[0]]
    assert result.json()["reader"]["resource"]["id"] == public[0]


def test_empty_hydration_has_no_fabricated_reader_or_graph_nodes(env):
    result = env.call("GET", f"/wiki/workspace?space_id={env.space}&hydrate=true")
    assert result.status_code == 200, result.text
    assert result.json()["pages"] == []
    assert "reader" not in result.json()
    assert result.json()["graph"]["nodes"] == result.json()["graph"]["edges"] == []


def test_taxonomy_read_does_not_truncate_existing_categories(env):
    # Exercise the projection's old read cap independently of creation limits.
    pages = {str(i): {"resource": SimpleNamespace(kind="knowledge", category=f"分类{i}")} for i in range(1005)}
    with env.db() as db:
        data = wiki.taxonomy(db, db.get(m.User, env.owner), env.space, pages=pages)
    assert len(data["categories"]) == 1005 and data["truncated"] is False
    assert sum(item["count"] for item in data["categories"]) == 1005
