"""Unit-level candidate fusion; rankings are navigation, never proof of truth."""
from __future__ import annotations

import hashlib
import json
import re


def query_weights(query):
    """Language-level routing only: no asset, document-title or answer rules."""
    if re.search(r"第[一二三四五六七八九十百\d]+条|[《\"“].{3,}[》\"”]|文号|原文|条款", query):
        return {"vector": .30, "bm25": .60, "catalog": .10, "profile": "exact_reference"}
    if re.search(r"区别|比较|分别|流程|全过程|如何处理|怎么处理|步骤|凭证|分录", query):
        return {"vector": .45, "bm25": .35, "catalog": .20, "profile": "multi_aspect"}
    return {"vector": .55, "bm25": .35, "catalog": .10, "profile": "semantic"}


def fuse_units(query, units_by_channel, catalog_versions):
    """Merge identical current source spans, retaining ALL different sections."""
    weights, merged = query_weights(query), {}
    catalog_rank = {vid: i for i, vid in enumerate(catalog_versions, 1)}
    for channel, rows in units_by_channel.items():
        seen = set()
        for rank, row in enumerate(rows, 1):
            identity = [row["version_id"], row["resource_id"], row.get("source_spans"), row["text"]]
            uid = "unit_" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if uid in seen:
                continue
            seen.add(uid)
            unit = merged.setdefault(uid, {**row, "unit_id": uid, "score": 0.0, "channels": [], "channel_ranks": {}})
            unit["score"] += weights.get(channel, 0) / (60 + rank)
            unit["channels"].append(channel)
            unit["channel_ranks"][channel] = rank
    for unit in merged.values():
        if unit["version_id"] in catalog_rank:
            unit["score"] += weights["catalog"] / (60 + catalog_rank[unit["version_id"]])
            unit["channels"].append("catalog")
    return sorted(merged.values(), key=lambda u: (-u["score"], u["unit_id"])), weights
