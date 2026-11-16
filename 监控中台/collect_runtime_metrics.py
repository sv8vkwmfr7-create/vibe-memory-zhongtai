#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""监控中台 · 运行时指标采集器（可观测层）

对 VibeMemory SDK 执行确定性混合负载（写入 + 检索 + 图维护），采样运行时指标，
产出运行存档：监控中台/runs/obs-<时间戳>.json

采集内容：
- 操作延迟：store / recall 的 p50/p95/p99（逐操作计时，独立于 SDK 内部统计）
- 检索质量：命中率（已知答案查询）、零命中率（未入库查询）
- 图规模：atoms / edges / 队列；降级事件（MetricsCollector：ppr_to_topk 等）
- 主题分布：语料的 tags 分布（模拟真实记忆构成）

负载设计（固定种子可复现）：
- 1000 条记忆：60% 技术事实 / 25% 决策与更正 / 15% 偏好（中文为主）
- 400 次检索：70% 已知答案（应命中）、30% 未入库查询（期望零命中）
用法：
    python collect_runtime_metrics.py [--atoms 1000] [--queries 400]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"
DEFAULT_REPO = Path("C:/Users/ASYS/Desktop/VibeMemory")
sys.path.insert(0, str(DEFAULT_REPO))

SERVICES = ["订单服务", "支付服务", "库存服务", "网关服务", "搜索服务", "消息服务", "风控服务", "报表服务"]
ATTRS = [("API超时时间", ["3秒", "5秒", "10秒", "30秒"]), ("最大重试次数", ["2次", "3次", "5次"]),
         ("限流阈值", ["每分钟200次", "每分钟1000次"]), ("连接池上限", ["50", "100", "200"]),
         ("服务副本数", ["2个", "4个", "8个"]), ("内存上限", ["512MB", "2GB", "4GB"])]
DECISIONS = [("部署脚本默认端口改为{}", ["8080", "9000", "9100"]),
             ("发布窗口固定在每周{}", ["周三", "周四", "周五"]),
             ("缓存层最终选型{}", ["Redis", "Memcached", "Caffeine"])]
PREFS = [("代码注释使用中文", ), ("周报避免使用“赋能”", ), ("讲解方案先给结论", ), ("会议安排在上午10点前", )]
UNSEEN_QUERIES = ["公司食堂周三菜单是什么", "机场班车几点发车", "年会抽奖一等奖是什么",
                  "健身房热水器修好了吗", "差旅报销标准是多少", "停车月卡在哪里办"]


def pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return round(sorted_vals[i], 3)


PROJECTS = ["夜莺", "知更鸟", "海豚", "天琴", "磐石", "星舰", "青鸾", "灯塔"]


def build_corpus(n: int, rng: random.Random) -> list[dict]:
    """确定性语料：唯一组合（月份/项目锚点同时进正文与查询），避免同文重复导致的命中碰撞。"""
    corpus = []
    for i in range(n):
        anchor = f"{i % 12 + 1}月"
        proj = PROJECTS[i % len(PROJECTS)]
        r = rng.random()
        if r < 0.60:
            svc = rng.choice(SERVICES)
            attr, vals = rng.choice(ATTRS)
            val = rng.choice(vals)
            text = f"{anchor}记录：{proj}项目的{svc}的{attr}是{val}（条目{i:04d}）。"
            query = f"{anchor}{proj}项目的{svc}的{attr}是多少？"
            tags = ["fact", svc]
        elif r < 0.85:
            tpl, vals = rng.choice(DECISIONS)
            v = rng.choice(vals)
            body = tpl.format(v)
            text = f"{anchor}决定：{proj}项目{body}（条目{i:04d}）。"
            query = f"{anchor}{proj}项目{body[:6]}决定是什么？"
            tags = ["decision"]
        else:
            p = rng.choice(PREFS)[0]
            text = f"{anchor}确认{proj}项目用户偏好：{p}（条目{i:04d}）。"
            query = f"{anchor}{proj}项目偏好里{p[:5]}是什么？"
            tags = ["preference"]
        corpus.append({"session_id": f"obs-{i:04d}", "text": text, "query": query, "tags": tags})
    return corpus


def main() -> int:
    ap = argparse.ArgumentParser(description="监控中台运行时采集器")
    ap.add_argument("--atoms", type=int, default=1000)
    ap.add_argument("--queries", type=int, default=400)
    ap.add_argument("--seed", type=int, default=20261108)
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    args = ap.parse_args()

    from vibe_memory import VibeMemory
    from vibe_memory.metrics import MetricsCollector

    rng = random.Random(args.seed)
    corpus = build_corpus(args.atoms, rng)
    mc = MetricsCollector()
    memory = VibeMemory(agent_id="obs-platform", db_path=":memory:",
                        embedding_backend="tfidf")
    memory.metrics = mc

    # ---- 写入相位 ----
    store_ms, t0 = [], time.perf_counter()
    for item in corpus:
        s = time.perf_counter()
        memory.store(item["text"], session_id=item["session_id"], tags=item["tags"] + ["obs"])
        store_ms.append(round((time.perf_counter() - s) * 1000, 3))
    store_s = round(time.perf_counter() - t0, 2)
    memory.flush_index()

    # ---- 检索相位：70% 已知答案 / 30% 未入库（精确统计两类查询数） ----
    recall_ms, hits, zero_hit = [], 0, 0
    known_q = unseen_q = 0
    t1 = time.perf_counter()
    for i in range(args.queries):
        if rng.random() < 0.70:
            c = rng.choice(corpus)
            q, expect = c["query"], c["session_id"]
            known_q += 1
        else:
            q, expect = rng.choice(UNSEEN_QUERIES), None
            unseen_q += 1
        s = time.perf_counter()
        res = memory.recall(q, mode="precision", top_k=5)
        recall_ms.append(round((time.perf_counter() - s) * 1000, 3))
        sids = [a.session_id for a in res.get("atoms", [])]
        if expect is not None:
            if expect in sids:
                hits += 1
        elif not sids:
            zero_hit += 1
    recall_s = round(time.perf_counter() - t1, 2)

    stats = memory.stats()
    store_ms.sort()
    recall_ms.sort()
    n_total = args.queries
    doc = {
        "run_id": f"obs-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "workload": {"atoms_target": args.atoms, "queries": args.queries,
                      "known_ratio": 0.70, "seed": args.seed,
                      "corpus_mix": "60%事实/25%决策/15%偏好", "mode": "precision", "k": 5},
        "store": {"count": len(store_ms), "total_s": store_s,
                   "p50_ms": pct(store_ms, 0.5), "p95_ms": pct(store_ms, 0.95), "p99_ms": pct(store_ms, 0.99)},
        "recall": {"count": n_total, "total_s": recall_s,
                    "p50_ms": pct(recall_ms, 0.5), "p95_ms": pct(recall_ms, 0.95), "p99_ms": pct(recall_ms, 0.99),
                    "known_queries": known_q, "known_hits": hits,
                    "hit_rate": round(hits / max(1, known_q), 4),
                    "unseen_queries": unseen_q, "zero_hit_on_unseen": zero_hit,
                    "zero_hit_rate": round(zero_hit / max(1, unseen_q), 4)},
        "graph": {"total_atoms": stats.get("total_atoms"), "total_edges": stats.get("total_edges"),
                   "edge_labels": stats.get("edge_labels"),
                   "active_atoms": stats.get("active_atoms")},
        "sdk_metrics": {"degradation": (stats.get("metrics") or {}).get("degradation"),
                         "operations": (stats.get("metrics") or {}).get("operations"),
                         "recall_hit_rate": (stats.get("metrics") or {}).get("recall_hit_rate")},
        "environment": {"vibe_memory_version": __import__("vibe_memory").__version__,
                         "embedding_backend": "tfidf", "db_path": ":memory:",
                         "python": sys.version.split()[0]},
        "limits": "确定性合成负载的运行时观测，非真实流量；延迟为本机单线程测量，不代表并发 SLA",
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNS_DIR / f"{doc['run_id']}.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    # 文件指纹由数据中台采集器统一登记（self-hash 不可行：写入即改变自身）

    print(f"[obs] atoms={doc['graph']['total_atoms']} edges={doc['graph']['total_edges']}")
    print(f"[obs] store p50/p95/p99 = {doc['store']['p50_ms']}/{doc['store']['p95_ms']}/{doc['store']['p99_ms']} ms")
    print(f"[obs] recall p50/p95/p99 = {doc['recall']['p50_ms']}/{doc['recall']['p95_ms']}/{doc['recall']['p99_ms']} ms")
    print(f"[obs] 命中率={doc['recall']['hit_rate']} ({hits}/{known_q})  未入库零命中率={doc['recall']['zero_hit_rate']}")
    print(f"[obs] 存档 -> {out.name}")
    memory.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
