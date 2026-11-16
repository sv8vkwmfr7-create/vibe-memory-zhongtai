#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据中台 · 采集器（数据汇聚层）—— 多项目适配器接口（M1）

从 projects/*.json 项目描述文件登记的可信来源采集数据。接口形状对齐 Singer 式
descriptor + extractor：每种 kind 是一个注册的抽取器（EXTRACTORS），接入新项目
只需新增一个项目描述文件，不改核心代码。

项目描述文件（projects/*.json）：
{
  "project_id": "...", "name": "...",
  "root": {"Windows": "C:/...", "Linux": "/data/home/..."},  # 按操作系统选根
  "license": {"spdx": "MIT"},
  "sources": [{"id", "kind", "path", "pattern?", "required?", "desc"}, ...]
}
路径占位符：{project_root}（项目根）、{root}（中台根）、{repo}（= project_root，兼容旧写法）。
兼容：projects/ 目录不存在时回退读旧版 sources.json（vibememory 单项目）。

用法：
    python collect.py [--root 中台根目录] [--repo 仓库根目录]

只用标准库；文件读取统一 utf-8-sig（仓库惯例）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
ODS_MAX_INLINE_BYTES = 200_000  # 超过该大小的源文件在 ODS 只保留标量摘要，不内嵌全文

# 大文件在 ODS 中保留的关键标量 keypath 白名单（补充摘要用，前缀匹配）
BIG_FILE_KEEP_PREFIXES = (
    "dataset", "experiment", "source", "mode", "scope", "limits", "caveats",
    "selected_instances", "scored", "initial_atoms", "elapsed_seconds",
    "wal_sampled_peak_bytes", "evidence_top5_hits", "mean_turn_evidence_recall",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig")


def walk_scalars(node, prefix: str = "", depth: int = 0, out: dict | None = None, cap: int = 220) -> dict:
    """递归提取 JSON 标量字段（keypath -> value），限制深度与数量，列表只记长度。"""
    if out is None:
        out = {}
    if len(out) >= cap or depth > 4:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            walk_scalars(v, f"{prefix}.{k}" if prefix else str(k), depth + 1, out, cap)
    elif isinstance(node, list):
        out[prefix + ".__len__"] = len(node)
        if node and isinstance(node[0], dict):
            # 对列表内数字子字段求均值（实验 rows 的通用摘要）
            num_acc: dict[str, list] = {}
            for item in node[:2000]:
                if isinstance(item, dict):
                    for k, v in item.items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            num_acc.setdefault(k, []).append(float(v))
            for k, vals in list(num_acc.items())[:10]:
                out[f"{prefix}.[]~mean.{k}"] = round(sum(vals) / len(vals), 6)
    elif isinstance(node, bool):
        out[prefix] = node
    elif isinstance(node, (int, float)):
        out[prefix] = node
    elif isinstance(node, str) and len(node) <= 160:
        out[prefix] = node
    return out


def parse_json_lenient(text: str):
    """解析 JSON；对文件尾附带多余内容的产物做 raw_decode 兜底（取第一个完整对象）。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        dec = json.JSONDecoder()
        return dec.raw_decode(text.lstrip())[0]


def ods_from_json_file(path: Path) -> dict:
    data = parse_json_lenient(read_text(path))
    inline = path.stat().st_size <= ODS_MAX_INLINE_BYTES
    snap = {
        "_snapshot": "full" if inline else "scalars-only",
        "data": data if inline else None,
        "scalars": walk_scalars(data),
    }
    if not inline:
        keep = {k: v for k, v in snap["scalars"].items()
                if any(k.split(".")[0].startswith(p) for p in BIG_FILE_KEEP_PREFIXES)}
        snap["scalars_keep"] = keep
    return snap


def ods_from_coverage_xml(path: Path) -> dict:
    """解析 pytest-cov/cobertura 格式覆盖率报告。"""
    root = ET.parse(str(path)).getroot()
    overall = {
        "line_rate": float(root.get("lines-valid") and root.get("line-rate") or 0),
        "branch_rate": float(root.get("branch-rate") or 0),
        "lines_valid": int(root.get("lines-valid") or 0),
        "lines_covered": int(root.get("lines-covered") or 0),
        "branches_valid": int(root.get("branches-valid") or 0),
        "branches_covered": int(root.get("branches-covered") or 0),
    }
    # 有些生成器把整体指标放在属性，有些只在子节点；做兼容
    if root.get("line-rate"):
        overall["line_rate"] = float(root.get("line-rate"))
    packages = []
    for pkg in root.iter("package"):
        cls = []
        for c in pkg.iter("class"):
            cls.append({
                "name": c.get("filename") or c.get("name") or "",
                "line_rate": float(c.get("line-rate") or 0),
                "branch_rate": float(c.get("branch-rate") or 0),
            })
        packages.append({
            "name": pkg.get("name") or "",
            "line_rate": float(pkg.get("line-rate") or 0),
            "branch_rate": float(pkg.get("branch-rate") or 0),
            "classes": sorted(cls, key=lambda x: x["line_rate"]),
        })
    return {"_snapshot": "parsed-cobertura", "overall": overall, "packages": packages}


def ods_from_markdown(path: Path) -> dict:
    text = read_text(path)
    headings = [{"level": len(m.group(1)), "text": m.group(2).strip()}
                for m in (re.match(r"^(#{1,4})\s+(.+)$", ln) for ln in text.splitlines()) if m]
    dates = sorted({m for m in re.findall(r"20\d{2}-\d{2}-\d{2}", text)})
    return {"_snapshot": "headings", "headings": headings, "heading_count": len(headings),
            "char_count": len(text), "dates_found": dates, "date_min": dates[0] if dates else None,
            "date_max": dates[-1] if dates else None}


def ods_from_test_dir(path: Path, pattern: str) -> dict:
    files = sorted(path.glob(pattern))
    recs = []
    for f in files:
        try:
            n = sum(1 for ln in read_text(f).splitlines() if ln.strip().startswith("def test_"))
        except Exception:
            n = None
        recs.append({"file": f.name, "bytes": f.stat().st_size, "test_funcs": n})
    return {"_snapshot": "file-index", "files": recs, "count": len(recs),
            "test_funcs_total": sum(r["test_funcs"] or 0 for r in recs)}


def ods_from_git_log(path: Path, max_commits: int) -> dict:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "log", f"-n{max_commits}", "--pretty=format:%H%x09%ad%x09%s", "--date=iso"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        ).stdout
    except Exception as e:
        return {"_snapshot": "error", "error": f"git 不可用: {e}"}
    commits = []
    for ln in out.splitlines():
        parts = ln.split("\t", 2)
        if len(parts) == 3:
            commits.append({"hash": parts[0][:10], "date": parts[1], "subject": parts[2]})
    return {"_snapshot": "git-log", "commits": commits, "count": len(commits)}


def ods_from_toml(path: Path) -> dict:
    try:
        import tomllib
        data = tomllib.loads(read_text(path))
    except Exception as e:  # py<3.11 兜底
        return {"_snapshot": "error", "error": f"toml 解析失败: {e}"}
    proj = data.get("project", {})
    return {"_snapshot": "parsed-toml", "scalars": {
        "project.name": proj.get("name"),
        "project.version": proj.get("version"),
        "project.requires-python": proj.get("requires-python"),
        "project.dependencies.__len__": len(proj.get("dependencies", []) or []),
        "project.optional-dependencies.__len__": len(proj.get("optional-dependencies", {}) or {}),
        "entry_points.__len__": len((proj.get("scripts") or {})),
    }}


# ---------------------------------------------------------------- 抽取器注册表
# Singer 式 extractor：每种 kind 一个函数，签名 (path, src) -> entry.update 载荷。
# 新增数据形态 = 新增一个 @extractor("kind")，不改 collect_source / main。

EXTRACTORS: dict[str, object] = {}


def extractor(kind: str):
    def deco(fn):
        EXTRACTORS[kind] = fn
        return fn
    return deco


def _file_stat(path: Path) -> dict:
    st = path.stat()
    return {"sha256": sha256_file(path), "bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")}


@extractor("jsonl_dir")
def ex_jsonl_dir(path: Path, src: dict) -> dict:
    pattern = src.get("pattern", "*.jsonl")
    files = sorted(path.glob(pattern))
    recs = []
    for f in files:
        lines = [ln for ln in read_text(f).splitlines() if ln.strip()]
        type_counter, isolate_n = {}, 0
        for ln in lines:
            try:
                it = json.loads(ln)
                type_counter[it.get("type", "?")] = type_counter.get(it.get("type", "?"), 0) + 1
                if it.get("isolate"):
                    isolate_n += 1
            except Exception:
                pass
        recs.append({"file": f.name, "sha256": sha256_file(f), "bytes": f.stat().st_size,
                     "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
                     "records": len(lines), "type_counts": type_counter,
                     "isolate_items": isolate_n,
                     "parse_error": None if len(lines) else "empty"})
    return {"records": len(recs), "ods": {"files": recs}}


@extractor("file_dir")
def ex_file_dir(path: Path, src: dict) -> dict:
    pattern = src.get("pattern", "*")
    files = sorted(p for p in path.rglob(pattern.replace("**/", ""))
                   if p.is_file()) if pattern.startswith("**") else sorted(p for p in path.glob(pattern) if p.is_file())
    recs = []
    for f in files:
        rel = f.relative_to(path).as_posix()
        recs.append({"file": rel, "sha256": sha256_file(f), "bytes": f.stat().st_size,
                     "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")})
    return {"records": len(recs), "total_bytes": sum(r["bytes"] for r in recs),
            "ods": {"files": recs}}


@extractor("json_dir")
def ex_json_dir(path: Path, src: dict) -> dict:
    pattern = src.get("pattern", "*.json")
    files = sorted(path.glob(pattern))
    recs, total_bytes = [], 0
    for f in files:
        total_bytes += f.stat().st_size
        rec = {"file": f.name, "sha256": sha256_file(f), "bytes": f.stat().st_size,
               "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")}
        try:
            rec.update({k: v for k, v in ods_from_json_file(f).items()})
        except Exception as e:
            rec["parse_error"] = str(e)
        recs.append(rec)
    return {"records": len(recs), "total_bytes": total_bytes, "ods": {"files": recs}}


@extractor("json_file")
def ex_json_file(path: Path, src: dict) -> dict:
    return {**_file_stat(path), "ods": ods_from_json_file(path)}


@extractor("coverage_xml")
def ex_coverage_xml(path: Path, src: dict) -> dict:
    return {**_file_stat(path), "ods": ods_from_coverage_xml(path)}


@extractor("markdown")
def ex_markdown(path: Path, src: dict) -> dict:
    return {**_file_stat(path), "ods": ods_from_markdown(path)}


@extractor("markdown_dir")
def ex_markdown_dir(path: Path, src: dict) -> dict:
    """markdown 目录（新增 kind：接入文档型项目用）。"""
    pattern = src.get("pattern", "*.md")
    files = sorted(path.glob(pattern))
    recs = []
    for f in files:
        ods = ods_from_markdown(f)
        recs.append({"file": f.name, "sha256": sha256_file(f), "bytes": f.stat().st_size,
                     "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
                     "heading_count": ods.get("heading_count"), "char_count": ods.get("char_count"),
                     "date_min": ods.get("date_min"), "date_max": ods.get("date_max")})
    return {"records": len(recs), "total_bytes": sum(r["bytes"] for r in recs), "ods": {"files": recs}}


@extractor("jsonl_file")
def ex_jsonl_file(path: Path, src: dict) -> dict:
    """单个 JSONL 文件（新增 kind：事件流/逐行记录型数据）。只做行数与类型统计摘要，不内嵌全文。"""
    lines = [ln for ln in read_text(path).splitlines() if ln.strip()]
    type_counter: dict[str, int] = {}
    for ln in lines[:20000]:
        try:
            it = json.loads(ln)
            k = str(it.get("type", it.get("event", "?")))
            type_counter[k] = type_counter.get(k, 0) + 1
        except Exception:
            pass
    return {**_file_stat(path), "records": len(lines),
            "ods": {"_snapshot": "jsonl-summary", "total_lines": len(lines),
                    "type_counts": type_counter, "types_capped": len(lines) > 20000}}


@extractor("test_dir")
def ex_test_dir(path: Path, src: dict) -> dict:
    return {"ods": ods_from_test_dir(path, src.get("pattern", "test_*.py"))}


@extractor("git_log")
def ex_git_log(path: Path, src: dict) -> dict:
    return {"ods": ods_from_git_log(path, int(src.get("max_commits", 50)))}


@extractor("toml_file")
def ex_toml_file(path: Path, src: dict) -> dict:
    return {**_file_stat(path), "ods": ods_from_toml(path)}


# ---------------------------------------------------------------- 项目描述与采集

def pick_root(spec_root, fallback: str = "") -> str:
    """项目根解析：支持按操作系统给根（{"Windows": ..., "Linux": ...}）。"""
    if isinstance(spec_root, dict):
        sysname = "Windows" if os.name == "nt" else "Linux"
        return str(spec_root.get(sysname) or spec_root.get("default") or fallback)
    if not spec_root:
        return fallback
    return str(spec_root)


def resolve_path(path_tpl: str, ctx: dict) -> Path:
    p = path_tpl.replace("{project_root}", str(ctx.get("project_root", "")))
    p = p.replace("{repo}", str(ctx.get("project_root", "")))  # 旧写法兼容：{repo} = 项目根
    p = p.replace("{root}", str(ctx.get("root", "")))
    return Path(p)


def collect_source(src: dict, ctx: dict) -> dict:
    path = resolve_path(src["path"], ctx)
    entry = {
        "id": src["id"], "kind": src["kind"], "path": str(path),
        "desc": src.get("desc", ""), "required": src.get("required", True),
        "project": ctx.get("project_id", ""),
        "origin": src.get("origin") or f"{ctx.get('project_name', '')} 来源",
        "collected_at": now_iso(), "status": "ok",
    }
    if not path.exists():
        entry["status"] = "missing"
        entry["error"] = "路径不存在"
        return entry
    try:
        fn = EXTRACTORS.get(src["kind"])
        if fn is None:
            entry["status"] = "unsupported"
            entry["error"] = f"未知来源类型 {src['kind']}"
        else:
            entry.update(fn(path, src))
    except Exception as e:
        entry["status"] = "error"
        entry["error"] = f"{type(e).__name__}: {e}"
    return entry


def load_projects(plat: Path):
    """加载项目描述文件；projects/ 不存在时回退旧版 sources.json（单项目兼容）。"""
    pdir = plat / "projects"
    if pdir.is_dir():
        return [json.loads(read_text(pf)) for pf in sorted(pdir.glob("*.json"))]
    spec = json.loads(read_text(plat / "sources.json"))
    return [{"project_id": "vibememory", "name": "VibeMemory", "root": spec.get("source_repo", ""),
             "sources": spec["sources"], "_legacy": True}]


def main() -> int:
    ap = argparse.ArgumentParser(description="数据中台采集器（多项目）")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="中台根目录")
    ap.add_argument("--repo", default=None, help="vibememory 项目根目录覆盖（服务器流水线传入）")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    plat = root / "数据中台"

    projects = load_projects(plat)
    print(f"[collect] 中台根目录: {root}")
    print(f"[collect] 接入项目: {len(projects)} 个 —— {', '.join(p.get('project_id', '?') for p in projects)}")

    prov_path = plat / "assets" / "provenance.json"
    prev = {}
    if prov_path.exists():
        try:
            prev = {e["id"]: e for e in json.loads(read_text(prov_path)).get("sources", [])}
        except Exception:
            prev = {}

    entries, ok_n, miss_n = [], 0, 0
    proj_summary = []
    multi = len(projects) > 1
    for proj in projects:
        pid = proj.get("project_id", "?")
        proot = pick_root(proj.get("root"), fallback="")
        if args.repo and pid == "vibememory":
            proot = args.repo  # 服务器流水线 --repo 覆盖（保持原语义）
        ctx = {"root": str(root), "project_id": pid,
               "project_name": proj.get("name", pid), "project_root": proot}
        print(f"[collect] 项目 {pid}（{ctx['project_name']}）根: {proot or '(未配置)'}")
        p_ok, p_total = 0, 0
        for src in proj.get("sources", []):
            e = collect_source(src, ctx)
            last = prev.get(e["id"], {})
            e["previous_sha256"] = last.get("sha256") or last.get("ods_sha256")
            e["changed_since_last"] = None if not e.get("sha256") else (e["sha256"] != e["previous_sha256"])
            if e["status"] == "ok":
                ok_n += 1
                p_ok += 1
            else:
                miss_n += 1
            p_total += 1
            entries.append(e)
            prefix = f"{pid}/" if multi else ""
            print(f"[collect] {prefix}{e['id']:<22} {e['status']:<8} {e.get('records', e.get('bytes', ''))}"
                  + (f"  (变更)" if e.get("changed_since_last") else ""))
        proj_summary.append({"project_id": pid, "name": ctx["project_name"],
                             "root": proot, "sources_ok": p_ok, "sources_total": p_total})

    required_missing = [e["id"] for e in entries
                        if e["status"] != "ok" and e["required"] and e["status"] != "unsupported"]
    doc = {
        "generated_at": now_iso(),
        "platform_root": str(root),
        "projects": proj_summary,
        "sources": entries,
        "summary": {
            "total": len(entries), "ok": ok_n,
            "missing_or_error": miss_n, "required_missing": required_missing,
        },
    }
    # ODS 快照逐来源落盘
    ods_dir = plat / "assets" / "ods"
    ods_dir.mkdir(parents=True, exist_ok=True)
    for e in entries:
        if "ods" in e:
            snap = dict(e)
            snap.pop("ods", None)
            snap["ods"] = e["ods"]
            (ods_dir / f"{e['id']}.json").write_text(
                json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")

    prov_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    # 血缘事件（OpenLineage 风格）：采集作业的输入=可信来源，输出=溯源档案+ODS 快照
    ev_inputs = [{"namespace": "platform", "name": e["id"],
                  "facets": {"sha256": e.get("sha256"), "project": e.get("project")}}
                 for e in entries if e["status"] == "ok"]
    ev_outputs = [{"namespace": "platform", "name": "provenance.json",
                   "facets": {"sha256": sha256_file(prov_path), "sources_ok": ok_n}}]
    ev_outputs += [{"namespace": "platform", "name": f"ods/{e['id']}"}
                   for e in entries if e["status"] == "ok" and "ods" in e]
    ev = {"eventType": "COMPLETE", "eventTime": now_iso(), "producer": "数据中台/collect",
          "job": {"namespace": "vibememory-zhongtai", "name": "collect"},
          "run": {"runId": str(uuid.uuid4())}, "inputs": ev_inputs, "outputs": ev_outputs}
    lineage_path = plat / "assets" / "lineage_events.jsonl"
    with lineage_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    print(f"[collect] 完成：{ok_n} 正常 / {miss_n} 异常；溯源档案 -> {prov_path.name}")
    if required_missing:
        print(f"[collect][警告] 必需来源缺席: {required_missing}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
