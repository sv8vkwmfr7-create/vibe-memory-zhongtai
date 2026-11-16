#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VibeMemory 四中台服务器：静态大屏 + 数据流水线 API + 数据对外 API

- GET  /             → pages/ 静态大屏
- GET  /api/status   → {running, last_run, last_result, head}
- POST /api/refresh  → 在服务器上执行数据流水线（collect→build→quality→pages），带互斥锁
- GET  /api/data/*   → 数据对外服务（别的程序直接取数，全部 JSON）：
    /api/data/overview                  总览：项目/来源/资产/质量/端点自述
    /api/data/catalog                   资产目录（含血缘 lineage）
    /api/data/provenance                溯源档案（逐来源指纹/记录数/变更）
    /api/data/quality                   质量六性报告
    /api/data/cards                     数据集卡（每类来源的身份证）
    /api/data/ods/<source_id>           单一来源 ODS 快照（id 需在 provenance 登记）
    /api/data/runs/<eval|test|obs>      运行存档清单
    /api/data/runs/<eval|test|obs>?name=<文件名>   取单个存档原文

只用标准库；线程化以支持流水线运行期间页面继续可访问。
ROOT 可用环境变量 ZHONGTAI_ROOT 覆盖（本机测试用），默认为服务器路径。
"""
import datetime
import http.server
import json
import os
import re
import socketserver
import subprocess
import threading

ROOT = os.environ.get("ZHONGTAI_ROOT", "/data/home/VibeMemory-中台")
REPO = os.environ.get("ZHONGTAI_REPO", "/data/home/vibe-memory")
PIPELINE = ["bash", ROOT + "/scripts/server/server_pipeline.sh"]
PLATFORM = os.path.join(ROOT, "数据中台")
RUN_DIRS = {"eval": os.path.join(ROOT, "评测中台", "runs"),
            "test": os.path.join(ROOT, "测试中台", "runs"),
            "obs": os.path.join(ROOT, "监控中台", "runs")}

state = {"running": False, "last_run": None, "last_result": None}
lock = threading.Lock()


def git_head():
    try:
        return subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return None


def load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def build_overview():
    catalog = load_json(os.path.join(PLATFORM, "assets", "catalog.json")) or {}
    quality = load_json(os.path.join(PLATFORM, "assets", "quality_report.json")) or {}
    prov = load_json(os.path.join(PLATFORM, "assets", "provenance.json")) or {}
    cards = load_json(os.path.join(PLATFORM, "dataset_cards.json")) or {}
    assets = catalog.get("assets", [])
    return {
        "service": "VibeMemory 数据中台 · 数据对外 API",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "projects": prov.get("projects", []),
        "sources": prov.get("summary", {}),
        "assets": {"count": len(assets),
                   "records_total": sum(a.get("rows") or 0 for a in assets)},
        "quality": quality.get("summary", {}),
        "dataset_cards": {"count": len(cards.get("cards", []))},
        "endpoints": ["/api/data/overview", "/api/data/catalog", "/api/data/provenance",
                      "/api/data/quality", "/api/data/cards", "/api/data/lineage",
                      "/api/data/weekly", "/api/data/signals", "/api/data/diff?from&to",
                      "/api/data/ods/<source_id>", "/api/data/runs/<eval|test|obs>[?name=文件名]"],
    }


def _read_lineage_events() -> list[dict]:
    lp = os.path.join(PLATFORM, "assets", "lineage_events.jsonl")
    try:
        with open(lp, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def build_diff(date_from: str, date_to: str) -> dict:
    """数据比对（借鉴 AllData 数据比对管理）：基于 lineage 事件重建两天末的状态快照并对比。

    - 来源状态来自 collect 事件的 inputs（source_id → sha256）
    - 资产状态来自 build_assets 事件的 outputs（asset_id → sha256/rows）
    - 「某天末的状态」= 该日期（含）之前最后一条事件所刻画的状态
    """
    events = _read_lineage_events()
    if not events:
        return {"_error": "lineage_events.jsonl 不存在或为空（先跑流水线）"}

    def snapshot(upto: str) -> tuple[dict, dict]:
        sources: dict[str, str] = {}
        assets: dict[str, dict] = {}
        for ev in sorted(events, key=lambda e: e.get("eventTime", "")):
            d = ev.get("eventTime", "")[:10]
            if d > upto:
                continue
            job = ev.get("job", {}).get("name")
            if job == "collect":
                for inp in ev.get("inputs", []):
                    sources[inp.get("name", "")] = (inp.get("facets") or {}).get("sha256")
            elif job == "build_assets":
                for out in ev.get("outputs", []):
                    fc = out.get("facets") or {}
                    assets[out.get("name", "")] = {"sha256": fc.get("sha256"),
                                                   "rows": fc.get("rows")}
        return sources, assets

    src_a, ast_a = snapshot(date_from)
    src_b, ast_b = snapshot(date_to)

    def diff_map(a: dict, b: dict, rows_key: bool):
        changed, added, removed = [], [], []
        for k in sorted(set(a) | set(b)):
            va, vb = a.get(k), b.get(k)
            if k not in a:
                added.append({"id": k, **({"rows": vb.get("rows")} if rows_key else {})})
            elif k not in b:
                removed.append({"id": k})
            elif va != vb:
                entry = {"id": k}
                if rows_key and isinstance(vb, dict):
                    entry.update({"rows_from": (va or {}).get("rows"),
                                  "rows_to": vb.get("rows"),
                                  "sha256_changed": (va or {}).get("sha256") != (vb or {}).get("sha256")})
                else:
                    entry["sha256_changed"] = True
                changed.append(entry)
        return {"changed": changed, "added": added, "removed": removed,
                "count_unchanged": sum(1 for k in set(a) & set(b) if a[k] == b[k])}

    src_diff = diff_map(src_a, src_b, False)
    ast_diff = diff_map(ast_a, ast_b, True)
    rows_from = sum((v or {}).get("rows") or 0 for v in ast_a.values())
    rows_to = sum((v or {}).get("rows") or 0 for v in ast_b.values())
    return {"from": date_from, "to": date_to,
            "sources": src_diff,
            "assets": ast_diff,
            "rows_total": {"from": rows_from, "to": rows_to,
                           "delta": rows_to - rows_from}}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT + "/pages", **kw)

    def log_message(self, *a):  # 静默访问日志
        pass

    def end_headers(self):
        # 大屏数据常更新：禁用浏览器缓存，避免改版后吃到旧页面
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            self._json({"running": state["running"], "last_run": state["last_run"],
                        "last_result": state["last_result"], "head": git_head()})
        elif self.path.startswith("/api/data/"):
            try:
                self._api_data(self.path[len("/api/data/"):])
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        else:
            super().do_GET()

    def _api_data(self, rest: str):
        path, _, query = rest.partition("?")
        path = path.strip("/")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path == "overview":
            self._json(build_overview())
        elif path == "catalog":
            self._json(load_json(os.path.join(PLATFORM, "assets", "catalog.json")) or {"_error": "catalog 不存在"})
        elif path == "provenance":
            self._json(load_json(os.path.join(PLATFORM, "assets", "provenance.json")) or {"_error": "provenance 不存在"})
        elif path == "quality":
            self._json(load_json(os.path.join(PLATFORM, "assets", "quality_report.json")) or {"_error": "quality_report 不存在"})
        elif path == "cards":
            self._json(load_json(os.path.join(PLATFORM, "dataset_cards.json")) or {"_error": "dataset_cards 不存在"})
        elif path == "weekly":
            self._json(load_json(os.path.join(PLATFORM, "assets", "dws", "weekly.json"))
                       or {"_error": "weekly 不存在（先跑 build_assets）"})
        elif path == "signals":
            self._json(load_json(os.path.join(PLATFORM, "assets", "signals.json"))
                       or {"_error": "signals 不存在（先跑 signals.py）"})
        elif path == "diff":
            date_from = params.get("from", "")
            date_to = params.get("to", "")
            date_re = re.fullmatch
            if not (date_re(r"\d{4}-\d{2}-\d{2}", date_from or "")
                    and date_re(r"\d{4}-\d{2}-\d{2}", date_to or "")):
                self._json({"error": "需要 from=YYYY-MM-DD&to=YYYY-MM-DD"}, 400)
                return
            if date_from > date_to:
                date_from, date_to = date_to, date_from
            self._json(build_diff(date_from, date_to))
        elif path == "lineage":
            lp = os.path.join(PLATFORM, "assets", "lineage_events.jsonl")
            evs = []
            try:
                with open(lp, encoding="utf-8") as f:
                    evs = [json.loads(ln) for ln in f if ln.strip()]
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            self._json({"count": len(evs), "events": evs[-50:]})
        elif path.startswith("ods/"):
            sid = path[len("ods/"):]
            if not re.fullmatch(r"[a-z0-9_]+", sid or ""):
                self._json({"error": "source_id 只允许 [a-z0-9_]"}, 400)
                return
            doc = load_json(os.path.join(PLATFORM, "assets", "ods", f"{sid}.json"))
            if doc is None:
                self._json({"error": f"来源 {sid} 无 ODS 快照"}, 404)
            else:
                self._json(doc)
        elif path.startswith("runs/"):
            kind = path[len("runs/"):]
            if kind not in RUN_DIRS:
                self._json({"error": "runs 类别只支持 eval|test|obs"}, 400)
                return
            name = params.get("name", "")
            if name:
                if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
                    self._json({"error": "文件名只允许 [A-Za-z0-9._-]"}, 400)
                    return
                doc = load_json(os.path.join(RUN_DIRS[kind], name))
                if doc is None:
                    self._json({"error": f"{name} 不存在或不是 JSON"}, 404)
                else:
                    self._json(doc)
            else:
                d = RUN_DIRS[kind]
                try:
                    files = sorted(({"name": f, "bytes": os.path.getsize(os.path.join(d, f)),
                                     "mtime": datetime.datetime.fromtimestamp(
                                         os.path.getmtime(os.path.join(d, f))).isoformat(timespec="seconds")}
                                    for f in os.listdir(d) if f.endswith(".json")),
                                   key=lambda x: x["name"], reverse=True)
                except FileNotFoundError:
                    files = []
                self._json({"kind": kind, "count": len(files), "files": files})
        else:
            self._json({"error": "未知端点", "endpoints": build_overview()["endpoints"]}, 404)

    def do_POST(self):
        if not self.path.startswith("/api/refresh"):
            self._json({"error": "not found"}, 404)
            return
        if not lock.acquire(blocking=False):
            self._json({"status": "busy", "detail": "上一轮流水线仍在运行"})
            return
        try:
            state["running"] = True
            state["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
            p = subprocess.run(PIPELINE, capture_output=True, text=True, timeout=600)
            tail = (p.stdout or "") + (p.stderr or "")
            state["last_result"] = {"code": p.returncode, "tail": tail[-1200:]}
            self._json({"status": "ok" if p.returncode == 0 else "fail",
                        "head": git_head(),
                        "detail": state["last_result"]["tail"][-400:]})
        except Exception as e:
            state["last_result"] = {"code": -1, "tail": str(e)}
            self._json({"status": "fail", "detail": str(e)})
        finally:
            state["running"] = False
            lock.release()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    Server(("0.0.0.0", 8080), Handler).serve_forever()
