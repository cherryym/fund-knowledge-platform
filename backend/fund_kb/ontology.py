"""Valuation/accounting concept system as data: a decomposition scaffold, not a rule source.

The concepts help a model (or the no-model router) name the dimensions of a
question. They carry no legal effect and never replace reading actual sources.
"""
from __future__ import annotations

import json
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

PATH = Path(__file__).with_name("domain") / "valuation_accounting.json"


@lru_cache(maxsize=1)
def load():
    raw = PATH.read_bytes()
    data = json.loads(raw)
    dimensions = {d["id"] for d in data["dimensions"]}
    ids = [c["id"] for c in data["concepts"]]
    if data.get("schema_version") != 1 or len(ids) != len(set(ids)) or any(
            c["dimension"] not in dimensions for c in data["concepts"]):
        raise ValueError("ONTOLOGY_INVALID")
    return {**data, "sha256": sha256(raw).hexdigest()}


def legacy_router():
    return load()["legacy_router"]


def outline_text():
    """Compact outline for a planning prompt: dimensions with checks, concepts with synonyms."""
    data = load()
    lines = [f"概念体系（{data['domain']}，{data['ontology_version']}）：{data['note']}"]
    for dimension in data["dimensions"]:
        concepts = [c for c in data["concepts"] if c["dimension"] == dimension["id"]]
        names = "；".join(c["name"] + (f"（{'、'.join(c['synonyms'][:4])}）" if c.get("synonyms") else "")
                         for c in concepts)
        lines.append(f"- {dimension['name']}：检查{dimension['check']}。" + (f"概念：{names}" if names else ""))
    return "\n".join(lines)
