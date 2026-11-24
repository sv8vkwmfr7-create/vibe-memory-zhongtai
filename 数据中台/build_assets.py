#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据中台 · 资产加工（数据开发层）

ODS 原始层 -> DWD 明细层（清洗/规范化）-> DWS 汇总层（指标主题），
并生成数据资产目录 catalog.json（含血缘 lineage）。

用法：
    python build_assets.py [--root 中台根目录]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_FILES = ["collect.py", "build_assets.py", "quality.py"]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def platform_fingerprint(plat: Path) -> str:
    """中台加工管线的代码指纹（版本三元组之 pipeline_commit）。"""
    h = hashlib.sha256()
    for n in PIPELINE_FILES:
        try:
            h.update((plat / n).read_bytes())
        except Exception:
            pass
    try:
        h.update((plat.parent / "scripts" / "build_pages.py").read_bytes())
    except Exception:
        pass
    return h.hexdigest()[:12]


def git_head_of(repo_root: str | None) -> str | None:
    if not repo_root:
        return None
    try:
        return subprocess.run(["git", "-C", repo_root, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        return None


def emit_lineage_event(plat: Path, job: str, inputs: list, outputs: list) -> None:
    """OpenLineage 风格血缘事件落 JSONL（验收 4：血缘可查的落地形态）。"""
    ev = {"eventType": "COMPLETE", "eventTime": now_iso(),
          "producer": f"数据中台/{job}",
          "job": {"namespace": "vibememory-zhongtai", "name": job},
          "run": {"runId": str(uuid.uuid4())},
          "inputs": inputs, "outputs": outputs}
    p = plat / "assets" / "lineage_events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8-sig"))


def save_json(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- DWD

def dwd_experiments(ods: dict) -> dict:
    """results/*.json -> 实验明细记录（每文件一条，含通用标量摘要与指标点）"""
    files = ods.get("ods", {}).get("files", [])
    recs = []
    for f in files:
        scalars = f.get("scalars") or f.get("scalars_keep") or {}
        # 提炼指标点：数值型标量 -> metric_point
        points = []
        for k, v in scalars.items():
            if isinstance(v, bool) or k.endswith(".__len__"):
                continue
            if isinstance(v, (int, float)):
                # keypath 末段作为指标名，中段作为维度
                seg = k.split(".")
                points.append({"metric": seg[-1], "path": k, "value": v,
                               "unit": "bytes" if "bytes" in k.lower() else
                                       ("ms" if k.endswith("_ms") else "")})
        recs.append({
            "file": f.get("file"), "sha256": f.get("sha256"), "bytes": f.get("bytes"),
            "mtime": f.get("mtime"), "parse_error": f.get("parse_error"),
            "dataset": scalars.get("dataset") or scalars.get("experiment") or scalars.get("source"),
            "mode": scalars.get("mode"),
            "limits": scalars.get("limits"),
            "metric_points": points[:120],
            **({"paired_evaluation": f["paired_evaluation"]} if "paired_evaluation" in f else {}),
        })
    ok = sum(1 for r in recs if not r["parse_error"])
    return {"asset": "dwd_experiments", "generated_at": now_iso(),
            "description": "实验结果明细（results/*.json 清洗规范化）",
            "record_count": len(recs), "parse_ok": ok, "parse_error": len(recs) - ok,
            "records": recs}


def dwd_coverage(ods: dict) -> dict:
    ov = (ods.get("ods", {}) or {}).get("overall", {})
    pkgs = (ods.get("ods", {}) or {}).get("packages", [])
    return {"asset": "dwd_coverage", "generated_at": now_iso(),
            "description": "测试覆盖率明细（coverage.xml 解析）",
            "source_sha256": ods.get("sha256"), "collected_at": ods.get("collected_at"),
            "overall": ov,
            "packages": [{"name": p["name"], "line_rate": p["line_rate"], "branch_rate": p["branch_rate"]}
                          for p in pkgs],
            "weakest_classes": sorted(
                [c for p in pkgs for c in p.get("classes", [])], key=lambda x: x["line_rate"])[:10]}


def dwd_docs(ods_status: dict | None, ods_changelog: dict | None) -> dict:
    out = {"asset": "dwd_docs", "generated_at": now_iso(),
           "description": "项目状态与版本演进明细"}
    if ods_status and ods_status.get("status") == "ok":
        o = ods_status["ods"]
        sections = [h["text"] for h in o.get("headings", []) if h["level"] <= 3]
        out["status"] = {"sha256": ods_status.get("sha256"),
                         "sections": sections[:80], "section_count": o.get("heading_count"),
                         "date_min": o.get("date_min"), "date_max": o.get("date_max"),
                         "char_count": o.get("char_count")}
    if ods_changelog and ods_changelog.get("status") == "ok":
        o = ods_changelog["ods"]
        out["changelog"] = {"sha256": ods_changelog.get("sha256"),
                            "sections": [h["text"] for h in o.get("headings", []) if h["level"] <= 2][:40]}
    return out


def dwd_repo(ods_meta: dict | None, ods_tests: dict | None, ods_git: dict | None, ods_seed: dict | None) -> dict:
    out = {"asset": "dwd_repo", "generated_at": now_iso(), "description": "仓库元数据/测试索引/提交活动明细"}
    if ods_meta and ods_meta.get("status") == "ok":
        out["meta"] = ods_meta["ods"].get("scalars", {})
        out["meta_source_sha256"] = ods_meta.get("sha256")
    if ods_tests and ods_tests.get("status") == "ok":
        o = ods_tests["ods"]
        out["tests"] = {"file_count": o.get("count"), "test_funcs_total": o.get("test_funcs_total"),
                        "files": o.get("files", [])[:200]}
    if ods_git and ods_git.get("status") == "ok":
        o = ods_git["ods"]
        commits = o.get("commits", [])
        out["git"] = {"recent_count": o.get("count"),
                      "last_commit_at": commits[0]["date"] if commits else None,
                      "first_commit_at": commits[-1]["date"] if commits else None,
                      "commits": commits}
    if ods_seed and ods_seed.get("status") == "ok":
        data = ods_seed["ods"].get("data") or {}
        out["seed_memory"] = {"sha256": ods_seed.get("sha256"),
                              "atom_count": len(data.get("atoms", [])),
                              "version": data.get("version"), "domain": data.get("domain")}
    return out


def dwd_test_cases(runs_dir: Path) -> dict:
    """junitxml 逐用例资产化：每轮回归的每个测试用例一条 DWD 记录（含状态与耗时）。"""
    recs, files_n = [], 0
    if not runs_dir.exists():
        return {"asset": "dwd_test_cases", "generated_at": now_iso(),
                "description": "pytest 逐用例明细（junitxml 炸开）",
                "source_files": 0, "record_count": 0, "records": []}
    for p in sorted(runs_dir.glob("test-*.junit.xml")):
        files_n += 1
        try:
            root = ET.parse(str(p)).getroot()
        except Exception:
            continue
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        run_ts = p.stem.replace("test-", "")
        if suite is None:
            continue
        for tc in suite.iter("testcase"):
            status = "passed"
            if tc.find("failure") is not None:
                status = "failure"
            elif tc.find("error") is not None:
                status = "error"
            elif tc.find("skipped") is not None:
                status = "skipped"
            recs.append({"run_ts": run_ts, "junit_file": p.name,
                         "classname": tc.get("classname"), "case": tc.get("name"),
                         "time_s": round(float(tc.get("time") or 0), 4), "status": status})
    return {"asset": "dwd_test_cases", "generated_at": now_iso(),
            "description": "pytest 逐用例明细（junitxml 炸开，含状态与耗时）",
            "source_files": files_n, "record_count": len(recs), "records": recs}


def dwd_eval_items(runs_dir: Path) -> dict:
    """逐题明细资产化（万级路径）：把每份评测运行存档的 items[] 炸开成逐用例 DWD 记录。

    直接读 评测中台/runs/run-*.json 原文（ODS 对 >200KB 的大存档只留标量摘要，
    items 在 ODS 层丢失——本资产补回逐题粒度），每条记录带 run 溯源指纹。
    """
    recs, files_n, parse_err = [], 0, 0
    if not runs_dir.exists():
        return {"asset": "dwd_eval_items", "generated_at": now_iso(),
                "description": "评测逐题明细（全部运行存档 items 炸开）",
                "source_files": 0, "parse_error": 0, "record_count": 0, "records": []}
    for p in sorted(runs_dir.glob("run-*.json")):
        files_n += 1
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            parse_err += 1
            continue
        run_id = data.get("run_id") or p.stem
        ctx = {"run_id": run_id, "evalset": data.get("evalset_name"),
               "created_at": data.get("created_at"), "k": data.get("k"),
               "mode": data.get("mode"), "run_sha256": sha256_file(p)}
        for it in data.get("items") or []:
            recs.append({**ctx, "case_id": it.get("id"), "type": it.get("type"),
                         "query": (it.get("query") or "")[:120],
                         "hit_at_k": it.get("hit_at_k"), "abstain_ok": it.get("abstain_ok"),
                         "session_recall": it.get("session_recall"), "ndcg": it.get("ndcg"),
                         "reciprocal_rank": it.get("reciprocal_rank"),
                         "first_hit_rank": it.get("first_hit_rank"),
                         "keywords_ok": it.get("keywords_ok")})
    return {"asset": "dwd_eval_items", "generated_at": now_iso(),
            "description": "评测逐题明细（全部运行存档 items 炸开，含 run 溯源指纹）",
            "source_files": files_n, "parse_error": parse_err,
            "record_count": len(recs), "records": recs}


def dwd_eval_runs(ods_runs: dict | None) -> dict:
    if not ods_runs or ods_runs.get("status") != "ok":
        return {"asset": "dwd_eval_runs", "generated_at": now_iso(),
                "description": "评测中台运行存档明细（回流）", "record_count": 0, "records": [],
                "note": "评测中台尚无运行存档，先运行 评测中台/run_eval.py"}
    files = ods_runs.get("ods", {}).get("files", [])
    recs = []
    for f in files:
        data = f.get("data")
        if not isinstance(data, dict):
            continue
        recs.append({
            "run_id": data.get("run_id"), "created_at": data.get("created_at"),
            "evalset": data.get("evalset_name"), "evalset_sha256": data.get("evalset_sha256"),
            "k": data.get("k"), "mode": data.get("mode"),
            "items_total": data.get("items_total"),
            "metrics": data.get("metrics"), "per_type": data.get("per_type"),
            "duration_ms": data.get("duration_ms"),
            "environment": data.get("environment"),
            "gate": data.get("gate"),
        })
    recs.sort(key=lambda r: r.get("created_at") or "")
    return {"asset": "dwd_eval_runs", "generated_at": now_iso(),
            "description": "评测中台运行存档明细（回流）",
            "record_count": len(recs), "records": recs}


def dwd_evalsets(ods_evalsets: dict | None, ods_benchmarks: dict | None) -> dict:
    def files(ods):
        return (ods or {}).get("ods", {}).get("files", []) if ods and ods.get("status") == "ok" else []
    recs = []
    for f in files(ods_evalsets):
        recs.append({"file": f.get("file"), "cases": f.get("records"),
                     "type_counts": f.get("type_counts"), "isolate_items": f.get("isolate_items"),
                     "sha256": f.get("sha256"), "bytes": f.get("bytes"), "mtime": f.get("mtime"),
                     "origin": "内建/生成"})
    for f in files(ods_benchmarks):
        recs.append({"file": f.get("file"), "cases": f.get("records"),
                     "type_counts": f.get("type_counts"), "isolate_items": f.get("isolate_items"),
                     "sha256": f.get("sha256"), "bytes": f.get("bytes"), "mtime": f.get("mtime"),
                     "origin": "公开基准转换"})
    total_cases = sum(r.get("cases") or 0 for r in recs)
    return {"asset": "dwd_evalsets", "generated_at": now_iso(),
            "description": "评测集资产明细（内建+生成+公开基准转换）",
            "file_count": len(recs), "total_cases": total_cases, "records": recs}


def dwd_datasets(ods_datasets: dict | None, platform_root: Path) -> dict:
    files = (ods_datasets or {}).get("ods", {}).get("files", [])
    recs, cards = [], []
    for f in files:
        rel = f.get("file", "")
        recs.append({"file": rel, "sha256": f.get("sha256"), "bytes": f.get("bytes"),
                     "mtime": f.get("mtime")})
    # 数据集指纹卡（平台自有文件，直接读取）
    ds_dir = platform_root / "评测中台" / "datasets"
    for p in sorted(ds_dir.glob("*/card.json")) if ds_dir.exists() else []:
        try:
            cards.append(json.loads(p.read_text(encoding="utf-8-sig")))
        except Exception:
            continue
    total_bytes = sum(r.get("bytes") or 0 for r in recs)
    return {"asset": "dwd_datasets", "generated_at": now_iso(),
            "description": "公开基准原始数据集明细（含指纹卡）",
            "file_count": len(recs), "total_bytes": total_bytes,
            "total_bytes_human": f"{total_bytes / 1048576:.1f} MB",
            "records": recs, "cards": cards}


def dwd_test_runs(ods_tests: dict | None) -> dict:
    files = (ods_tests or {}).get("ods", {}).get("files", [])
    recs = []
    for f in files:
        data = f.get("data")
        if not isinstance(data, dict):
            continue
        s = data.get("summary") or {}
        recs.append({
            "run_id": data.get("run_id"), "created_at": data.get("created_at"),
            "scope": data.get("scope"),
            "total": s.get("total"), "passed": s.get("passed"),
            "failures": s.get("failures"), "errors": s.get("errors"), "skipped": s.get("skipped"),
            "pass_rate": s.get("pass_rate"), "duration_s": s.get("duration_s"),
            "coverage": data.get("coverage"), "gate": data.get("gate"),
            "failures_detail": (s.get("failures_detail") or [])[:20],
        })
    recs.sort(key=lambda r: r.get("created_at") or "")
    latest = recs[-1] if recs else None
    return {"asset": "dwd_test_runs", "generated_at": now_iso(),
            "description": "pytest 回归运行存档明细（测试中台回流）",
            "record_count": len(recs), "latest": latest, "records": recs}


def dwd_obs_runs(ods_obs: dict | None) -> dict:
    files = (ods_obs or {}).get("ods", {}).get("files", [])
    recs = []
    for f in files:
        data = f.get("data")
        if not isinstance(data, dict):
            continue
        recs.append({
            "run_id": data.get("run_id"), "created_at": data.get("created_at"),
            "workload": data.get("workload"),
            "store": data.get("store"), "recall": data.get("recall"),
            "graph": data.get("graph"),
            "degradation": (data.get("sdk_metrics") or {}).get("degradation"),
        })
    recs.sort(key=lambda r: r.get("created_at") or "")
    return {"asset": "dwd_obs_runs", "generated_at": now_iso(),
            "description": "SDK 运行时指标明细（监控中台回流）",
            "record_count": len(recs), "latest": recs[-1] if recs else None, "records": recs}


# ---------------------------------------------------------------- DWS

def dws_repo_health(dwd_repo_data: dict, dwd_cov: dict) -> dict:
    meta = dwd_repo_data.get("meta", {})
    tests = dwd_repo_data.get("tests", {})
    git = dwd_repo_data.get("git", {})
    return {
        "asset": "dws_repo_health", "generated_at": now_iso(),
        "description": "仓库健康主题（汇总指标）",
        "version": meta.get("project.version"),
        "python_required": meta.get("project.requires-python"),
        "dependencies": meta.get("project.dependencies.__len__"),
        "entry_points": meta.get("entry_points.__len__"),
        "test_files": tests.get("file_count"),
        "test_funcs": tests.get("test_funcs_total"),
        "coverage_line_rate": dwd_cov.get("overall", {}).get("line_rate"),
        "coverage_branch_rate": dwd_cov.get("overall", {}).get("branch_rate"),
        "coverage_lines": dwd_cov.get("overall", {}).get("lines_covered"),
        "coverage_lines_valid": dwd_cov.get("overall", {}).get("lines_valid"),
        "recent_commits": git.get("recent_count"),
        "last_commit_at": git.get("last_commit_at"),
    }


def dws_experiment_metrics(dwd_exp: dict) -> dict:
    """从实验明细中提取核心可读指标（keypath 定位，缺失即跳过——诚实呈现）"""
    want = {
        "mcp_maintenance_smoke.json": {
            "维护轮数": ("rounds", None), "锚点命中率": ("anchor_hits.__len__", None),
            "维护状态截断数": ("maintenance_statuses.truncated", None),
            "检查点回程p50(ms)": ("checkpoint_round_trip_ms.p50", "ms"),
            "检查点回程p95(ms)": ("checkpoint_round_trip_ms.p95", "ms"),
        },
        "disk_soak_sdk_maintenance_30min.json": {
            "soak时长(s)": ("elapsed_seconds", "s"),
            "WAL峰值(MB)": ("wal_sampled_peak_bytes", "bytes->MB"),
            "读线程数": ("readers.__len__", None),
            "读延迟p95均值(ms)": ("readers.[]~mean.sdk_ms.p95", "ms"),
        },
        "disk_soak.json": {
            "WAL峰值-被动基线(MB)": ("wal_sampled_peak_bytes", "bytes->MB"),
            "完整性检查": ("integrity_check", None),
        },
        "longmemeval_minilm_smoke.json": {
            "LongMemEval冒烟-计分题数": ("scored", None),
            "LongMemEval冒烟-Top5证据命中": ("evidence_top5_hits", None),
            "LongMemEval冒烟-轮级证据召回均值": ("mean_turn_evidence_recall", None),
            "查询p50(ms)": ("query_p50_ms", "ms"),
        },
        "locomo_answer_pilot.json": {
            "LoCoMo答案试跑-题数": ("rows.__len__", None),
            "LoCoMo答案试跑-token_f1均值(带记忆)": ("rows.[]~mean.mcp_memory.token_f1", None),
            "LoCoMo答案试跑-recall_ms均值": ("rows.[]~mean.mcp_memory.recall_ms", "ms"),
        },
    }
    metrics = []
    for rec in dwd_exp.get("records", []):
        if rec.get("parse_error") or rec.get("file") not in want:
            continue
        by_path = {p["path"]: p for p in rec.get("metric_points", [])}
        for label, (path, unit) in want[rec["file"]].items():
            for cand, p in by_path.items():
                if cand == path or cand.endswith("." + path) or cand.endswith(path):
                    val = p["value"]
                    if unit == "bytes->MB":
                        val = round(val / 1048576, 2); unit = "MB"
                    metrics.append({"experiment_file": rec["file"], "dataset": rec.get("dataset"),
                                    "metric": label, "value": val, "unit": unit or "",
                                    "source_path": p["path"], "mtime": rec.get("mtime")})
                    break
    return {"asset": "dws_experiment_metrics", "generated_at": now_iso(),
            "description": "核心实验指标主题（来源文件级溯源）",
            "metric_count": len(metrics), "metrics": metrics}


def _parse_ts(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    try:
        return datetime.strptime(s[:15], "%Y%m%d-%H%M%S")
    except Exception:
        return None


def _hit_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return None


def dws_weekly(e_items: dict, e_cases: dict, e_tests: dict, e_obs: dict, e_runs: dict,
               ods_kb_docs: dict | None = None, ods_cz_events: dict | None = None) -> dict:
    """周对比主题（验收 2：质量闭环——回答「这周比上周好吗」）。

    滚动窗口：本周 = 近 7 天，上周 = 前 7 天。窗口无数据的维度如实标 insufficient。
    projects 维度：文档型/事件型项目无评测测试管道，用「本周活跃度」做质量代理
    （知识库=本周修改的文档数，codex=事件流是否更新）。
    """
    now = datetime.now().astimezone()
    wk_this = (now.timestamp() - 7 * 86400, now.timestamp())
    wk_last = (now.timestamp() - 14 * 86400, now.timestamp() - 7 * 86400)

    def in_win(dt, win):
        if dt is None:
            return False
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return win[0] <= dt.timestamp() < win[1]

    def agg_eval(recs, win):
        n_hit = n_hit_ok = n_abs = n_abs_ok = 0
        runs = set()
        for r in recs:
            dt = _parse_ts(r.get("created_at"))
            if not in_win(dt, win):
                continue
            runs.add(r.get("run_id"))
            h = _hit_bool(r.get("hit_at_k"))
            if h is not None:
                n_hit += 1
                n_hit_ok += 1 if h else 0
            a = _hit_bool(r.get("abstain_ok"))
            if a is not None:
                n_abs += 1
                n_abs_ok += 1 if a else 0
        return {"runs": len(runs), "cases": n_hit + n_abs,
                "hit_rate": round(n_hit_ok / n_hit, 4) if n_hit else None,
                "abstain_rate": round(n_abs_ok / n_abs, 4) if n_abs else None}

    def agg_test(recs, win):
        run_ids, cases, prs = set(), 0, []
        for r in recs:
            dt = _parse_ts(r.get("run_ts"))
            if not in_win(dt, win):
                continue
            run_ids.add(r.get("run_ts"))
            cases += 1
            st = r.get("status")
            prs.append(1.0 if st == "passed" else 0.0)
        return {"rounds": len(run_ids), "cases": cases,
                "case_pass_rate": round(sum(prs) / len(prs), 4) if prs else None}

    def agg_obs(recs, win):
        rounds, hits = 0, []
        for r in recs:
            dt = _parse_ts(r.get("created_at"))
            if not in_win(dt, win):
                continue
            rounds += 1
            rc = r.get("recall") or {}
            if rc.get("hit_rate") is not None:
                hits.append(rc["hit_rate"])
        return {"rounds": rounds,
                "hit_rate": round(sum(hits) / len(hits), 4) if hits else None}

    def delta(this, last, key):
        a, b = this.get(key), last.get(key)
        if a is None or b is None:
            return None
        return round(a - b, 4)

    def verdict(this, last, key, tol=0.02):
        d = delta(this, last, key)
        if d is None:
            return "insufficient"
        if d > tol:
            return "better"
        if d < -tol:
            return "worse"
        return "same"

    it_recs = e_items.get("records", [])
    tc_recs = e_cases.get("records", [])
    obs_recs = e_obs.get("records", [])
    ev_this, ev_last = agg_eval(it_recs, wk_this), agg_eval(it_recs, wk_last)
    ts_this, ts_last = agg_test(tc_recs, wk_this), agg_test(tc_recs, wk_last)
    ob_this, ob_last = agg_obs(obs_recs, wk_this), agg_obs(obs_recs, wk_last)

    # projects 维度：文档/事件型项目的活跃度质量代理
    projects = []
    kb_files = ((ods_kb_docs or {}).get("ods") or {}).get("files", [])
    if kb_files:
        def touched(win):
            n, chars = 0, 0
            for f in kb_files:
                dt = _parse_ts(f.get("mtime"))
                if in_win(dt, win):
                    n += 1
                    chars += f.get("char_count") or 0
            return n, chars
        t_n, t_chars = touched(wk_this)
        l_n, _ = touched(wk_last)
        projects.append({"project_id": "knowledge-base", "kind": "docs",
                         "this": {"docs_total": len(kb_files), "docs_touched": t_n,
                                  "chars_touched": t_chars},
                         "last": {"docs_touched": l_n},
                         "verdict": "active" if t_n else "stale"})
    cz = (ods_cz_events or {}).get("ods") or {}
    if cz.get("_snapshot") == "jsonl-summary":
        projects.append({"project_id": "codex-zcode-team", "kind": "events",
                         "this": {"events_total": cz.get("total_lines"),
                                  "type_counts_top": dict(list((cz.get("type_counts") or {}).items())[:5])},
                         "verdict": "active" if cz.get("total_lines") else "stale"})

    v_hit = verdict(ev_this, ev_last, "hit_rate")
    v_pass = verdict(ts_this, ts_last, "case_pass_rate")
    parts, marks = [], []
    pct = lambda v: "无数据" if v is None else f"{v:.1%}"
    if ev_this["cases"]:
        parts.append(f"评测 {ev_this['runs']} 轮 {ev_this['cases']} 题，命中率 "
                     f"{pct(ev_this['hit_rate'])}（上周 {pct(ev_last['hit_rate'])}）")
        marks.append(v_hit)
    if ts_this["cases"]:
        parts.append(f"测试 {ts_this['rounds']} 轮 {ts_this['cases']} 用例，通过率 "
                     f"{pct(ts_this['case_pass_rate'])}（上周 {pct(ts_last['case_pass_rate'])}）")
        marks.append(v_pass)
    if not parts:
        answer = "近 7 天暂无评测/测试产出（定时产出未运行或刚启用）"
        overall = "insufficient"
    else:
        if "worse" in marks:
            overall = "worse"
        elif any(m == "insufficient" for m in marks):
            overall = "insufficient"   # 上周同维度无数据，不做无据比对
        elif "better" in marks:
            overall = "better"
        else:
            overall = "same"
        zh = {"better": "较上周变好", "same": "与上周持平",
              "worse": "较上周变差", "insufficient": "上周同维度无数据，待积累满一周后可比"}
        answer = "；".join(parts) + f" → {zh[overall]}"

    return {"asset": "dws_weekly", "generated_at": now_iso(),
            "description": "周对比主题（滚动 7 天 vs 前 7 天：评测命中率/测试通过率）",
            "window": {"this_week": {"start": datetime.fromtimestamp(wk_this[0]).isoformat(timespec="seconds"),
                                     "end": datetime.fromtimestamp(wk_this[1]).isoformat(timespec="seconds")},
                       "last_week": {"start": datetime.fromtimestamp(wk_last[0]).isoformat(timespec="seconds"),
                                     "end": datetime.fromtimestamp(wk_last[1]).isoformat(timespec="seconds")}},
            "eval": {"this": ev_this, "last": ev_last,
                     "delta_hit_rate": delta(ev_this, ev_last, "hit_rate"), "verdict": v_hit},
            "test": {"this": ts_this, "last": ts_last,
                     "delta_case_pass_rate": delta(ts_this, ts_last, "case_pass_rate"), "verdict": v_pass},
            "obs": {"this": ob_this, "last": ob_last},
            "projects": projects,
            "overall_verdict": overall, "answer": answer}


def dws_eval_summary(dwd_runs: dict) -> dict:
    """按评测集聚合：最新一次运行指标 + 历史序列"""
    by_set: dict[str, list] = {}
    for r in dwd_runs.get("records", []):
        by_set.setdefault(r.get("evalset") or "?", []).append(r)
    sets = []
    for name, runs in sorted(by_set.items()):
        runs.sort(key=lambda r: r.get("created_at") or "")
        latest = runs[-1]
        sets.append({
            "evalset": name, "run_count": len(runs),
            "latest_run_id": latest.get("run_id"), "latest_created_at": latest.get("created_at"),
            "k": latest.get("k"), "mode": latest.get("mode"), "items_total": latest.get("items_total"),
            "latest_metrics": latest.get("metrics"), "per_type": latest.get("per_type"),
            "gate": latest.get("gate"),
            "history": [{"run_id": r.get("run_id"), "created_at": r.get("created_at"),
                          "recall_at_k": (r.get("metrics") or {}).get("recall_at_k"),
                          "mrr": (r.get("metrics") or {}).get("mrr")}
                         for r in runs],
        })
    return {"asset": "dws_eval_summary", "generated_at": now_iso(),
            "description": "评测结果主题（按评测集聚合）",
            "evalset_count": len(sets), "evalsets": sets}


# ---------------------------------------------------------------- 目录

def build_catalog(sources_prov: dict, assets: list[dict]) -> dict:
    layers = {
        "ods": "原始接入层：来源快照 + 指纹（不改动原始数值）",
        "dwd": "明细层：清洗、规范化、统一记录模型",
        "dws": "汇总层：面向应用的指标主题",
    }
    prov_sources = []
    for s in sources_prov.get("sources", []):
        prov_sources.append({
            "id": s.get("id"), "kind": s.get("kind"), "path": s.get("path"),
            "status": s.get("status"), "desc": s.get("desc"), "origin": s.get("origin"),
            "project": s.get("project", "vibememory"),
            "sha256": s.get("sha256"), "records": s.get("records"),
            "bytes": s.get("bytes"), "mtime": s.get("mtime"),
            "collected_at": s.get("collected_at"), "changed_since_last": s.get("changed_since_last"),
        })
    return {
        "generated_at": now_iso(),
        "project": "VibeMemory 数据中台",
        "repo": sources_prov.get("repo"),
        "layer_semantics": layers,
        "sources": prov_sources,
        "assets": assets,
        "quality_report": "assets/quality_report.json",
        "provenance": "assets/provenance.json",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="数据中台资产加工")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    args = ap.parse_args()
    root = Path(args.root).resolve()
    plat = root / "数据中台"
    ods_dir = plat / "assets" / "ods"

    def ods(sid: str):
        p = ods_dir / f"{sid}.json"
        return load_json(p) if p.exists() else None

    prov = load_json(plat / "assets" / "provenance.json")

    dwd_dir = plat / "assets" / "dwd"
    dws_dir = plat / "assets" / "dws"

    e_exp = dwd_experiments(ods("results_experiments") or {})
    e_cov = dwd_coverage(ods("coverage_xml") or {})
    e_docs = dwd_docs(ods("status_md"), ods("changelog_md"))
    e_repo = dwd_repo(ods("repo_meta"), ods("tests_index"), ods("git_activity"), ods("seed_memory"))
    e_runs = dwd_eval_runs(ods("eval_runs"))
    e_items = dwd_eval_items(root / "评测中台" / "runs")
    e_cases = dwd_test_cases(root / "测试中台" / "runs")
    e_evalsets = dwd_evalsets(ods("evalsets_local"), ods("benchmarks_local"))
    e_datasets = dwd_datasets(ods("datasets_public"), root)
    e_tests = dwd_test_runs(ods("test_runs"))
    e_obs = dwd_obs_runs(ods("obs_runs"))

    save_json(dwd_dir / "experiments.json", e_exp)
    save_json(dwd_dir / "coverage.json", e_cov)
    save_json(dwd_dir / "docs.json", e_docs)
    save_json(dwd_dir / "repo.json", e_repo)
    save_json(dwd_dir / "eval_runs.json", e_runs)
    save_json(dwd_dir / "eval_items.json", e_items)
    save_json(dwd_dir / "evalsets.json", e_evalsets)
    save_json(dwd_dir / "datasets.json", e_datasets)
    save_json(dwd_dir / "test_runs.json", e_tests)
    save_json(dwd_dir / "test_cases.json", e_cases)
    save_json(dwd_dir / "obs_runs.json", e_obs)

    s_health = dws_repo_health(e_repo, e_cov)
    s_metrics = dws_experiment_metrics(e_exp)
    s_eval = dws_eval_summary(e_runs)
    s_weekly = dws_weekly(e_items, e_cases, e_tests, e_obs, e_runs,
                          ods_kb_docs=ods("kb_docs"), ods_cz_events=ods("cz_events"))
    save_json(dws_dir / "repo_health.json", s_health)
    save_json(dws_dir / "experiment_metrics.json", s_metrics)
    save_json(dws_dir / "eval_summary.json", s_eval)
    save_json(dws_dir / "weekly.json", s_weekly)

    # 资产目录（血缘：sources -> ods -> dwd -> dws）
    ods_ids = [s["id"] for s in prov.get("sources", []) if s.get("status") == "ok"]
    assets = [
        {"id": "ods_*", "layer": "ods", "path": "数据中台/assets/ods/",
         "desc": "9 类可信来源的原始快照（指纹+记录数）",
         "rows": len(ods_ids), "lineage": {"upstream": ods_ids, "transforms": ["snapshot", "checksum"]}},
        {"id": "dwd_experiments", "layer": "dwd", "path": "数据中台/assets/dwd/experiments.json",
         "desc": "实验结果明细", "rows": e_exp["record_count"],
         "fields": ["file", "sha256", "dataset", "mode", "limits", "metric_points[]"],
         "lineage": {"upstream": ["results_experiments"], "transforms": ["parse", "flatten", "normalize"]}},
        {"id": "dwd_coverage", "layer": "dwd", "path": "数据中台/assets/dwd/coverage.json",
         "desc": "覆盖率明细", "rows": len(e_cov.get("packages", [])),
         "fields": ["overall.line_rate", "overall.branch_rate", "packages[]", "weakest_classes[]"],
         "lineage": {"upstream": ["coverage_xml"], "transforms": ["xml-parse", "rollup"]}},
        {"id": "dwd_docs", "layer": "dwd", "path": "数据中台/assets/dwd/docs.json",
         "desc": "状态台账与版本演进明细", "rows": len((e_docs.get("status") or {}).get("sections", [])),
         "fields": ["status.sections[]", "changelog.sections[]"],
         "lineage": {"upstream": ["status_md", "changelog_md"], "transforms": ["heading-index", "date-scan"]}},
        {"id": "dwd_repo", "layer": "dwd", "path": "数据中台/assets/dwd/repo.json",
         "desc": "仓库元数据/测试索引/提交活动", "rows": 1,
         "fields": ["meta", "tests", "git", "seed_memory"],
         "lineage": {"upstream": ["repo_meta", "tests_index", "git_activity", "seed_memory"],
                      "transforms": ["merge", "count"]}},
        {"id": "dwd_eval_runs", "layer": "dwd", "path": "数据中台/assets/dwd/eval_runs.json",
         "desc": "评测运行存档明细（评测中台回流）", "rows": e_runs.get("record_count", 0),
         "fields": ["run_id", "evalset", "metrics", "per_type", "environment"],
         "lineage": {"upstream": ["eval_runs"], "transforms": ["parse", "project"]}},
        {"id": "dwd_eval_items", "layer": "dwd", "path": "数据中台/assets/dwd/eval_items.json",
         "desc": "评测逐题明细（运行存档 items 炸开，万级记录主体）", "rows": e_items.get("record_count", 0),
         "fields": ["run_id", "evalset", "case_id", "type", "query", "hit_at_k", "session_recall", "ndcg"],
         "lineage": {"upstream": ["eval_runs"], "transforms": ["explode", "project", "checksum"]}},
        {"id": "dwd_evalsets", "layer": "dwd", "path": "数据中台/assets/dwd/evalsets.json",
         "desc": "评测集资产明细（内建+生成+公开基准转换）", "rows": e_evalsets["file_count"],
         "fields": ["file", "cases", "type_counts", "isolate_items", "sha256", "origin"],
         "lineage": {"upstream": ["evalsets_local", "benchmarks_local"], "transforms": ["index", "count"]}},
        {"id": "dwd_datasets", "layer": "dwd", "path": "数据中台/assets/dwd/datasets.json",
         "desc": "公开基准原始数据集明细（含指纹卡）", "rows": e_datasets["file_count"],
         "fields": ["file", "sha256", "bytes", "cards[]"],
         "lineage": {"upstream": ["datasets_public"], "transforms": ["index", "checksum", "card-merge"]}},
        {"id": "dwd_test_runs", "layer": "dwd", "path": "数据中台/assets/dwd/test_runs.json",
         "desc": "pytest 回归运行明细（测试中台回流）", "rows": e_tests["record_count"],
         "fields": ["run_id", "summary.pass_rate", "coverage", "gate", "failures_detail"],
         "lineage": {"upstream": ["test_runs"], "transforms": ["parse", "project"]}},
        {"id": "dwd_test_cases", "layer": "dwd", "path": "数据中台/assets/dwd/test_cases.json",
         "desc": "pytest 逐用例明细（junitxml 炸开）", "rows": e_cases.get("record_count", 0),
         "fields": ["run_ts", "classname", "case", "time_s", "status"],
         "lineage": {"upstream": ["test_runs"], "transforms": ["explode", "project"]}},
        {"id": "dwd_obs_runs", "layer": "dwd", "path": "数据中台/assets/dwd/obs_runs.json",
         "desc": "运行时指标明细（监控中台回流）", "rows": e_obs["record_count"],
         "fields": ["run_id", "store.p95_ms", "recall.p95_ms", "hit_rate", "degradation"],
         "lineage": {"upstream": ["obs_runs"], "transforms": ["parse", "project"]}},
        {"id": "dws_repo_health", "layer": "dws", "path": "数据中台/assets/dws/repo_health.json",
         "desc": "仓库健康指标主题", "rows": 1,
         "fields": ["version", "test_files", "test_funcs", "coverage_line_rate", "recent_commits"],
         "lineage": {"upstream": ["dwd_repo", "dwd_coverage"], "transforms": ["aggregate"]}},
        {"id": "dws_experiment_metrics", "layer": "dws", "path": "数据中台/assets/dws/experiment_metrics.json",
         "desc": "核心实验指标主题", "rows": s_metrics["metric_count"],
         "fields": ["experiment_file", "metric", "value", "unit", "source_path"],
         "lineage": {"upstream": ["dwd_experiments"], "transforms": ["curate", "unit-convert"]}},
        {"id": "dws_eval_summary", "layer": "dws", "path": "数据中台/assets/dws/eval_summary.json",
         "desc": "评测结果主题（按评测集）", "rows": s_eval["evalset_count"],
         "fields": ["evalset", "latest_metrics", "per_type", "history[]"],
         "lineage": {"upstream": ["dwd_eval_runs"], "transforms": ["group-by", "latest"]}},
        {"id": "dws_weekly", "layer": "dws", "path": "数据中台/assets/dws/weekly.json",
         "desc": "周对比主题（本周 vs 上周：命中率/通过率，回答「这周比上周好吗」）", "rows": 1,
         "fields": ["eval.this/last", "test.this/last", "overall_verdict", "answer"],
         "lineage": {"upstream": ["dwd_eval_items", "dwd_test_cases", "dwd_obs_runs"],
                      "transforms": ["window-agg", "compare"]}},
    ]
    catalog = build_catalog(prov, assets)

    # 版本三元组（验收 4）：upstream_revision / pipeline_commit / dataset_sha256
    vm_root = next((p.get("root") for p in prov.get("projects", [])
                    if p.get("project_id") == "vibememory"), None)
    upstream = git_head_of(vm_root)
    pipeline_commit = platform_fingerprint(plat)
    total_rows = 0
    for a in catalog.get("assets", []):
        apath = root / a.get("path", "")
        ds = sha256_file(apath) if apath.is_file() else None   # ods_* 是目录，无单文件指纹
        a["version"] = {"upstream_revision": upstream, "pipeline_commit": pipeline_commit,
                        "dataset_sha256": ds}
        total_rows += a.get("rows") or 0
    save_json(plat / "assets" / "catalog.json", catalog)

    # 血缘事件（OpenLineage 风格，落 JSONL）
    ok_sources = [{"namespace": "platform", "name": s.get("id"),
                   "facets": {"sha256": s.get("sha256"), "project": s.get("project")}}
                  for s in prov.get("sources", []) if s.get("status") == "ok"]
    key_outputs = [{"namespace": "platform", "name": a["id"],
                    "facets": {"sha256": a.get("version", {}).get("dataset_sha256"),
                               "rows": a.get("rows"), "version": a.get("version")}}
                   for a in catalog.get("assets", []) if a.get("layer") in ("dwd", "dws")]
    emit_lineage_event(plat, "build_assets", ok_sources, key_outputs)

    print(f"[assets] DWD: experiments={e_exp['record_count']}(解析失败{e_exp['parse_error']}) "
          f"eval_items={e_items.get('record_count', 0)}(源文件{e_items.get('source_files', 0)}/"
          f"解析失败{e_items.get('parse_error', 0)}) eval_runs={e_runs.get('record_count', 0)} "
          f"test_cases={e_cases.get('record_count', 0)}")
    print(f"[assets] DWS: 核心实验指标={s_metrics['metric_count']} 评测集={s_eval['evalset_count']} "
          f"周对比={s_weekly.get('overall_verdict')} 项目活跃="
          f"{','.join(p['project_id'] + ':' + p['verdict'] for p in s_weekly.get('projects', []))}")
    print(f"[assets] 资产记录总量={total_rows}；版本三元组 upstream={upstream} pipeline={pipeline_commit}")
    print(f"[assets] 资产目录 -> {plat / 'assets' / 'catalog.json'}；血缘事件 -> assets/lineage_events.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
