#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试中台 · 回归测试执行器

对 VibeMemory 仓库执行 pytest 回归测试，产出可溯源的运行存档：
    测试中台/runs/test-<时间戳>.json       # 汇总（用例数/通过/失败/跳过/耗时/通过率/覆盖率/门禁）
    测试中台/runs/test-<时间Stamp>.junit.xml  # 原始 junitxml（证据文件）
    测试中台/runs/cov-<时间戳>.xml         # coverage xml（证据文件）

要点：
- 在仓库 .venv 里执行（--repo 指定仓库根），basetemp 指向系统临时目录，不污染仓库；
- --scope quick 仅跑 SDK/检索核心冒烟子集（秒级），--scope full 跑全量 112 文件；
- 门禁（2026-11-14 重构）：**绝对门禁优先**——任何失败/错误用例即 FAIL（要求 0 失败）；
  0 失败时再与上一轮对比，通过率下降超过 --tolerance（默认 2pp）也 FAIL；
  退出码：FAIL=1、无结果=2、PASS/首轮=0；
- 存档含 revision 字段（commit/分支/未提交改动），可回答"测的是哪版代码"；
- 覆盖率可选（--no-cov 跳过，跳过时 quicker）。

用法：
    python run_tests.py --scope full            # 全量回归（数分钟）
    python run_tests.py --scope quick           # 核心冒烟
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"
DEFAULT_REPO = Path("C:/Users/ASYS/Desktop/VibeMemory")
DEFAULT_PYTHON = DEFAULT_REPO / ".venv" / "Scripts" / "python.exe"

QUICK_TESTS = [
    "tests/test_m1.py",
    "tests/test_tfidf_provider_contract.py",
    "tests/test_rrf_fusion.py",
    "tests/test_retrieval_projection.py",
    "tests/test_strict_scope.py",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def parse_junit(junit_path: Path) -> dict:
    root = ET.parse(str(junit_path)).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    totals = {
        "total": int(suite.get("tests") or 0),
        "failures": int(suite.get("failures") or 0),
        "errors": int(suite.get("errors") or 0),
        "skipped": int(suite.get("skipped") or 0),
        "duration_s": round(float(suite.get("time") or 0), 1),
    }
    totals["passed"] = totals["total"] - totals["failures"] - totals["errors"] - totals["skipped"]
    totals["pass_rate"] = round(totals["passed"] / max(1, totals["total"]), 4)
    fails = []
    for tc in suite.iter("testcase"):
        # 注意：ElementTree 的空元素（只有文本、无子节点）布尔值为 False，
        # 不能用 `find("failure") or find("error")` —— 会漏掉真实存在的失败节点。
        f_el, e_el = tc.find("failure"), tc.find("error")
        failure = f_el if f_el is not None else e_el
        if failure is None:
            continue
        msg = (failure.get("message") or "").splitlines()
        fails.append({
            "test": f"{tc.get('classname','')}.{tc.get('name','')}",
            "time_s": round(float(tc.get("time") or 0), 2),
            "message": (msg[0][:200] if msg else (failure.text or "")[:200]),
        })
    totals["failures_detail"] = fails[:50]
    totals["failures_count_real"] = len(fails)
    return totals


def parse_coverage_xml(cov_path: Path) -> dict:
    root = ET.parse(str(cov_path)).getroot()
    return {
        "line_rate": float(root.get("line-rate") or 0),
        "branch_rate": float(root.get("branch-rate") or 0),
        "lines_valid": int(root.get("lines-valid") or 0),
        "lines_covered": int(root.get("lines-covered") or 0),
    }


def prev_pass_rate() -> float | None:
    runs = sorted(RUNS_DIR.glob("test-*.json"))
    for p in reversed(runs):
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
            v = (d.get("summary") or {}).get("pass_rate")
            if v is not None:
                return v
        except Exception:
            continue
    return None


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
    status_out = _run("status", "--porcelain")  # 含 untracked：新文件也会被 pytest 收集
    dirty_lines = [ln for ln in status_out.splitlines() if ln.strip()]
    return {"git_available": True,
            "commit": commit[:12], "branch": _run("rev-parse", "--abbrev-ref", "HEAD") or None,
            "dirty_count": len(dirty_lines),
            "dirty_files": [ln[3:].strip() for ln in dirty_lines[:20]],
            "note": "未提交改动会改变被测代码；引用本存档指标时须结合 dirty 记录"}


def judge_gate(summary: dict, prev: float | None, tolerance: float) -> tuple[dict, int]:
    """测试门禁：绝对门禁（任何失败/错误 → FAIL）优先于相对容忍带。
    返回 (gate, exit_code)：FAIL→1，NO_RESULT→2，PASS/NO_BASELINE→0。"""
    if summary.get("pass_rate") is None:
        return {"status": "NO_RESULT", "detail": "junitxml 缺失或未解析，无法判定"}, 2
    fails_abs = (summary.get("failures") or 0) + (summary.get("errors") or 0)
    if fails_abs > 0:
        return {"status": "FAIL",
                "detail": (f"绝对门禁：{fails_abs} 个失败/错误用例"
                           f"（failures={summary.get('failures')}, errors={summary.get('errors')}），"
                           "回归门禁要求 0 失败"),
                "failures_detail": (summary.get("failures_detail") or [])[:8]}, 1
    if prev is None:
        return {"status": "NO_BASELINE", "detail": "首轮运行，无对比基线"}, 0
    drop = prev - summary["pass_rate"]
    gate = {"status": "FAIL" if drop > tolerance + 1e-9 else "PASS",
            "previous_pass_rate": prev, "current": summary["pass_rate"],
            "tolerance": tolerance}
    if gate["status"] == "FAIL":
        gate["detail"] = f"通过率下降 {drop:.2%}，超过容忍带 {tolerance:.0%}"
    return gate, (1 if gate["status"] == "FAIL" else 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="测试中台执行器")
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--python", default=str(DEFAULT_PYTHON))
    ap.add_argument("--scope", choices=["full", "quick"], default="full")
    ap.add_argument("--no-cov", action="store_true", help="跳过覆盖率（更快）")
    ap.add_argument("--tolerance", type=float, default=0.02, help="通过率容忍带（默认0.02）")
    ap.add_argument("--timeout", type=int, default=1800, help="pytest 超时秒数")
    ap.add_argument("--note", default="", help="运行备注")
    args = ap.parse_args()

    repo = Path(args.repo)
    py = Path(args.python)
    if not py.exists():
        print(f"[test] python 不存在: {py}", file=sys.stderr)
        return 3
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    junit_out = (RUNS_DIR / f"test-{ts}.junit.xml").resolve()
    cov_out = (RUNS_DIR / f"cov-{ts}.xml").resolve()
    basetemp = Path(tempfile.mkdtemp(prefix="vm-testplat-"))

    cmd = [str(py), "-m", "pytest", "-q", f"--basetemp={basetemp}", f"--junitxml={junit_out}"]
    if args.scope == "quick":
        cmd += QUICK_TESTS
    else:
        cmd.append("tests")
    if not args.no_cov:
        cmd += ["--cov=vibe_memory", f"--cov-report=xml:{cov_out}"]

    print(f"[test] scope={args.scope} cov={not args.no_cov}")
    print(f"[test] $ {' '.join(cmd[:6])} ... (cwd={repo})")
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=args.timeout)
        rc, tail = proc.returncode, (proc.stdout or "")[-2000:]
    except subprocess.TimeoutExpired:
        rc, tail = -1, f"超时（>{args.timeout}s）"
    wall_s = round(time.perf_counter() - t0, 1)

    summary = {"total": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0,
               "pass_rate": None, "duration_s": None, "failures_detail": [], "pytest_rc": rc}
    if junit_out.exists():
        try:
            summary = parse_junit(junit_out)
        except Exception as e:
            summary["parse_error"] = str(e)
        summary["pytest_rc"] = rc
    else:
        summary["pytest_tail"] = tail[-800:]
    coverage = parse_coverage_xml(cov_out) if cov_out.exists() else None

    # 同步一份到仓库根：数据中台 coverage_xml 源固定读 {repo}/coverage.xml
    if cov_out.exists():
        try:
            shutil.copyfile(cov_out, Path(repo) / "coverage.xml")
        except Exception as e:
            print(f"[test] 覆盖率同步到仓库根失败: {e}", file=sys.stderr)

    # 测试门禁：绝对门禁（0 失败）优先，相对容忍带兜底；退出码随门禁状态
    prev = prev_pass_rate()
    gate, exit_code = judge_gate(summary, prev, args.tolerance)

    doc = {
        "run_id": f"test-{ts}", "created_at": now_iso(),
        "scope": args.scope, "repo": str(repo),
        "revision": git_info(repo),
        "summary": summary, "coverage": coverage,
        "duration_wall_s": wall_s, "gate": gate,
        "artifacts": {"junit_xml": junit_out.name, "coverage_xml": cov_out.name if coverage else None,
                      "junit_sha256": sha256_file(junit_out) if junit_out.exists() else None},
        "note": args.note or "pytest 在仓库 .venv 内执行；basetemp 使用系统临时目录；被测代码版本见 revision 字段",
    }
    out = RUNS_DIR / f"test-{ts}.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    s = summary
    cov_s = f" 覆盖率 行{coverage['line_rate']:.1%}/分支{coverage['branch_rate']:.1%}" if coverage else ""
    rev = doc["revision"]
    rev_s = f" 代码={rev.get('commit', '?')}{'+' + str(rev.get('dirty_count')) + '处未提交' if rev.get('dirty_count') else ''}" if rev.get("git_available") else ""
    print(f"[test] 用例={s.get('total')} 通过={s.get('passed')} 失败={s.get('failures')} "
          f"错误={s.get('errors')} 跳过={s.get('skipped')} 通过率={s.get('pass_rate')}"
          f"{cov_s} 墙钟={wall_s}s 门禁={gate['status']}{rev_s}")
    for f_ in (s.get("failures_detail") or [])[:8]:
        print(f"[test][FAIL] {f_['test']}: {f_['message'][:120]}")
    print(f"[test] 存档 -> {out.name}")
    shutil.rmtree(basetemp, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
