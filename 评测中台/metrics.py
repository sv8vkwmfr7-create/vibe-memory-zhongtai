#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测中台 · 指标计算（判分层）

指标体系（对齐检索/记忆评测惯例）：
- Recall@K      : Top-K 内含任一标准会话的用例占比（主指标）
- MRR@K         : 标准会话首次命中排名倒数的均值（仅统计 K 内命中）
- nDCG@K        : 二元相关度的折损累计增益（支持多标准答案）
- KeywordHit@K  : 首个命中会话的正文包含全部期望关键词的用例占比（内容级校验）
- AbstentionPrecision : 拒答集通过率（Top-K 不含任何陷阱词的用例占比）

每个指标只在"适用该指标的用例"上取均值（如 KeywordHit 只算带关键词的用例），
适用范围与样本数随结果一起返回，避免隐性口径混淆。
"""
from __future__ import annotations

import math


def judge_item(item: dict, ranked_sessions: list[str], ranked_contents: list[str], k: int) -> dict:
    """对单条用例判分。

    ranked_sessions / ranked_contents：召回结果按排名的会话ID与正文（同序）。
    """
    expected = set(item.get("expected_memory_ids") or [])
    trap = item.get("trap_keywords")
    topk_sessions = ranked_sessions[:k]

    ranks = [i + 1 for i, s in enumerate(ranked_sessions) if s in expected]
    first_hit_rank = ranks[0] if ranks else None

    hit_at_k = first_hit_rank is not None and first_hit_rank <= k
    rr = (1.0 / first_hit_rank) if (first_hit_rank and first_hit_rank <= k) else 0.0

    # nDCG@K（二元相关）
    dcg = sum(1.0 / math.log2(i + 1) for i, s in enumerate(topk_sessions, start=1) if s in expected)
    ideal = min(len(expected), k) or 1
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal + 1))
    ndcg = dcg / idcg if idcg else 0.0

    # 会话级召回（Top-K 内命中的标准会话占比）
    recall = (len(set(topk_sessions) & expected) / len(expected)) if expected else None

    # 关键词校验：首个命中会话的正文需包含全部期望关键词
    kw_ok = None
    if item.get("expected_keywords") and first_hit_rank:
        content = ranked_contents[first_hit_rank - 1] if first_hit_rank <= len(ranked_contents) else ""
        kw_ok = all(kw in content for kw in item["expected_keywords"])

    # 拒答（负面用例）：Top-K 不得包含任何陷阱词
    abstain_ok = None
    if trap is not None:
        topk_contents = ranked_contents[:k]
        abstain_ok = not any(t in c for c in topk_contents for t in trap)

    return {
        "id": item["id"], "type": item.get("type", ""),
        "query": item["query"],
        "expected_memory_ids": sorted(expected),
        "expected_keywords": item.get("expected_keywords") or [],
        "first_hit_rank": first_hit_rank,
        "hit_at_k": hit_at_k if expected else None,
        "reciprocal_rank": rr if expected else None,
        "ndcg": ndcg if expected else None,
        "session_recall": recall,
        "keywords_ok": kw_ok,
        "abstain_ok": abstain_ok,
        "returned_top_k": [
            {"rank": i + 1, "session_id": s, "content_preview": (ranked_contents[i][:64] if i < len(ranked_contents) else "")}
            for i, s in enumerate(topk_sessions)
        ],
    }


def _mean(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def wilson_ci(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """Wilson 得分区间（小样本二项比例的推荐区间）。"""
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round(max(0.0, (c - r) / d), 4), round(min(1.0, (c + r) / d), 4)]


def aggregate(rows: list[dict]) -> dict:
    """把逐题判分聚合为指标集（含样本数、适用口径与 95% 置信区间）。"""
    m = {
        "recall_at_k": _mean([r["hit_at_k"] for r in rows if r["hit_at_k"] is not None]),
        "mrr": _mean([r["reciprocal_rank"] for r in rows if r["reciprocal_rank"] is not None]),
        "ndcg_at_k": _mean([r["ndcg"] for r in rows if r["ndcg"] is not None]),
        "keyword_hit": _mean([r["keywords_ok"] for r in rows if r["keywords_ok"] is not None]),
        "abstention_precision": _mean([r["abstain_ok"] for r in rows if r["abstain_ok"] is not None]),
    }
    n_hit = sum(1 for r in rows if r["hit_at_k"] is not None)
    n_kw = sum(1 for r in rows if r["keywords_ok"] is not None)
    n_abs = sum(1 for r in rows if r["abstain_ok"] is not None)
    m["sample_sizes"] = {"with_expected": n_hit, "with_keywords": n_kw, "abstention": n_abs}
    # 主指标：有标准答案用 Recall@K；纯拒答集用 AbstentionPrecision
    m["primary"] = "recall_at_k" if n_hit > 0 else "abstention_precision"
    m["primary_value"] = m["recall_at_k"] if n_hit > 0 else m["abstention_precision"]
    # 证据力：二项命中的 Wilson 95% CI（样本量直接决定区间宽度）
    m["recall_at_k_ci"] = wilson_ci(sum(1 for r in rows if r["hit_at_k"]), n_hit)
    m["abstention_precision_ci"] = wilson_ci(sum(1 for r in rows if r["abstain_ok"]), n_abs)
    return m


def per_type(rows: list[dict]) -> dict:
    types = sorted({r["type"] for r in rows})
    return {t: aggregate([r for r in rows if r["type"] == t]) for t in types}
