#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测中台 · 回归门禁

两种门禁：
- 绝对门禁：核心指标 >= 基线阈值（threshold = 基线值 - 容忍带）
- 相对门禁：与基线之后最新的运行对比不回退超过容忍带（本实现以绝对阈值固化体现）

用法：
    python gate.py --init [--tolerance 0.05]   # 从最新运行固化基线（首轮）
    python gate.py                              # 对比门禁，FAIL 时退出码 1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"
BASELINE_PATH = HERE / "baseline.json"
REPORT_PATH = HERE / "gate_report.json"

GATED_METRICS = ["recall_at_k", "mrr", "ndcg_at_k", "keyword_hit", "abstention_precision"]
# 拒答指标波动大，容忍带放宽
METRIC_TOLERANCE = {"abstention_precision": 0.10}


def load_runs() -> list[dict]:
    runs = []
    for p in sorted(RUNS_DIR.glob("run-*.json")):
        try:
            runs.append(json.loads(p.read_text(encoding="utf-8-sig")))
        except Exception:
            continue
    runs.sort(key=lambda r: r.get("created_at") or "")
    return runs


def latest_by_evalset(runs: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for r in runs:
        latest[r.get("evalset_name") or "?"] = r  # 已按时间排序，后者覆盖
    return latest


def main() -> int:
    ap = argparse.ArgumentParser(description="评测回归门禁")
    ap.add_argument("--init", action="store_true", help="从当前最新运行固化基线")
    ap.add_argument("--tolerance", type=float, default=0.05, help="指标容忍带（默认0.05）")
    args = ap.parse_args()

    runs = load_runs()
    if not runs:
        print("[gate] 没有运行存档，请先运行 run_eval.py", file=sys.stderr)
        return 2
    latest = latest_by_evalset(runs)

    if args.init:
        evalsets = {}
        for name, r in sorted(latest.items()):
            thresholds = {}
            for m in GATED_METRICS:
                v = (r.get("metrics") or {}).get(m)
                if v is None:
                    continue
                tol = METRIC_TOLERANCE.get(m, args.tolerance)
                # 阈值不低于 0.5*基线值，避免低基线时阈值失去意义
                thresholds[m] = round(max(v - tol, v * 0.5), 4)
            evalsets[name] = {
                "run_id": r.get("run_id"), "created_at": r.get("created_at"),
                "environment": {"protocol": (r.get("environment") or {}).get("protocol"),
                                 "shared_atoms_total": (r.get("environment") or {}).get("shared_atoms_total")},
                "baseline_values": {m: (r.get("metrics") or {}).get(m) for m in GATED_METRICS
                                     if (r.get("metrics") or {}).get(m) is not None},
                "thresholds": thresholds,
            }
        BASELINE_PATH.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "tolerance": args.tolerance, "evalsets": evalsets,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[gate] 基线已固化（{len(evalsets)} 个评测集）-> {BASELINE_PATH.name}")
        for name, b in evalsets.items():
            print(f"[gate]   {name:<16} 基线Recall@K={b['baseline_values'].get('recall_at_k')}")
        return 0

    if not BASELINE_PATH.exists():
        print("[gate] 尚无基线，请先 python gate.py --init", file=sys.stderr)
        return 2
    base = json.loads(BASELINE_PATH.read_text(encoding="utf-8-sig"))

    report = {"checked_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
              "baseline_generated_at": base.get("generated_at"), "evalsets": {}, "status": "PASS"}
    for name, r in sorted(latest.items()):
        b = base.get("evalsets", {}).get(name)
        if not b:
            report["evalsets"][name] = {"status": "SKIP", "detail": "基线中无此评测集"}
            continue
        checks = []
        fail = False
        for m, thr in (b.get("thresholds") or {}).items():
            got = (r.get("metrics") or {}).get(m)
            if got is None:
                continue
            ok = got >= thr - 1e-9
            checks.append({"metric": m, "value": got, "threshold": thr, "pass": ok})
            if not ok:
                fail = True
        st = "FAIL" if fail else "PASS"
        # 环境可比性提示：记忆库规模或协议与基线差异过大时，指标不可直接对比
        warn = None
        b_env = b.get("environment") or {}
        r_env = r.get("environment") or {}
        if b_env.get("protocol") and r_env.get("protocol") and b_env["protocol"] != r_env["protocol"]:
            warn = f"协议不同：基线 {b_env['protocol']} vs 当前 {r_env['protocol']}"
        elif (b_env.get("shared_atoms_total") and r_env.get("shared_atoms_total")
              and max(b_env["shared_atoms_total"], r_env["shared_atoms_total"])
              / max(1, min(b_env["shared_atoms_total"], r_env["shared_atoms_total"])) > 3):
            warn = (f"记忆库规模差异大：基线 {b_env['shared_atoms_total']} atoms vs "
                    f"当前 {r_env['shared_atoms_total']} atoms，指标不可直接对比")
        report["evalsets"][name] = {"run_id": r.get("run_id"), "status": st, "checks": checks,
                                     "env_warning": warn}
        if fail:
            report["status"] = "FAIL"
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    for name, res in report["evalsets"].items():
        if res["status"] == "SKIP":
            print(f"[gate] {name:<16} SKIP（基线中无此评测集）")
            continue
        bad = [c for c in res["checks"] if not c["pass"]]
        mark = "✗" if bad else "✓"
        print(f"[gate] {name:<16} {res['status']} {mark}")
        if res.get("env_warning"):
            print(f"[gate]    ⚠ {res['env_warning']}")
        for c in res["checks"]:
            print(f"[gate]    {c['metric']:<22} {c['value']:.4f} (阈值 {c['threshold']:.4f}) "
                  f"{'OK' if c['pass'] else 'FAIL'}")
    print(f"[gate] 总体门禁: {report['status']} -> {REPORT_PATH.name}")
    return 1 if report["status"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
