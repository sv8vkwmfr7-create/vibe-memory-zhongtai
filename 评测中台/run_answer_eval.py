#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测中台 · 答案级评测执行器（LLM-judge 协议）

与 run_eval.py（召回评测，每日管线）互补：本执行器在召回之上增加
「生成回答 → LLM 判分」两个环节，覆盖规则判分无法覆盖的公开基准题：
    benchmarks/locomo_adversarial.jsonl  446 道对抗题（带参考答案）
    benchmarks/longmemeval_abs.jsonl      30 道拒答题（expected_abstain）

流程（对每个评测集文件）：
    注入会话（pooled/isolate 按 jsonl 字段路由，与 run_eval 同口径）
      → 逐题召回 top-K → Qwen 生成回答 → llm_judge 判分
      → 存档 runs/ans-*.json（逐题落盘，中断可续）

用法：
    python run_answer_eval.py --evalset benchmarks/locomo_adversarial.jsonl [--limit 10]
    python run_answer_eval.py --evalset benchmarks/longmemeval_abs.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = Path("C:/Users/ASYS/Desktop/VibeMemory")
sys.path.insert(0, str(REPO))

from run_eval import import_vibe_memory, git_info, seed_sessions, apply_links  # noqa: E402
import llm_judge  # noqa: E402

RUNS_DIR = HERE / "runs"
DEFAULT_MODEL = REPO / "models" / "Qwen3-4B-Instruct-2507"
CONTEXT_CHAR_CAP = 12000  # 生成侧上下文上限（top-K 全文拼接截断；Qwen3-4B 32k 窗口，NF4 8GB 可容）


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_answer_evalset(path: Path, limit: int | None = None) -> list[dict]:
    """轻量加载（不走 run_eval.load_evalset 的召回校验——答案级评测无 trap_keywords）。"""
    items = []
    for i, ln in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        ln = ln.strip()
        if not ln:
            continue
        it = json.loads(ln)
        if not it.get("id") or not it.get("query"):
            print(f"[ans_eval] {path.name} 第{i}行缺 id/query，跳过", file=sys.stderr)
            continue
        items.append(it)
    return items[:limit] if limit else items


def top_contexts(res: dict, k: int) -> list[str]:
    atoms = res.get("atoms", [])[:k]
    out, total = [], 0
    for a in atoms:
        c = a.content
        if total + len(c) > CONTEXT_CHAR_CAP and out:
            break
        out.append(c)
        total += len(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="答案级评测（LLM-judge）")
    ap.add_argument("--evalset", required=True, help="benchmarks/ 下的 jsonl 路径")
    ap.add_argument("--limit", type=int, default=None, help="抽前 N 题（试跑用）")
    ap.add_argument("--k", type=int, default=5, help="召回 top-K")
    ap.add_argument("--model", default=str(DEFAULT_MODEL), help="本地模型目录")
    ap.add_argument("--agent-id", default="answer-eval")
    ap.add_argument("--cpu", action="store_true", help="强制 CPU（跳过 NF4 尝试）")
    args = ap.parse_args()

    evalset_path = Path(args.evalset)
    if not evalset_path.is_absolute():
        evalset_path = HERE / evalset_path
    items = load_answer_evalset(evalset_path, args.limit)
    if not items:
        print(f"[ans_eval] 评测集为空: {evalset_path}", file=sys.stderr)
        return 2
    isolate_mode = bool(items[0].get("isolate"))

    print(f"[ans_eval] vibe_memory | evalset={evalset_path.name} | {len(items)} 题 | "
          f"isolate={isolate_mode} | k={args.k}")

    vm, vm_import_path = import_vibe_memory(REPO)
    revision = git_info(REPO)
    memory = vm.VibeMemory(agent_id=args.agent_id, db_path=":memory:",
                           embedding_backend="tfidf")

    # ---------- 注入（pooled 一次注入；isolate 每题注入后即焚） ----------
    seen: set[str] = set()
    session_index: dict[str, dict] = {}
    if not isolate_mode:
        for it in items:
            seed_sessions(memory, it.get("sessions") or [],
                          ["ans_eval", it["type"], evalset_path.stem],
                          session_index, seen, auto_edges=True)
        try:
            memory.flush_index()
        except Exception as e:
            print(f"[ans_eval][警告] flush_index 失败（不阻断）: {e}", file=sys.stderr)
        stats = memory.stats()
        print(f"[ans_eval] 注入完成 atoms={stats.get('total_atoms', 0)} "
              f"edges={stats.get('total_edges', 0)}")

    # ---------- 模型 ----------
    print(f"[ans_eval] 加载模型: {args.model}")
    model_pack = llm_judge.load_model(args.model, prefer_gpu=not args.cpu)
    print(f"[ans_eval] 设备={model_pack['device']} 量化={model_pack['quantized']} "
          f"加载 {model_pack['load_ms']}ms")

    # ---------- 逐题：召回 → 生成 → 判分（增量落盘，中断可续） ----------
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    uniq = uuid.uuid4().hex[:6]
    run_id = f"ans-{ts}-{uniq}-{evalset_path.stem}"
    rows: list[dict] = []
    archive = RUNS_DIR / f"{run_id}.json"

    def flush():
        doc = {"run_id": run_id, "created_at": now_iso(), "evalset": evalset_path.name,
               "evalset_sha256": hashlib.sha256(evalset_path.read_bytes()).hexdigest(),
               "revision": revision,
               "vibe_memory_import_path": vm_import_path,
               "k": args.k, "model": model_pack["model_path"],
               "device": model_pack["device"], "quantized": model_pack["quantized"],
               "items_total": len(items), "rows": rows}
        archive.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    t0 = time.perf_counter()
    for idx, it in enumerate(items, 1):
        if isolate_mode:
            m2 = vm.VibeMemory(agent_id=f"{args.agent_id}-iso", db_path=":memory:",
                               embedding_backend="tfidf")
            seed_sessions(m2, it.get("sessions") or [],
                          ["ans_eval", it["type"], evalset_path.stem], {}, set(),
                          auto_edges=True)
            try:
                m2.flush_index()
            except Exception:
                pass
            res = m2.recall(it["query"], mode="precision", top_k=args.k)
            m2.close()
        else:
            res = memory.recall(it["query"], mode="precision", top_k=args.k)
        contexts = top_contexts(res, args.k)

        answer, prompt_tokens = llm_judge.generate_answer(model_pack, it["query"], contexts)
        expect_abstain = bool(it.get("expected_abstain"))
        is_adversarial = it.get("type") == "locomo_adversarial"
        reference = str(it.get("answer", "")) if not expect_abstain else \
            str(it.get("answer", "") or "（应弃答）")
        ranked = [a.session_id for a in res.get("atoms", [])]
        hit = bool(set(ranked[:args.k]) & set(it.get("expected_memory_ids") or []))
        if is_adversarial:
            # 对抗协议：answer 字段是误导性答案（诱饵）；证据文本取自 session_index（金标会话）
            bait = reference
            evidence_text = "\n".join(
                str(session_index.get(sid, {}).get("content", ""))
                for sid in (it.get("expected_memory_ids") or []) if sid in session_index)
            judge = llm_judge.judge_adversarial(model_pack, it["query"], bait,
                                                evidence_text, answer)
            rows.append({"id": it["id"], "type": it.get("type", ""),
                         "query": it["query"][:160],
                         "bait": bait[:200], "answer": answer[:300],
                         "verdict": judge["verdict"], "reasoning": judge["reasoning"],
                         "token_f1": llm_judge.token_f1(answer, bait),
                         "hit_at_k": hit,
                         "prompt_tokens": prompt_tokens,
                         "judge_ms": judge["judge_ms"]})
        else:
            judge = llm_judge.judge_answer(model_pack, it["query"], reference, answer,
                                           expect_abstain=expect_abstain)
            rows.append({"id": it["id"], "type": it.get("type", ""),
                         "query": it["query"][:160],
                         "reference": reference[:200], "answer": answer[:300],
                         "verdict": judge["verdict"], "reasoning": judge["reasoning"],
                         "token_f1": llm_judge.token_f1(answer, reference),
                         "hit_at_k": hit if it.get("expected_memory_ids") else None,
                         "prompt_tokens": prompt_tokens,
                         "judge_ms": judge["judge_ms"]})

        if idx % 10 == 0 or idx == len(items):
            flush()
            elapsed = time.perf_counter() - t0
            print(f"[ans_eval] {idx}/{len(items)} 题 "
                  f"({elapsed / idx:.1f}s/题，剩余约 {elapsed / idx * (len(items) - idx) / 60:.0f} 分钟)")

    flush()

    # ---------- 汇总 ----------
    dist: dict[str, int] = {}
    for r in rows:
        dist[r["verdict"]] = dist.get(r["verdict"], 0) + 1
    n = len(rows)
    if any(r["verdict"].startswith("abstain") for r in rows):
        ok = sum(1 for r in rows if r["verdict"] == "abstain_ok")
        summary_verdict = f"abstain_ok {ok}/{n} ({ok / max(1, n):.1%})"
    else:
        c = sum(1 for r in rows if r["verdict"] == "correct")
        summary_verdict = f"correct {c}/{n} ({c / max(1, n):.1%})"
    doc = json.loads(archive.read_text(encoding="utf-8"))
    doc["summary"] = {"verdict_distribution": dist, "duration_s": round(time.perf_counter() - t0, 1),
                      "headline": summary_verdict}
    archive.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[ans_eval] 完成 {n} 题 → {archive.name}")
    print(f"[ans_eval] 判定分布: {dist}")
    print(f"[ans_eval] 结论: {summary_verdict}")
    if not isolate_mode:
        memory.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
