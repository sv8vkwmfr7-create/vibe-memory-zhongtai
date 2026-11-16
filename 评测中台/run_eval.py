#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测中台 · 评测执行器

流程：加载评测集(校验) → 注入会话(store) → 跨会话建边(flush_index)
      → 逐题召回(recall) → 判分 → 运行存档(runs/run-*.json, 可复现)

两种记忆协议（自动按用例字段路由）：
- pooled（默认）：一次调用中所有普通用例共享同一个 VibeMemory 实例，
  模拟真实单 Agent 记忆库，评测集之间天然构成语义近邻干扰；
- isolated：用例带 "isolate": true 时，逐题建独立实例，只注入该题自己的
  会话与 links——用于 LoCoMo/LongMemEval 这类"每题独立记忆库"的公开基准协议。

其他要点：
- 默认 db_path=":memory:"、embedding_backend="tfidf"：零依赖、离线、可复现；
- --limit N 可对每个评测集抽样前 N 条（冒烟用，note 自动标注）；
- 若 baseline.json 存在则自动执行门禁对比，结果写入运行存档的 gate 字段。

用法：
    python run_eval.py --all                       # 跑 evalsets/ 下全部评测集
    python run_eval.py --evalset evalsets/temporal.jsonl [--k 5] [--limit 20]
    python run_eval.py --evalset benchmarks/locomo_c01.jsonl   # 公开基准（逐文件独立实例）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_REPO = Path("C:/Users/ASYS/Desktop/VibeMemory")
EVALSETS_DIR = HERE / "evalsets"
RUNS_DIR = HERE / "runs"
BASELINE_PATH = HERE / "baseline.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def import_vibe_memory(repo: Path):
    """强制从 --repo 指定的仓库目录导入 vibe_memory。

    2026-11-14 修复：旧实现先 import 环境里已有的 vibe_memory（pip install -e
    或其它 sys.path 命中的副本），导致 --repo 参数被无视——评测可能测错代码。
    现在按文件路径直接加载指定仓库的包（子模块经 submodule_search_locations 解析），
    并清掉同进程已加载的残留模块。返回 (模块, 实际导入路径)。"""
    import importlib.util

    repo = Path(repo).resolve()
    pkg_dir = repo / "vibe_memory"
    pkg_init = pkg_dir / "__init__.py"
    if not pkg_init.exists():
        print(f"[run_eval] {repo} 下没有 vibe_memory 包（缺 {pkg_init}）", file=sys.stderr)
        print("[run_eval] 请用 --repo 指定 VibeMemory 仓库根目录", file=sys.stderr)
        sys.exit(3)
    for m in [k for k in list(sys.modules) if k == "vibe_memory" or k.startswith("vibe_memory.")]:
        del sys.modules[m]
    # repo 放 sys.path 最前：即便 spec 加载，子模块相对解析与依赖查找也优先命中本仓库
    if str(repo) in sys.path:
        sys.path.remove(str(repo))
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location(
        "vibe_memory", pkg_init, submodule_search_locations=[str(pkg_dir)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vibe_memory"] = mod
    spec.loader.exec_module(mod)
    return mod, str(pkg_dir)


def git_info(repo: Path) -> dict:
    """采集被测代码版本：commit / 分支 / 未提交改动。git 不可用时如实记 None。"""
    def _run(*args):
        try:
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=15).stdout.strip()
        except Exception:
            return ""
    commit = _run("rev-parse", "HEAD")
    if not commit:
        return {"git_available": False}
    status_out = _run("status", "--porcelain")
    dirty_lines = [ln for ln in status_out.splitlines() if ln.strip()]
    return {"git_available": True,
            "commit": commit[:12], "branch": _run("rev-parse", "--abbrev-ref", "HEAD") or None,
            "dirty_count": len(dirty_lines),
            "dirty_files": [ln[3:].strip() for ln in dirty_lines[:20]],
            "note": "未提交改动会改变被测代码；引用本存档指标时须结合 dirty 记录"}


def load_evalset(path: Path, limit: int | None = None) -> tuple[list[dict], set[str]]:
    """加载并校验评测集，返回 (用例列表, 本文件会出现的全部 session_id)。"""
    items, errors = [], []
    for i, ln in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        ln = ln.strip()
        if not ln:
            continue
        try:
            it = json.loads(ln)
        except json.JSONDecodeError as e:
            errors.append(f"  第{i}行 JSON 解析失败: {e}")
            continue
        missing = [k for k in ("id", "type", "query") if not it.get(k)]
        if missing:
            errors.append(f"  第{i}行 缺少字段 {missing}")
            continue
        if not it.get("expected_memory_ids") and it.get("trap_keywords") is None:
            errors.append(f"  第{i}行 ({it.get('id')}) 既无 expected_memory_ids 也无 trap_keywords")
            continue
        items.append(it)
        if limit and len(items) >= limit:
            break
    if errors:
        print(f"[run_eval] 评测集 {path.name} 校验失败：", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        sys.exit(2)
    sids = {s["session_id"] for it in items for s in (it.get("sessions") or []) if s.get("session_id")}
    return items, sids


def make_label(raw):
    from vibe_memory.models.memory_atom import EdgeLabel
    try:
        return EdgeLabel(raw)
    except ValueError:
        return EdgeLabel[str(raw).upper()]


def apply_links(memory, link_specs: list[dict], session_index: dict) -> int:
    """link_specs: [{from,to,label,confidence}]，引用 session_index 里的 atom_id。"""
    built = 0
    for lk in link_specs:
        src = session_index.get(lk["from"])
        dst = session_index.get(lk["to"])
        if not src or not dst:
            print(f"[run_eval][警告] link 引用的会话不存在: {lk}", file=sys.stderr)
            continue
        try:
            memory.link(src["atom_id"], dst["atom_id"], label=make_label(lk.get("label", "同类经验")),
                        confidence=float(lk.get("confidence", 0.9)))
            built += 1
        except Exception as e:
            print(f"[run_eval][警告] 建边失败 {lk}: {e}", file=sys.stderr)
    return built


def seed_sessions(memory, sessions: list[dict], tags: list[str],
                  session_index: dict, seen_global: set, auto_edges: bool = True) -> int:
    n = 0
    for ses in sessions:
        sid = ses["session_id"]
        if sid in seen_global:
            print(f"[run_eval][警告] session_id 重复，跳过: {sid}", file=sys.stderr)
            continue
        atom = memory.store(ses["text"], session_id=sid, tags=tags,
                            auto_build_edges=auto_edges)
        session_index[sid] = {"atom_id": atom.id, "content": ses["text"]}
        seen_global.add(sid)
        n += 1
    return n


def run_gate(evalset_name: str, metrics: dict) -> dict:
    """若 baseline.json 存在则对比门禁；否则返回未初始化状态。"""
    if not BASELINE_PATH.exists():
        return {"status": "no_baseline", "detail": "尚未初始化基线（python gate.py --init）"}
    base = json.loads(BASELINE_PATH.read_text(encoding="utf-8-sig"))
    b = base.get("evalsets", {}).get(evalset_name)
    if not b:
        return {"status": "no_baseline_for_set", "detail": "基线中无此评测集"}
    details, fail = [], False
    for metric, threshold in (b.get("thresholds") or {}).items():
        got = metrics.get(metric)
        if got is None:
            continue
        ok = got >= threshold - 1e-9
        details.append({"metric": metric, "value": got, "threshold": threshold, "pass": ok})
        if not ok:
            fail = True
    return {"status": "FAIL" if fail else "PASS",
            "baseline_run_id": b.get("run_id"), "details": details}


def main() -> int:
    ap = argparse.ArgumentParser(description="VibeMemory 评测执行器")
    ap.add_argument("--all", action="store_true", help="运行 evalsets/ 下全部评测集")
    ap.add_argument("--evalset", default=None, help="评测集 JSONL 路径（可传多个，逗号分隔）")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--mode", default="precision", choices=["precision", "recall", "budget"])
    ap.add_argument("--limit", type=int, default=None,
                    help="每个评测集只取前 N 条用例（抽样冒烟；note 自动标注）")
    ap.add_argument("--no-edges", action="store_true",
                    help="消融开关：store 不自动建边、跳过 flush_index 与显式 links——量化图检索贡献")
    ap.add_argument("--repo", default=str(DEFAULT_REPO), help="VibeMemory 仓库根目录")
    ap.add_argument("--agent-id", default="eval-platform")
    ap.add_argument("--note", default="", help="运行备注（如：调参后复跑）")
    args = ap.parse_args()

    if args.evalset:
        evalset_paths = [Path(p) for p in args.evalset.split(",") if p.strip()]
    elif args.all:
        evalset_paths = sorted(EVALSETS_DIR.glob("*.jsonl"))
    else:
        ap.error("请指定 --all 或 --evalset")
    if not evalset_paths:
        print("[run_eval] 没有找到评测集", file=sys.stderr)
        return 2

    vm, vm_import_path = import_vibe_memory(Path(args.repo))
    revision = git_info(Path(args.repo))
    from vibe_memory import VibeMemory
    from metrics import aggregate, judge_item, per_type  # 本目录模块

    note = args.note
    if args.limit:
        note = (note + " | " if note else "") + f"limit={args.limit}（抽样运行，非全量）"
    if args.no_edges:
        note = (note + " | " if note else "") + "--no-edges 消融（无图边）"

    print(f"[run_eval] vibe_memory v{vm.__version__} | Python {platform.python_version()} "
          f"| mode={args.mode} k={args.k}" + (f" | limit={args.limit}" if args.limit else ""))

    # ---------- 加载（含跨题 session_id 重复检测：重复会造成静默假 FAIL，必须报错） ----------
    loaded: list[tuple[Path, list[dict]]] = []
    seen_global: set[str] = set()
    sid_owner: dict[str, str] = {}
    dup_errors = []
    for path in evalset_paths:
        items, sids = load_evalset(path, args.limit)
        loaded.append((path, items))
        for sid in sorted(sids):
            if sid in sid_owner:
                dup_errors.append(f"  session_id 跨用例重复: {sid}  （{sid_owner[sid]} 与 {path.name}）")
            else:
                sid_owner[sid] = path.name
    if dup_errors:
        print(f"[run_eval] session_id 重复检测失败：", file=sys.stderr)
        print("\n".join(dup_errors[:20]), file=sys.stderr)
        sys.exit(2)

    # ---------- 注入阶段：pooled 用例共享一个实例；isolated 用例逐题独立 ----------
    memory = VibeMemory(agent_id=args.agent_id, db_path=":memory:", embedding_backend="tfidf")
    session_index: dict[str, dict] = {}
    pooled_links: list[tuple[str, list[dict]]] = []
    t0 = time.perf_counter()
    pooled_stores = 0
    for path, items in loaded:
        for it in items:
            if it.get("isolate"):
                continue
            pooled_stores += seed_sessions(memory, it.get("sessions") or [],
                                           ["eval", it["type"], path.stem],
                                           session_index, seen_global,
                                           auto_edges=not args.no_edges)
            if it.get("links") and not args.no_edges:
                pooled_links.append((path.stem, it["links"]))
    if not args.no_edges:
        for ev, lks in pooled_links:
            # 临时挂 evalset 名以便报错定位
            built = apply_links(memory, lks, session_index)
            _ = ev, built
        try:
            memory.flush_index()
        except Exception as e:
            print(f"[run_eval][警告] flush_index 失败（不阻断）: {e}", file=sys.stderr)
    else:
        print("[run_eval][消融] --no-edges：已跳过 flush_index 与显式 links，store 不自动建边")
    stats = memory.stats()
    shared_atoms = stats.get("total_atoms", 0)
    shared_edges = stats.get("total_edges", 0)
    print(f"[run_eval] 共享实例注入完成：pooled_stores={pooled_stores} "
          f"atoms={shared_atoms} edges={shared_edges} ({time.perf_counter() - t0:.2f}s)")

    # ---------- 召回 + 判分阶段 ----------
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    uniq = uuid.uuid4().hex[:6]  # 防同秒批量运行互相覆盖
    overall_rc = 0
    for path, items in loaded:
        rows = []
        iso_stores = iso_inits = 0
        t1 = time.perf_counter()
        for it in items:
            cb = bool(it.get("causal_bridge")) and args.mode == "precision"
            if it.get("causal_bridge") and args.mode != "precision":
                print(f"[run_eval][提示] {it['id']}: causal_bridge 仅 precision 模式支持，已忽略", file=sys.stderr)
            if it.get("isolate"):
                iso_inits += 1
                m2 = VibeMemory(agent_id=f"{args.agent_id}-iso", db_path=":memory:",
                                embedding_backend="tfidf")
                idx2: dict[str, dict] = {}
                iso_stores += seed_sessions(m2, it.get("sessions") or [],
                                            ["eval", it["type"], path.stem], idx2, seen_global,
                                            auto_edges=not args.no_edges)
                if it.get("links") and not args.no_edges:
                    apply_links(m2, it["links"], idx2)
                if not args.no_edges:
                    try:
                        m2.flush_index()
                    except Exception as e:
                        print(f"[run_eval][警告] {it['id']}: flush_index 失败: {e}", file=sys.stderr)
                res = m2.recall(it["query"], mode=args.mode, top_k=args.k, causal_bridge=cb)
                m2.close()
            else:
                res = memory.recall(it["query"], mode=args.mode, top_k=args.k, causal_bridge=cb)
            ranked_sessions = [a.session_id for a in res.get("atoms", [])]
            ranked_contents = [a.content for a in res.get("atoms", [])]
            rows.append(judge_item(it, ranked_sessions, ranked_contents, args.k))
        metrics = aggregate(rows)
        ptype = per_type(rows)
        duration_ms = int((time.perf_counter() - t1) * 1000)

        evalset_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        gate = run_gate(path.stem, metrics)
        run_id = f"run-{ts}-{uniq}-{path.stem}"
        protocol = ("mixed-isolated" if iso_inits else "pooled")
        doc = {
            "run_id": run_id, "created_at": now_iso(),
            "evalset_name": path.stem, "evalset_path": str(path), "evalset_sha256": evalset_sha,
            "k": args.k, "mode": args.mode, "items_total": len(items),
            "shared_memory_agent": args.agent_id,
            "metrics": metrics, "per_type": ptype,
            "items": rows,
            "duration_ms": duration_ms, "note": note,
            "environment": {
                "vibe_memory_version": vm.__version__,
                "vibe_memory_import_path": vm_import_path,
                "vibe_memory_import_sha256": hashlib.sha256(
                    (Path(vm_import_path) / "__init__.py").read_bytes()).hexdigest()[:16],
                "revision": revision,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "embedding_backend": "tfidf",
                "db_path": ":memory:",
                "edges_disabled": bool(args.no_edges),
                "protocol": protocol,
                "shared_atoms_total": shared_atoms,
                "shared_edges_total": shared_edges,
                "isolated_items": iso_inits,
                "isolated_stores": iso_stores,
                "pooled_stores": pooled_stores,
            },
            "gate": gate,
            "limits": "内建评测集为回归级规则判分（会话ID+关键词+陷阱词）；"
                      "isolated 用例为逐题独立记忆库协议，对齐公开基准评测方式；"
                      "拒答指标为规则代理判分",
        }
        out = RUNS_DIR / f"{run_id}.json"
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        gate_s = gate.get("status", "?")
        proto_s = f" protocol={protocol}" if protocol != "pooled" else ""
        print(f"[run_eval] {path.stem:<28} Recall@{args.k}={metrics['recall_at_k']} "
              f"MRR={metrics['mrr']} nDCG={metrics['ndcg_at_k']} "
              f"KW={metrics['keyword_hit']} ABST={metrics['abstention_precision']} "
              f"门禁={gate_s}{proto_s} -> {out.name}")
        if gate.get("status") == "FAIL":
            overall_rc = 1

    memory.close()
    print(f"[run_eval] 全部完成，运行存档目录: {RUNS_DIR}")
    return overall_rc


if __name__ == "__main__":
    sys.exit(main())
