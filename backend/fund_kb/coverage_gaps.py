"""Aggregate model-declared evidence gaps (GAP lines) into a library-completion list.

Gaps are unverified model statements about missing materials or facts. Editors
and administrators see the space aggregate; other readers see only their own
runs. The aggregate never includes other users' questions or run identifiers.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from . import models as m
from . import services as svc
from .wiki_reader import clean_gap

MAX_RUNS = 500


def _key(text):
    return re.sub(r"[\s《》“”\"'（）()、，,.。:：]", "", text).casefold()


def aggregate(db, user, space_id, *, max_runs=MAX_RUNS):
    svc.space_access(db, user, space_id)
    manage = bool(svc.roles(db, user, space_id) & {"admin", "editor"})
    query = (select(m.ConsultationRun.id, m.ConsultationRun.model_snapshot, m.ConsultationRun.created_at,
                    m.ConsultationThread.owner_id)
             .join(m.ConsultationThread, m.ConsultationThread.id == m.ConsultationRun.thread_id)
             .where(m.ConsultationThread.space_id == space_id, m.ConsultationThread.deleted_at.is_(None),
                    m.ConsultationRun.state == "COMPLETED", m.ConsultationRun.invalidated_at.is_(None)))
    if not manage:
        query = query.where(m.ConsultationThread.owner_id == user.id)
    groups, scanned = {}, 0
    for run_id, snapshot, created_at, owner_id in db.execute(
            query.order_by(m.ConsultationRun.created_at.desc()).limit(max_runs)):
        scanned += 1
        gaps = (snapshot or {}).get("coverage_gaps") or {}
        seen = set()
        for phase in ("planning", "answer"):
            for raw in gaps.get(phase) or []:
                text = clean_gap(str(raw))  # also normalizes records stored before the parser fix
                material, _, purpose = text.partition("｜")
                key = _key(material)
                if not key or key in seen:
                    continue
                seen.add(key)
                item = groups.setdefault(key, {"gap": material.strip()[:200], "purposes": [], "runs": 0,
                                                "first_seen": created_at, "last_seen": created_at, "own_run_ids": []})
                item["runs"] += 1
                item["first_seen"], item["last_seen"] = min(item["first_seen"], created_at), max(item["last_seen"], created_at)
                if purpose.strip() and purpose.strip()[:200] not in item["purposes"] and len(item["purposes"]) < 5:
                    item["purposes"].append(purpose.strip()[:200])
                if owner_id == user.id and len(item["own_run_ids"]) < 5:
                    item["own_run_ids"].append(run_id)
    items = sorted(groups.values(), key=lambda g: (-g["runs"], g["gap"]))
    return {"items": [{**g, "first_seen": svc.primitive(g["first_seen"]), "last_seen": svc.primitive(g["last_seen"])}
                      for g in items],
            "scope": "space" if manage else "own_runs", "runs_scanned": scanned, "max_runs": max_runs,
            "notes": "GAP为模型在答疑中声明的缺失资料或事实，未经核实；用于补充资料的线索，不是业务结论。"}
