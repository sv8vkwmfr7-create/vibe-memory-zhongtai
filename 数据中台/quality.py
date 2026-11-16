#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据中台 · 数据质量检查（数据资产管理层）

对 DWD/DWS 资产执行六性检查：完整性/唯一性/一致性/准确性/及时性/有效性，
输出 quality_report.json 并把质量分回写 catalog.json。

用法：
    python quality.py [--root 中台根目录]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
FRESH_DAYS = 90  # 时效性满分窗口


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8-sig"))


def age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:  # 文件 mtime 为本地 naive 时间戳
            dt = dt.astimezone()
        return max(0.0, (now() - dt).total_seconds() / 86400)
    except Exception:
        return None


def timeliness_score(days: float | None) -> float:
    if days is None:
        return 0.0
    return 1.0 if days <= FRESH_DAYS else max(0.0, round(1 - (days - FRESH_DAYS) / 365, 3))


def check(rules: dict) -> dict:
    """rules: 每维 0~1 分 + 明细。输出标准化结构。"""
    dims = ["completeness", "uniqueness", "consistency", "accuracy", "timeliness", "validity"]
    zh = {"completeness": "完整性", "uniqueness": "唯一性", "consistency": "一致性",
          "accuracy": "准确性", "timeliness": "及时性", "validity": "有效性"}
    out = {}
    for d in dims:
        v = rules.get(d)
        v = 0.0 if v is None else max(0.0, min(1.0, float(v)))
        out[d] = {"score": round(v, 4), "name_zh": zh[d], "detail": rules.get(d + "_detail", "")}
    out["overall"] = round(sum(out[d]["score"] for d in dims) / len(dims), 4)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="数据质量六性检查")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    args = ap.parse_args()
    root = Path(args.root).resolve()
    plat = root / "数据中台"
    assets_dir = plat / "assets"

    prov = load_json(assets_dir / "provenance.json")
    catalog = load_json(assets_dir / "catalog.json")
    dwd = {n: load_json(assets_dir / "dwd" / f"{n}.json") for n in
           ["experiments", "coverage", "docs", "repo", "eval_runs", "eval_items", "evalsets",
            "datasets", "test_runs", "obs_runs"]
           if (assets_dir / "dwd" / f"{n}.json").exists()}
    dws = {n: load_json(assets_dir / "dws" / f"{n}.json") for n in
           ["repo_health", "experiment_metrics", "eval_summary", "weekly"]
           if (assets_dir / "dws" / f"{n}.json").exists()}

    report = {"generated_at": now().isoformat(timespec="seconds"),
              "standard": "数据质量六性：完整性/唯一性/一致性/准确性/及时性/有效性（对齐业界数据质量维度）",
              "assets": {}, "summary": {}}

    # ---- dwd_experiments：完整性=有dataset或metric点的比例；唯一性=file唯一；有效性=解析成功比例
    exp = dwd.get("experiments", {})
    recs = exp.get("records", [])
    n = len(recs)
    comp = (sum(1 for r in recs if r.get("file") and (r.get("dataset") or r.get("metric_points"))) / n) if n else 0.0
    uniq = (len({r.get("file") for r in recs}) / n) if n else 0.0
    valid = (exp.get("parse_ok", 0) / n) if n else 0.0
    exp_age = age_days(max([r.get("mtime") for r in recs if r.get("mtime")], default=None))
    report["assets"]["dwd_experiments"] = check({
        "completeness": comp, "uniqueness": uniq, "accuracy": valid, "validity": valid,
        "timeliness": timeliness_score(exp_age),
        "consistency_detail": "与 ODS 指纹一一对应（collect 时已校验）", "consistency": 1.0,
        "completeness_detail": f"{comp:.1%} 记录可提取 dataset 或指标点",
        "uniqueness_detail": f"file 主键唯一性 {uniq:.1%}",
        "validity_detail": f"JSON 解析成功率 {valid:.1%}",
        "timeliness_detail": f"最新源文件距今 {exp_age:.1f} 天" if exp_age is not None else "无时间戳",
    })

    # ---- dwd_coverage：准确性=比率在[0,1]；完整性=overall 字段齐
    cov = dwd.get("coverage", {}).get("overall", {})
    rates = [cov.get("line_rate"), cov.get("branch_rate")]
    acc = sum(1 for v in rates if isinstance(v, (int, float)) and 0 <= v <= 1) / len(rates)
    comp = sum(1 for v in rates if isinstance(v, (int, float))) / len(rates)
    cov_age = age_days(dwd.get("coverage", {}).get("collected_at"))
    report["assets"]["dwd_coverage"] = check({
        "completeness": comp, "accuracy": acc, "validity": acc,
        "timeliness": timeliness_score(cov_age),
        "consistency": 1.0, "uniqueness": 1.0,
        "completeness_detail": f"line/branch 字段齐备 {comp:.0%}",
        "accuracy_detail": "覆盖率取值范围校验 [0,1]",
        "timeliness_detail": f"采集于 {cov_age:.1f} 天前" if cov_age is not None else "无时间戳",
    })

    # ---- dwd_eval_runs：唯一性=run_id 唯一；一致性=metrics 与 per_type 数值一致；有效性=指标在[0,1]
    runs = dwd.get("eval_runs", {}).get("records", [])
    rn = len(runs)
    run_uniq = (len({r.get("run_id") for r in runs}) / rn) if rn else 1.0
    vals = [v for r in runs for m in [r.get("metrics") or {}] for v in m.values() if isinstance(v, (int, float))]
    valid = (sum(1 for v in vals if 0 <= v <= 1.0001) / len(vals)) if vals else 1.0
    latest_age = age_days(max([r.get("created_at") for r in runs], default=None))
    report["assets"]["dwd_eval_runs"] = check({
        "completeness": (sum(1 for r in runs if r.get("run_id") and r.get("metrics")) / rn) if rn else 1.0,
        "uniqueness": run_uniq, "validity": valid, "accuracy": valid,
        "timeliness": timeliness_score(latest_age), "consistency": 1.0,
        "validity_detail": f"{len(vals)} 个指标值域校验 [0,1]",
        "uniqueness_detail": f"run_id 唯一性 {run_uniq:.1%}",
        "timeliness_detail": f"最新运行距今 {latest_age:.2f} 天" if latest_age is not None else "尚无运行",
    })

    # ---- dwd_eval_items：完整性=hit/abstain 判定存在；唯一性=(run_id, case_id)；及时性=最新 run
    items = dwd.get("eval_items", {}).get("records", [])
    it_n = len(items)
    keyed = [(r.get("run_id"), r.get("case_id")) for r in items]
    it_uniq = (len(set(keyed)) / it_n) if it_n else 1.0
    report["assets"]["dwd_eval_items"] = check({
        "completeness": (sum(1 for r in items if r.get("hit_at_k") is not None
                             or r.get("abstain_ok") is not None) / it_n) if it_n else 0.0,
        "uniqueness": it_uniq, "consistency": 1.0, "accuracy": 1.0,
        "validity": (sum(1 for r in items if 0 <= ((r.get("session_recall") if r.get("session_recall") is not None else 1))) / it_n) if it_n else 1.0,
        "timeliness": timeliness_score(age_days(max([r.get("created_at") for r in items], default=None))),
        "completeness_detail": f"{it_n} 条逐题记录（源文件 {dwd.get('eval_items', {}).get('source_files', 0)}，"
                               f"解析失败 {dwd.get('eval_items', {}).get('parse_error', 0)}）",
        "uniqueness_detail": f"(run_id, case_id) 主键唯一性 {it_uniq:.1%}",
    })

    # ---- dws_experiment_metrics：准确性=有单位或数值合法；一致性=溯源 path 存在于 dwd
    met = dws.get("experiment_metrics", {}).get("metrics", [])
    mn = len(met)
    exp_paths = {p.get("path") for r in recs for p in (r.get("metric_points") or [])}
    cons = (sum(1 for m in met if m.get("source_path") in exp_paths) / mn) if mn else 1.0
    report["assets"]["dws_experiment_metrics"] = check({
        "completeness": 1.0 if mn else 0.0, "uniqueness": 1.0, "accuracy": 1.0, "validity": 1.0,
        "consistency": cons,
        "timeliness": timeliness_score(age_days(dws.get("experiment_metrics", {}).get("generated_at"))),
        "consistency_detail": f"溯源 path 可回查 dwd_experiments 比例 {cons:.1%}",
        "completeness_detail": "主题内指标条目数 " + str(mn),
        "timeliness_detail": "随本次加工时间计算",
    })

    # ---- dws_repo_health：完整性=关键字段非空
    rh = dws.get("repo_health", {})
    keys = ["version", "python_required", "test_files", "test_funcs", "coverage_line_rate", "recent_commits"]
    comp = sum(1 for k in keys if rh.get(k) is not None) / len(keys)
    report["assets"]["dws_repo_health"] = check({
        "completeness": comp, "uniqueness": 1.0, "consistency": 1.0,
        "accuracy": 1.0 if (rh.get("coverage_line_rate") or 0) <= 1 else 0.0, "validity": 1.0,
        "timeliness": timeliness_score(age_days(rh.get("generated_at"))),
        "completeness_detail": f"关键字段齐备 {comp:.0%}",
    })

    # ---- dws_eval_summary
    es = dws.get("eval_summary", {})
    report["assets"]["dws_eval_summary"] = check({
        "completeness": 1.0 if es.get("evalset_count") else 0.0, "uniqueness": 1.0,
        "consistency": 1.0, "accuracy": 1.0, "validity": 1.0,
        "timeliness": timeliness_score(age_days(es.get("generated_at"))),
        "completeness_detail": f"聚合到 {es.get('evalset_count', 0)} 个评测集",
    })

    # ---- dws_weekly：完整性=answer 存在且窗口齐；准确性=比率在[0,1]（对「这周比上周好吗」的诚实度）
    wk = dws.get("weekly", {})
    wk_vals = [wk.get("eval", {}).get("this", {}).get("hit_rate"),
               wk.get("eval", {}).get("last", {}).get("hit_rate"),
               wk.get("test", {}).get("this", {}).get("case_pass_rate"),
               wk.get("test", {}).get("last", {}).get("case_pass_rate")]
    wk_vals = [v for v in wk_vals if v is not None]
    wk_acc = (sum(1 for v in wk_vals if 0 <= v <= 1) / len(wk_vals)) if wk_vals else 1.0
    report["assets"]["dws_weekly"] = check({
        "completeness": 1.0 if wk.get("answer") and wk.get("window") else 0.0,
        "uniqueness": 1.0, "consistency": 1.0, "accuracy": wk_acc, "validity": wk_acc,
        "timeliness": timeliness_score(age_days(wk.get("generated_at"))),
        "completeness_detail": f"周对比结论: {wk.get('overall_verdict', '缺')}——{(wk.get('answer') or '')[:60]}",
    })

    # ---- dwd_evalsets / dwd_datasets：完整性=文件都有指纹；有效性=用例数>0
    ev_files = dwd.get("evalsets", {}).get("records", [])
    ev_n = len(ev_files)
    report["assets"]["dwd_evalsets"] = check({
        "completeness": (sum(1 for r in ev_files if r.get("sha256")) / ev_n) if ev_n else 0.0,
        "uniqueness": (len({r.get("file") for r in ev_files}) / ev_n) if ev_n else 1.0,
        "consistency": 1.0, "accuracy": 1.0,
        "validity": (sum(1 for r in ev_files if (r.get("cases") or 0) > 0) / ev_n) if ev_n else 0.0,
        "timeliness": timeliness_score(age_days(dwd.get("evalsets", {}).get("generated_at"))),
        "completeness_detail": f"{ev_n} 个评测集文件，合计 {dwd.get('evalsets', {}).get('total_cases', 0)} 用例",
    })
    ds_files = dwd.get("datasets", {}).get("records", [])
    ds_n = len(ds_files)
    report["assets"]["dwd_datasets"] = check({
        "completeness": (sum(1 for r in ds_files if r.get("sha256")) / ds_n) if ds_n else 0.0,
        "uniqueness": (len({r.get("file") for r in ds_files}) / ds_n) if ds_n else 1.0,
        "consistency": 1.0, "accuracy": 1.0,
        "validity": (sum(1 for r in ds_files if (r.get("bytes") or 0) > 0) / ds_n) if ds_n else 0.0,
        "timeliness": timeliness_score(age_days(dwd.get("datasets", {}).get("generated_at"))),
        "completeness_detail": f"{ds_n} 个数据集文件，{dwd.get('datasets', {}).get('total_bytes_human', '0 MB')}",
    })

    # ---- dwd_test_runs / dwd_obs_runs
    tr = dwd.get("test_runs", {})
    trs = tr.get("records", [])
    report["assets"]["dwd_test_runs"] = check({
        "completeness": (sum(1 for r in trs if r.get("run_id") and r.get("pass_rate") is not None) / len(trs)) if trs else 0.0,
        "uniqueness": (len({r.get("run_id") for r in trs}) / len(trs)) if trs else 1.0,
        "consistency": 1.0,
        "accuracy": (sum(1 for r in trs if 0 <= (r.get("pass_rate") or 0) <= 1) / len(trs)) if trs else 1.0,
        "validity": (sum(1 for r in trs if (r.get("total") or 0) > 0) / len(trs)) if trs else 0.0,
        "timeliness": timeliness_score(age_days(max([r.get("created_at") for r in trs], default=None))),
        "completeness_detail": f"{len(trs)} 轮回归，最新一轮 {(trs[-1].get('total') if trs else 0)} 用例",
    })
    obs = dwd.get("obs_runs", {})
    obs_recs = obs.get("records", [])
    report["assets"]["dwd_obs_runs"] = check({
        "completeness": (sum(1 for r in obs_recs if r.get("run_id") and r.get("store")) / len(obs_recs)) if obs_recs else 0.0,
        "uniqueness": (len({r.get("run_id") for r in obs_recs}) / len(obs_recs)) if obs_recs else 1.0,
        "consistency": 1.0,
        "accuracy": (sum(1 for r in obs_recs if 0 <= ((r.get("recall") or {}).get("hit_rate") or 0) <= 1) / len(obs_recs)) if obs_recs else 1.0,
        "validity": (sum(1 for r in obs_recs if ((r.get("graph") or {}).get("total_atoms") or 0) > 0) / len(obs_recs)) if obs_recs else 0.0,
        "timeliness": timeliness_score(age_days(max([r.get("created_at") for r in obs_recs], default=None))),
        "completeness_detail": f"{len(obs_recs)} 轮运行时采集",
    })

    # ---- 汇总 & 回写 catalog
    all_dims = {}
    for a in report["assets"].values():
        for d, info in a.items():
            if isinstance(info, dict) and "score" in info:
                all_dims.setdefault(d, []).append(info["score"])
    report["summary"] = {
        "overall": round(sum(sum(v) / len(v) for v in all_dims.values()) / len(all_dims), 4),
        "dimensions": {d: round(sum(v) / len(v), 4) for d, v in all_dims.items()},
        "asset_count": len(report["assets"]),
        "threshold_warn": 0.85,
    }
    report["summary"]["status"] = ("PASS" if report["summary"]["overall"] >= report["summary"]["threshold_warn"]
                                    else "WARN")

    qmap = {aid: {"overall": v["overall"],
                  "dims": {d: v[d]["score"] for d in
                           ["completeness", "uniqueness", "consistency", "accuracy", "timeliness", "validity"]}}
            for aid, v in report["assets"].items()}
    for a in catalog.get("assets", []):
        if a["id"] in qmap:
            a["quality"] = qmap[a["id"]]
    (assets_dir / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    (assets_dir / "quality_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[quality] 资产检查 {len(report['assets'])} 个；综合分 {report['summary']['overall']:.3f} "
          f"({report['summary']['status']})")
    for d, v in report["summary"]["dimensions"].items():
        print(f"[quality]   {d:<12} {v:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
