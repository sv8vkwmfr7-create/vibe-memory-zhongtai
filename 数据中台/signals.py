#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据中台 · 异常信号检测（验收 5：异常有信号）

输入：assets/provenance.json、quality_report.json、dwd/test_runs.json、
      assets/service_heartbeat.json（keepalive_loop 每 10 秒写，服务失联自愈循环）
输出：assets/signals.json —— 每条信号 {level: ok|warn|bad, code, detail} + overall

规则（阈值与定时产出节奏匹配：评测/测试/监控每日一轮）：
  data_fresh_eval/test/obs   最新存档距现在 >48h → warn（>96h → bad）
  source_health              required 来源缺席 → bad；其他来源异常 → warn
  quality_gate               质量分 < 0.85 → warn
  test_gate                  最新一轮测试门禁 FAIL → bad
  service                    心跳缺失/距今 >5min → bad（8080/8081 失联已由 keepalive 自愈重启）

用法：python signals.py [--root 中台根目录]   （server_pipeline.sh 每轮流水线末尾调用）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
FRESH_WARN_H = 48
FRESH_BAD_H = 96


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def hours_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return max(0.0, (now() - dt).total_seconds() / 3600)
    except Exception:
        return None


def latest_mtime_hours(d: Path) -> float | None:
    try:
        mt = max(p.stat().st_mtime for p in d.glob("*.json"))
    except ValueError:
        return None
    return max(0.0, (now().timestamp() - mt) / 3600)


def sig(level: str, code: str, detail: str) -> dict:
    return {"level": level, "code": code, "detail": detail}


def fresh_signal(code: str, zh: str, hours: float | None) -> dict:
    if hours is None:
        return sig("warn", code, f"{zh}：尚无任何存档")
    if hours > FRESH_BAD_H:
        return sig("bad", code, f"{zh}：最新存档距今 {hours:.0f}h（>{FRESH_BAD_H}h，疑似停更）")
    if hours > FRESH_WARN_H:
        return sig("warn", code, f"{zh}：最新存档距今 {hours:.0f}h（>{FRESH_WARN_H}h）")
    return sig("ok", code, f"{zh}：最新存档距今 {hours:.1f}h")


def main() -> int:
    ap = argparse.ArgumentParser(description="数据中台异常信号检测")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    args = ap.parse_args()
    root = Path(args.root).resolve()
    plat = root / "数据中台"
    assets = plat / "assets"

    signals: list[dict] = []

    # 1) 数据停更（三中台 runs 目录）
    for d, code, zh in [("评测中台/runs", "data_fresh_eval", "评测存档"),
                        ("测试中台/runs", "data_fresh_test", "测试存档"),
                        ("监控中台/runs", "data_fresh_obs", "监控存档")]:
        signals.append(fresh_signal(code, zh, latest_mtime_hours(root / d)))

    # 2) 来源健康
    prov = load(assets / "provenance.json") or {}
    s = prov.get("summary", {})
    req_missing = s.get("required_missing") or []
    if req_missing:
        signals.append(sig("bad", "source_health", f"必需来源缺席: {req_missing}"))
    elif s.get("missing_or_error"):
        signals.append(sig("warn", "source_health",
                           f"{s.get('ok', '?')}/{s.get('total', '?')} 来源正常，"
                           f"{s.get('missing_or_error')} 个异常（非必需，降级容忍）"))
    else:
        signals.append(sig("ok", "source_health",
                           f"来源 {s.get('ok', '?')}/{s.get('total', '?')} 全部正常"))

    # 3) 质量门禁
    q = load(assets / "quality_report.json") or {}
    overall = (q.get("summary") or {}).get("overall")
    if overall is None:
        signals.append(sig("warn", "quality_gate", "质量报告缺失"))
    elif overall < 0.85:
        signals.append(sig("warn", "quality_gate", f"质量分 {overall:.3f} 低于阈值 0.85"))
    else:
        signals.append(sig("ok", "quality_gate", f"质量分 {overall:.3f}"))

    # 4) 测试门禁
    tr = load(assets / "dwd" / "test_runs.json") or {}
    latest_gate = (tr.get("latest") or {}).get("gate") or {}
    gstatus = latest_gate.get("status")
    if gstatus == "FAIL":
        signals.append(sig("bad", "test_gate",
                           f"最新回归门禁 FAIL：{latest_gate.get('detail', '')}"))
    elif gstatus in ("PASS", "NO_BASELINE"):
        signals.append(sig("ok", "test_gate", f"最新回归门禁 {gstatus}"))
    else:
        signals.append(sig("warn", "test_gate", f"最新回归门禁状态未知（{gstatus or '无存档'}）"))

    # 4.5) 新接入项目的活跃度信号（文档型/事件型无评测测试管道，新鲜度阈值放宽：14 天 warn / 28 天 bad）
    PROJ_WARN_H, PROJ_BAD_H = 336, 672

    def proj_fresh_signal(code: str, zh: str, hours: float | None) -> dict:
        if hours is None:
            return sig("warn", code, f"{zh}：尚无采集记录")
        if hours > PROJ_BAD_H:
            return sig("bad", code, f"{zh}：最新更新距今 {hours / 24:.0f} 天（>{PROJ_BAD_H // 24} 天）")
        if hours > PROJ_WARN_H:
            return sig("warn", code, f"{zh}：最新更新距今 {hours / 24:.0f} 天（>{PROJ_WARN_H // 24} 天）")
        return sig("ok", code, f"{zh}：最近更新距今 {hours:.0f}h")

    kb = load(assets / "ods" / "kb_docs.json") or {}
    kb_files = (kb.get("ods") or {}).get("files", [])
    if kb_files:
        import datetime as _dt
        newest = max((_dt.datetime.fromisoformat(f["mtime"]).timestamp()
                      for f in kb_files if f.get("mtime")), default=None)
        if newest:
            signals.append(proj_fresh_signal("proj_fresh_kb", "知识库文档",
                                             max(0.0, (now().timestamp() - newest) / 3600)))
        else:
            signals.append(sig("warn", "proj_fresh_kb", "知识库文档：无 mtime"))
    else:
        signals.append(sig("warn", "proj_fresh_kb", "知识库文档：ODS 缺失"))
    prov_by_id = {e.get("id"): e for e in prov.get("sources", [])}
    signals.append(proj_fresh_signal("proj_fresh_cz", "codex 事件流",
                                     hours_since(prov_by_id.get("cz_events", {}).get("mtime"))))

    # 5) 服务心跳（keepalive_loop 每 10s 写；缺失/过期=大屏或档案服务失联，自愈已介入）
    hb = load(assets / "service_heartbeat.json") or {}
    hb_age = hours_since(hb.get("ts"))
    if hb_age is None:
        signals.append(sig("warn", "service", "服务心跳文件缺失（keepalive 未运行或未升级）"))
    elif hb_age > 5 / 60:
        signals.append(sig("bad", "service", f"服务心跳停滞 {hb_age * 60:.0f} 分钟（自愈循环应已重启）"))
    else:
        downs = hb.get("down") or []
        signals.append(sig("ok" if not downs else "warn", "service",
                           f"服务心跳正常（{hb_age * 60:.0f}s 前）"
                           + (f"；近期失联已自愈: {downs}" if downs else "")))

    order = {"bad": 0, "warn": 1, "ok": 2}
    overall = "ok"
    for x in signals:
        if order[x["level"]] < order[overall]:
            overall = x["level"]
    doc = {"generated_at": now().isoformat(timespec="seconds"),
           "overall": overall, "signals": signals,
           "note": "阈值对齐每日定时产出节奏；服务失联由 keepalive_loop 自愈重启并记 signals.log"}
    (assets / "signals.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[signals] 总体={overall}；{len(signals)} 条信号 -> assets/signals.json")
    for x in signals:
        if x["level"] != "ok":
            print(f"[signals]   [{x['level']}] {x['code']}: {x['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
