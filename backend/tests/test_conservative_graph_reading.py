from test_evidence_router import connect, page, unit

from fund_kb.evidence_router import plan_evidence_reads


def test_unreviewed_graph_hub_is_not_automatically_expanded_but_remains_readable():
    pages = {p: page(p, "knowledge" if p != "source" else "document") for p in ["wiki", "hub", "source"]}
    connect(pages, "wiki", "source", blocks=["source-block"])
    connect(pages, "wiki", "hub", "APPLIES_TO", origin="proposed", verification_status="PROPOSED")
    units = [unit(pages, "wiki", "one", rerank_score=5), unit(pages, "hub", "two", rerank_score=-8)]
    first = plan_evidence_reads("晶核交接", pages, units, max_seed_units=1, conservative_graph=True)
    assert set(first["requested"]) == {"wiki", "source"} and "hub" in first["deferred_pages"]
    later = plan_evidence_reads("晶核交接", pages, [], conservative_graph=True, explicit_pages=["hub"])
    assert later["requested"] == ["hub"]


def test_explicit_model_read_gets_real_source_closure_without_synthetic_unit():
    pages = {"wiki": page("wiki", "knowledge"), "source": page("source")}
    connect(pages, "wiki", "source", blocks=["b"])
    result = plan_evidence_reads("任意问题", pages, [], conservative_graph=True,
        explicit_pages=["wiki", "not-authorized"])
    assert set(result["requested"]) == {"wiki", "source"}
    assert result["anchors"]["source"] == ["b"]
    assert "EXPLICIT_PAGE_NOT_ADMITTED" in result["warnings"]
