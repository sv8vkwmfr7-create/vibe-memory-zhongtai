#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""可视化页面生成器（数据服务/应用层）

从 数据中台/assets 与 评测中台/runs 读取最新资产与运行存档，
注入 HTML 模板生成 pages/ 下五张静态大屏：
    index.html            门户
    data-platform.html    数据中台
    eval-platform.html    评测中台
    test-platform.html    测试中台
    ops-platform.html     监控中台

特点：数据内嵌、零外部依赖（自带 SVG 图表库）、双击离线可开。
用法：python scripts/build_pages.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TPL = HERE / "templates"
PAGES = ROOT / "pages"

EVALSET_META = {
    "basic_recall": ("单跳事实召回", "会话事实直接提问，考 BM25/向量/图基础召回与同词区分"),
    "temporal": ("时序推理", "时间锚点事实（最新/最早/本季度），考时序检索与状态选择"),
    "conflict_update": ("知识更新", "更正/推翻旧结论，考修正后的新值优先"),
    "multi_hop": ("多跳图推理", "跨会话因果链溯因/推演，默认单跳配置"),
    "multi_hop_bridge": ("多跳·显式建边对照", "与 multi_hop 同题，显式因果边+causal_bridge，验证图链路价值"),
    "preference": ("用户偏好记忆", "语言/风格/禁忌/习惯偏好召回"),
    "abstention": ("拒答（负面）", "主体相关但属性未知的提问，考不编造（规则代理判分）"),
    "recall_basic_large": ("单跳召回·大样本", "生成评测集 v2：240 例，(环境,服务,属性)组合唯一+近邻干扰"),
    "temporal_large": ("时序·大样本", "生成评测集 v2：140 例，(服务,属性)唯一，时序状态选择"),
    "conflict_update_large": ("知识更新·大样本", "生成评测集 v2：140 例，(项目,月份,组件)锚点唯一"),
    "multi_hop_large": ("多跳·大样本", "生成评测集 v2：180 例，唯一(事故,后果)组合+显式因果边+causal_bridge"),
    "preference_large": ("偏好·大样本", "生成评测集 v2：90 例，(人,场景)唯一锚点"),
    "abstention_large": ("拒答·大样本", "生成评测集 v2：120 例，主体在库但属性未存入"),
    "recall_basic_en_large": ("英文召回·大样本", "生成评测集 v2：60 例，(服务,属性)唯一锚点"),
}

GEN_PREFIX_ZH = {"GB": "单跳召回·大样本", "GT": "时序·大样本", "GC": "知识更新·大样本",
                 "GM": "多跳·大样本", "GP": "偏好·大样本", "GA": "拒答·大样本", "GE": "英文召回·大样本"}


def meta_for(name: str) -> tuple[str, str]:
    if name in EVALSET_META:
        return EVALSET_META[name]
    if name.startswith("locomo_"):
        return ("LoCoMo 公开基准", "长对话记忆基准（会话级证据协议，逐对话隔离运行）")
    if name.startswith("longmemeval"):
        return ("LongMemEval-S 公开基准", "长程记忆基准子集（逐题独立记忆库协议）")
    prefix = name[:2]
    if prefix in GEN_PREFIX_ZH:
        return (GEN_PREFIX_ZH[prefix], "生成评测集（固定种子可复现）")
    return (name, "评测集")


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def jdump(data) -> str:
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def fmt_pct(v) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def short_dt(s) -> str:
    """ISO 时间 → 'MM-DD HH:MM'（无年份、无 T 分隔符）。"""
    return str(s or "")[5:16].replace("T", " ")


def fmt_val(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".") if v < 1 else f"{v:,.2f}".rstrip("0").rstrip(".")
    return str(v)


NAV = [("index", "index.html", "门户"),
       ("data", "data-platform.html", "数据中台"),
       ("eval", "eval-platform.html", "评测中台"),
       ("test", "test-platform.html", "测试中台"),
       ("ops", "ops-platform.html", "监控中台")]

# 内联 SVG 图标（stroke 风格，currentColor 继承主题色）
ICONS = {
    "database": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"/><path d="M4 11.5v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"/></svg>',
    "target": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.8"/><circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/></svg>',
    "flask": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3v6.5L4.6 18a2 2 0 0 0 1.7 3h11.4a2 2 0 0 0 1.7-3L14 9.5V3"/><path d="M8.5 3h7"/><path d="M7.6 14h8.8"/></svg>',
    "pulse": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2.5-7 4 14 2.5-7h5"/></svg>',
    "shield": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 3v6c0 4.5-3.2 7.7-8 9-4.8-1.3-8-4.5-8-9V6z"/><path d="m9 12 2 2 4-4"/></svg>',
    "layers": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="m12 3 9 5-9 5-9-5z"/><path d="m3.6 12.6 8.4 4.7 8.4-4.7"/><path d="m3.6 16.8 8.4 4.7 8.4-4.7" opacity="0.55"/></svg>',
    "clock": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></svg>',
    "check": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.2 2.4 2.4 4.8-5"/></svg>',
    "gauge": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 19a9 9 0 1 1 14 0"/><path d="M12 13l4.2-4.2"/><circle cx="12" cy="14" r="1.6" fill="currentColor" stroke="none"/></svg>',
    "logo": '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round"><path d="m12 3 9 5-9 5-9-5z"/><path d="m4.6 12.6 7.4 4.1 7.4-4.1"/><path d="m4.6 16.6 7.4 4.1 7.4-4.1" opacity="0.55"/></svg>',
}


def icon(name: str) -> str:
    return ICONS.get(name, "")


def page_shell(title: str, sub: str, body: str, generated_at: str, page_key: str = "") -> str:
    css = TPL.joinpath("shared.css").read_text(encoding="utf-8")
    js_charts = TPL.joinpath("charts.js").read_text(encoding="utf-8")
    js_app = TPL.joinpath("app.js").read_text(encoding="utf-8")
    nav_parts = []
    for key, href, label in NAV:
        cls = ' class="active"' if key == page_key else ""
        nav_parts.append(f'<a href="{href}"{cls}>{label}</a>')
    nav = "".join(nav_parts)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{css}</style>
</head>
<body data-page="{escape(page_key or 'index')}">
<header class="topbar">
  <a class="brand" href="index.html"><span class="logo">{icon('logo')}</span><span class="brand-t">VibeMemory <b>四中台</b></span></a>
  <nav class="nav">{nav}</nav>
  <div class="spacer"></div>
  <span id="status-chip" class="status-chip" style="display:none"></span>
  <button id="refresh-btn" class="refresh-btn" style="display:none">⟳ 刷新数据</button>
</header>
<main class="wrap">
  <div class="page-head">
    <div>
      <h1>{escape(title.split('·')[-1].strip() if '·' in title else title)}</h1>
      <div class="sub">{escape(sub)}</div>
    </div>
    <div class="meta">生成时间：{escape(generated_at)}<br>数据内嵌 · 离线可开</div>
  </div>
  {body}
  <div class="footer">VibeMemory 数据中台 + 评测/测试/监控中台 · 方法论对齐信通院数据中台实践指南 / OneData / OpenCompass 式评测三层抽象 ·
  资料见 docs/ 目录 · 重新生成：<code>python scripts/build_pages.py</code></div>
</main>
<script>{js_charts}</script>
<script>{js_app}</script>
<script>
window.DATA = __DATA__;
__PAGEJS__
</script>
</body>
</html>"""


def kpi(label: str, value: str, hint: str = "", cls: str = "", ic: str = "", target: str = "") -> str:
    ico = f'<div class="ico">{icon(ic)}</div>' if ic else ""
    tgt = f' data-target="{escape(target)}" title="点击跳转到对应面板"' if target else ""
    return (f'<div class="kpi {cls}"{tgt}>{ico}<div class="body"><div class="label">{escape(label)}</div>'
            f'<div class="value">{value}</div>'
            + (f'<div class="hint">{escape(hint)}</div>' if hint else "") + "</div></div>")


def detail_attr(d: dict) -> str:
    """行明细 JSON → 安全的 HTML 属性（配合 app.js 行展开交互）。"""
    return escape(json.dumps(d, ensure_ascii=False), quote=True)


# ---------------------------------------------------------------- 数据中台页

def render_paired_evaluations(records: list[dict]) -> str:
    panels = []
    for record in records:
        pair = record.get("paired_evaluation")
        if not pair:
            continue
        diffs = pair["differences"]
        on_only = sum(row["on"]["hit"] for row in diffs)
        off_only = len(diffs) - on_only
        both = pair["on_hits"] - on_only
        neither = pair["questions"] - both - len(diffs)
        details = "".join(
            f"<details class='pair-case'><summary><code>{escape(row['id'])}</code> <span>{'仅开图命中' if row['on']['hit'] else '仅关图命中'}</span></summary>"
            f"<p>官方证据 ID · {escape(', '.join(row['gold']))}</p>"
            + "".join(
                f"<p>{label}排名 · "
                + escape(', '.join(f"{rank}. {atom}" for rank, atom in enumerate(row[arm]["ids"], 1)))
                + "</p>" for arm, label in (("on", "开图"), ("off", "关图"))
            )
            + "</details>" for row in diffs
        )
        scope = "0 条边，仅验证对照流程" if pair["edges"] == 0 else f"{pair['edges']} 条边"
        stats = "".join(
            f"<div><dt>{label}</dt><dd>{value:,}</dd></div>"
            for label, value in (("评测题数", pair["questions"]), ("开图命中", pair["on_hits"]),
                                 ("关图命中", pair["off_hits"]), ("结果有差异", len(diffs)))
        )
        panels.append(
            f"<article class='pair-run'><header><code>{escape(record['file'])}</code>"
            f"<span>{scope}</span></header><dl class='pair-stats'>{stats}</dl>"
            f"<p class='pair-outcomes'>仅开图 {on_only} · 仅关图 {off_only} · 均命中 {both:,} · 均漏召回 {neither:,}</p>"
            f"{details}<details class='pair-meta'><summary>版本、配置与评测范围</summary>"
            f"<p>SDK 提交 <code>{escape(pair['commit'])}</code></p>"
            f"<p>数据 SHA-256 <code>{escape(pair['source_sha256'])}</code></p>"
            f"<p>{escape(pair['backend'])} / {escape(pair['mode'])} / Top-{pair['top_k']}</p>"
            f"<p>{escape(pair['limits'])}</p></details></article>"
        )
    if not panels:
        return ""
    return '<section class="panel pair-panel"><h2>图开关配对评测</h2>' + "".join(panels) + "</section>"


def build_data_page() -> str:
    assets_dir = ROOT / "数据中台" / "assets"
    catalog = load(assets_dir / "catalog.json") or {}
    prov = load(assets_dir / "provenance.json") or {}
    quality = load(assets_dir / "quality_report.json") or {}
    health = load(assets_dir / "dws" / "repo_health.json") or {}
    exp_metrics = load(assets_dir / "dws" / "experiment_metrics.json") or {}
    experiments = load(assets_dir / "dwd" / "experiments.json") or {}
    paired_panel = render_paired_evaluations(experiments.get("records", []))
    cards_reg = load(assets_dir.parent / "dataset_cards.json") or {}

    # 数据集卡（M0 身份证）：哪来的 / 哪个版本 / 能不能用
    def license_badge(lic: dict) -> str:
        spdx = (lic or {}).get("spdx")
        note = (lic or {}).get("note") or ""
        if spdx:
            return f"<span class='badge ok'>{escape(spdx)}</span>"
        if "未核实" in note:
            return "<span class='badge bad'>未核实</span>"
        return "<span class='badge warn'>混合</span>"

    cons_cls = {"non-commercial": "warn", "unverified-license": "bad",
                "derived-content": "info", "share-alike": "warn"}
    cons_short = {"non-commercial": "NC 禁商用", "unverified-license": "许可证未核实",
                  "derived-content": "派生内容", "share-alike": "同牌共享"}
    card_rows = ""
    for c in cards_reg.get("cards", []):
        cons = " ".join(f"<span class='badge {cons_cls.get(x, 'dim')}' title='{escape(x)}'>"
                        f"{escape(cons_short.get(x, x))}</span>"
                        for x in (c.get("constraints") or [])) or "<span class='badge dim'>无</span>"
        card_detail = {"名称": c.get("name", ""), "产地/生成机制": c.get("origin", ""),
                       "版本锚点": c.get("version_anchor", ""),
                       "许可证": (c.get("license") or {}).get("spdx") or (c.get("license") or {}).get("note") or "-",
                       "约束": "、".join(c.get("constraints") or []) or "无",
                       "消费方": "、".join(c.get("consumers") or []) or "-"}
        card_rows += (f"<tr data-detail='{detail_attr(card_detail)}'><td><span class='badge info'>{escape(c.get('category', ''))}</span> "
                      f"<span class='mono'>{escape(c.get('id', ''))}</span>"
                      f"<div class='desc'>{escape(c.get('name', ''))}</div></td>"
                      f"<td>{escape(c.get('origin', ''))}</td>"
                      f"<td class='mono' style='font-size:11px'>{escape(c.get('version_anchor', ''))}</td>"
                      f"<td>{license_badge(c.get('license'))}</td><td class='nowrap'>{cons}</td>"
                      f"<td class='num'>{len(c.get('consumers') or [])} 处</td></tr>")

    sources = catalog.get("sources", [])
    src_ok = sum(1 for s in sources if s.get("status") == "ok")
    assets = catalog.get("assets", [])
    total_rows = sum(a.get("rows") or 0 for a in assets)
    qsum = quality.get("summary", {})
    gen_at = (catalog.get("generated_at") or "")[:19].replace("T", " ")

    # 来源卡（多项目：project 徽标 + 项目数统计）
    proj_names = {p.get("project_id"): p.get("name") for p in (prov.get("projects") or [])}
    n_projects = len(proj_names) if proj_names else len({s.get("project", "vibememory") for s in sources})
    src_rows = ""
    for s in sources:
        st = s.get("status", "?")
        badge = '<span class="badge ok">正常</span>' if st == "ok" else f'<span class="badge bad">{escape(st)}</span>'
        changed = s.get("changed_since_last")
        ch = "" if changed is None else ('<span class="badge warn">已变更</span>' if changed else '<span class="badge dim">未变更</span>')
        sha = s.get("sha256") or ""
        rec = s.get("records")
        if rec is not None:
            rec_s = f"<b>{rec}</b> <span class='sub-num'>文件</span>"
        elif s.get("bytes"):
            rec_s = f"<b>{(s.get('bytes') or 0) / 1024:.0f}</b> <span class='sub-num'>KB</span>"
        else:
            rec_s = "-"
        pid = s.get("project", "vibememory")
        proj_badge = f"<span class='badge dim' title='{escape(proj_names.get(pid, pid))}'>{escape(pid)}</span>" if n_projects > 1 else ""
        full_path = s.get("path", "")
        # 路径只显示末两段（全路径放悬浮提示），避免 Windows 绝对路径爆宽
        path_parts = full_path.replace("\\", "/").rstrip("/").split("/")
        short_path = "/".join(path_parts[-2:]) if len(path_parts) >= 2 else full_path
        src_detail = {"完整路径": full_path or "-", "SHA-256": sha or "-",
                      "项目": proj_names.get(pid, pid), "类型": s.get("kind", ""),
                      "规模": (f"{rec} 个文件" if rec is not None else (f"{(s.get('bytes') or 0)/1024:.0f} KB" if s.get("bytes") else "-")),
                      "采集时间": s.get("collected_at", "")}
        src_rows += (f"<tr data-detail='{detail_attr(src_detail)}'><td class='mono'>{escape(s.get('id',''))}</td>"
                     f"<td class='nowrap'>{proj_badge}</td>"
                     f"<td><span class='badge info'>{escape(s.get('kind',''))}</span></td>"
                     f"<td class='mono' title='{escape(full_path)}'><div class='ellipsis' style='max-width:150px;font-size:11px'>{escape(short_path)}</div></td>"
                     f"<td class='num'>{rec_s}</td>"
                     f"<td class='mono' title='{escape(sha)}' style='font-size:11px'>{escape(sha[:10] + '…') if sha else '-'}</td>"
                     f"<td class='nowrap'>{badge} {ch}</td>"
                     f"<td class='mono' style='font-size:11px'>{escape(short_dt(s.get('collected_at')))}</td></tr>")

    # 资产表
    asset_rows = ""
    for a in assets:
        q = a.get("quality") or {}
        qo = q.get("overall")
        qbadge = ""
        if qo is not None:
            cls = "ok" if qo >= 0.9 else ("warn" if qo >= 0.75 else "bad")
            qbadge = f"<span class='badge {cls}'>{qo:.3f}</span>"
        rows_v = a.get("rows", "")
        rows_s = f"{rows_v:,}" if isinstance(rows_v, int) else str(rows_v)
        asset_detail = {"层级": a.get("layer", ""), "资产 ID": a.get("id", ""),
                        "说明": a.get("desc", ""), "路径": a.get("path", ""),
                        "行数": rows_s, "上游血缘": " ← ".join(a.get("lineage", {}).get("upstream", [])) or "-",
                        "质量分": f"{qo:.3f}" if qo is not None else "-"}
        asset_rows += (f"<tr data-detail='{detail_attr(asset_detail)}'><td><span class='badge dim'>{escape(a.get('layer',''))}</span> <span class='mono'>{escape(a.get('id',''))}</span></td>"
                       f"<td>{escape(a.get('desc',''))}</td><td class='num'>{rows_s}</td>"
                       f"<td class='mono' style='font-size:11px'>{escape(a.get('path',''))}</td>"
                       f"<td>{qbadge}</td><td class='mono' style='font-size:11px'>{' ← '.join(a.get('lineage', {}).get('upstream', []))}</td></tr>")

    # 质量维度条
    dims = qsum.get("dimensions", {})
    zh = {"completeness": "完整性", "uniqueness": "唯一性", "consistency": "一致性",
          "accuracy": "准确性", "timeliness": "及时性", "validity": "有效性"}
    dim_rows = ""
    for k in ["completeness", "uniqueness", "consistency", "accuracy", "timeliness", "validity"]:
        v = dims.get(k, 0)
        dim_rows += (f"<tr><td>{zh[k]}</td><td style='width:60%'><div class='progress'><i style='width:{v*100:.0f}%'></i></div></td>"
                     f"<td class='num'>{v:.3f}</td></tr>")

    # 实验指标
    em_rows = ""
    for m in exp_metrics.get("metrics", []):
        em_rows += (f"<tr><td>{escape(m.get('metric',''))}</td><td class='num'>{fmt_val(m.get('value'))}"
                    f"<span class='badge dim' style='margin-left:6px'>{escape(m.get('unit') or '')}</span></td>"
                    f"<td class='mono' style='font-size:11px'>{escape(m.get('experiment_file',''))}</td>"
                    f"<td class='mono' style='font-size:11px'>{escape((m.get('mtime') or '')[:10])}</td></tr>")

    # 覆盖率进度
    cov_l = health.get("coverage_line_rate")
    cov_b = health.get("coverage_branch_rate")

    body = f"""
  <div class="kpis">
    {kpi('可信数据来源', f'{src_ok}<span class="unit">/ {len(sources)}</span>', '采集校验全部通过' if src_ok == len(sources) else '存在缺席来源', 'good' if src_ok == len(sources) else 'bad', 'shield', 'p-srcs')}
    {kpi('数据资产数', str(len(assets)), 'ODS / DWD / DWS 三层', '', 'layers', 'p-assets')}
    {kpi('资产记录总量', f'{total_rows:,}', '明细与汇总条目', '', 'database', 'p-chart')}
    {kpi('数据质量综合分', f'{qsum.get("overall", 0):.3f}', f'阈值 {qsum.get("threshold_warn", 0.85)} · {qsum.get("status", "")}', 'good' if qsum.get("status") == 'PASS' else 'warn', 'gauge', 'p-quality')}
    {kpi('最近采集', escape(short_dt(gen_at) or '-'), 'collect.py 每次比对指纹', '', 'clock')}
  </div>

  {paired_panel}

  <div class="panel">
    <h2>数据汇聚 · 全链路血缘 <span class="tag">六层架构：汇聚 → 存储 → 开发 → 资产治理 → 服务 → 应用</span></h2>
    <div class="flow">
      <div class="stage"><h3>① 可信来源（9 类）</h3><ul>
        <li>results/*.json 实验存档</li><li>coverage.xml 覆盖率</li><li>STATUS / CHANGELOG</li>
        <li>seed_memory · tests · git · pyproject</li><li>评测中台 runs（回流）</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>② 采集 collect.py</h3><ul><li>存在性校验</li><li>SHA-256 指纹</li>
        <li>记录数 / 大小 / mtime</li><li>变更检测</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>③ ODS 原始层</h3><ul><li>结构化快照</li><li>大文件标量摘要</li><li>溯源档案 provenance</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>④ DWD 明细层</h3><ul><li>清洗 / 规范化</li><li>统一记录模型</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>⑤ DWS 汇总层</h3><ul><li>仓库健康主题</li><li>核心实验指标</li><li>评测结果主题</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>⑥ 数据应用</h3><ul><li>本页大屏</li><li>评测中台门禁</li></ul></div>
    </div>
  </div>

  <div class="grid g21">
    <div class="panel">
      <h2>可信数据来源清单 <span id="p-srcs" class="tag">指纹可追溯 · 变更可检测 · {n_projects} 个项目接入</span></h2>
      <div class="tbl-wrap"><table class="tbl" data-enhance><thead><tr><th>来源 ID</th><th>项目</th><th>类型</th><th>路径</th><th>规模</th><th>SHA-256</th><th>状态</th><th>采集时间</th></tr></thead>
      <tbody>{src_rows}</tbody></table></div>
    </div>
    <div>
      <div class="panel">
        <h2>数据质量六性 <span id="p-quality" class="tag">{escape(quality.get('standard',''))}</span></h2>
        <table class="tbl"><tbody>{dim_rows}</tbody></table>
      </div>
      <div class="panel">
        <h2>仓库健康 <span class="tag">DWS 主题</span></h2>
        <table class="tbl"><tbody>
          <tr><td>版本</td><td class="mono">{escape(str(health.get('version','')))} <span class="badge info">Python {escape(str(health.get('python_required','')))}</span></td></tr>
          <tr><td>回归测试</td><td class="num">{health.get('test_files','?')} 个文件 / {health.get('test_funcs','?')} 个用例</td></tr>
          <tr><td>行覆盖率</td><td><div class="progress"><i style="width:{(cov_l or 0)*100:.0f}%"></i></div><span class="num" style="font-size:11px">{fmt_pct(cov_l)}</span></td></tr>
          <tr><td>分支覆盖率</td><td><div class="progress"><i style="width:{(cov_b or 0)*100:.0f}%"></i></div><span class="num" style="font-size:11px">{fmt_pct(cov_b)}</span></td></tr>
          <tr><td>提交活动</td><td class="num">近 {health.get('recent_commits','?')} 次 · 最新 {escape(str(health.get('last_commit_at',''))[:16])}</td></tr>
        </tbody></table>
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>数据集卡（来源身份证） <span class="tag">M0 · 对齐 Datasheets for Datasets 精简版 · {len(cards_reg.get('cards', []))} 张</span></h2>
    <div class="desc">每类来源回答三问：<b>哪来的</b>（origin）、<b>哪个版本</b>（版本锚点）、<b>能不能用</b>（许可证+约束）。SHA-256 指纹与变更检测见 collect.py（provenance.json）。</div>
    <div class="tbl-wrap"><table class="tbl" data-enhance><thead><tr><th>来源</th><th>产地与生成机制</th><th>版本锚点</th><th>许可证</th><th>约束</th><th>消费方</th></tr></thead>
    <tbody>{card_rows}</tbody></table></div>
    <div class="desc" style="margin-top:8px">完整卡内容（schema、已知局限、消费方明细）见 <code>数据中台/dataset_cards.json</code>。</div>
  </div>

  <div class="panel">
    <h2>数据资产目录 <span id="p-assets" class="tag">含血缘 lineage 与质量分 · catalog.json</span></h2>
    <div class="tbl-wrap"><table class="tbl" data-enhance><thead><tr><th>资产</th><th>说明</th><th>行数</th><th>路径</th><th>质量</th><th>上游血缘</th></tr></thead>
    <tbody>{asset_rows}</tbody></table></div>
  </div>

  <div class="panel">
    <h2>核心实验指标主题 <span class="tag">DWS · {exp_metrics.get('metric_count', 0)} 条 · 来源文件级溯源</span></h2>
    <div class="tbl-wrap"><table class="tbl" data-enhance><thead><tr><th>指标</th><th>数值</th><th>来源实验文件</th><th>数据日期</th></tr></thead>
    <tbody>{em_rows}</tbody></table></div>
    <details class="tblwrap"><summary>查看全部 168 个实验结果文件索引（ODS 层）</summary>
      <div class="desc" style="margin-top:8px">完整清单见 <code>数据中台/assets/ods/results_experiments.json</code>，每文件含 SHA-256 指纹、大小、mtime 与标量摘要。</div>
    </details>
  </div>

  <div class="grid g2">
    <div class="panel" id="p-chart"><h2>资产记录规模 <span class="tag">逐资产行数 · 三层一览</span></h2>
      <div id="chart-assets" class="chart-box"></div></div>
    <div class="panel"><h2>覆盖率与测试规模</h2>
      <div id="chart-coverage" class="chart-box"></div>
      <div class="desc" style="margin-top:8px">覆盖率来自 coverage.xml（cobertura 解析），测试规模来自 tests/ 文件索引。</div>
    </div>
  </div>
"""

    page_js = """
(function(){
  var assets = DATA.catalog.assets.slice().sort(function(a,b){ return (b.rows||0)-(a.rows||0); });
  VMCharts.bar(document.getElementById('chart-assets'), assets.map(function(a){
    return {name: a.id, value: a.rows || 0};
  }), {labelW: 150, rowH: 21, fmt: function(v){ return v.toLocaleString(); }});
  var H = DATA.repo_health;
  VMCharts.bar(document.getElementById('chart-coverage'), [
    {name:'行覆盖率', value:H.coverage_line_rate, max:1, color:'#2563eb'},
    {name:'分支覆盖率', value:H.coverage_branch_rate, max:1, color:'#60a5fa'},
    {name:'测试文件/112', value:H.test_files/112, max:1, color:'#93c5fd'}
  ], {fmt:function(v){return (v*100).toFixed(1)+'%';}, max:1});
})();
"""
    return page_shell(
        "VibeMemory · 数据中台",
        "数据汇聚 → 分层加工 → 质量治理 → 资产目录 → 统一出口",
        body, gen_at, page_key="data",
    ).replace("__DATA__", jdump({
        "quality_summary": quality.get("summary", {}),
        "repo_health": health,
        "catalog": {"sources": sources, "assets": assets},
    })).replace("__PAGEJS__", page_js)


# ---------------------------------------------------------------- 评测中台页

def build_eval_page() -> str:
    runs_dir = ROOT / "评测中台" / "runs"
    runs = []
    for p in sorted(runs_dir.glob("run-*.json")):
        d = load(p)
        if d:
            runs.append(d)
    runs.sort(key=lambda r: r.get("created_at") or "")
    baseline = load(ROOT / "评测中台" / "baseline.json") or {}
    gate_report = load(ROOT / "评测中台" / "gate_report.json") or {}
    dwd_evalsets = load(ROOT / "数据中台" / "assets" / "dwd" / "evalsets.json") or {}
    dwd_datasets = load(ROOT / "数据中台" / "assets" / "dwd" / "datasets.json") or {}

    # 答案级评测（LLM-judge）：读最新的 ans-*.json 存档
    ans_latest = None
    ans_files = sorted((HERE.parent / "评测中台" / "runs").glob("ans-*.json"))
    if ans_files:
        ans_latest = load(ans_files[-1])

    # 每个评测集取最新运行
    latest: dict[str, dict] = {}
    for r in runs:
        latest[r.get("evalset_name") or "?"] = r
    case_counts = {}
    for d in [ROOT / "评测中台" / "evalsets", ROOT / "评测中台" / "benchmarks"]:
        for p in d.glob("*.jsonl"):
            case_counts[p.stem] = sum(1 for ln in p.read_text(encoding="utf-8-sig").splitlines() if ln.strip())

    total_items = sum(r.get("items_total") or 0 for r in latest.values())
    primary_vals = [(n, r.get("metrics", {}).get("primary_value")) for n, r in latest.items()
                    if r.get("metrics", {}).get("primary_value") is not None]
    avg_primary = (sum(v for _, v in primary_vals) / len(primary_vals)) if primary_vals else None
    gate_status = gate_report.get("status", "未运行")

    # 证据力：从完整逐题明细计算 Wilson 95% CI（旧存档无 metrics.ci 字段，此处统一补算）
    def wilson(k: int, n: int, z: float = 1.96):
        if n <= 0:
            return None
        p = k / n
        d = 1 + z * z / n
        c = p + z * z / (2 * n)
        r_ = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5
        return [round(max(0.0, (c - r_) / d), 3), round(min(1.0, (c + r_) / d), 3)]

    ci_map: dict[str, dict] = {}
    for name, r in latest.items():
        rows = r.get("items") or []
        n_hit = sum(1 for x in rows if x.get("hit_at_k") is not None)
        k_hit = sum(1 for x in rows if x.get("hit_at_k"))
        n_abs = sum(1 for x in rows if x.get("abstain_ok") is not None)
        k_abs = sum(1 for x in rows if x.get("abstain_ok"))
        ci_map[name] = {"recall": wilson(k_hit, n_hit), "abst": wilson(k_abs, n_abs)}

    def light(r: dict) -> dict:
        """页面嵌入用轻量投影：剔除逐题明细（items），只留汇总。"""
        env = r.get("environment") or {}
        return {"run_id": r.get("run_id"), "created_at": r.get("created_at"),
                "evalset_name": r.get("evalset_name"), "k": r.get("k"), "mode": r.get("mode"),
                "items_total": r.get("items_total"), "metrics": r.get("metrics"),
                "per_type": r.get("per_type"), "duration_ms": r.get("duration_ms"),
                "note": r.get("note"), "gate": r.get("gate"),
                "environment": {"protocol": env.get("protocol"),
                                 "shared_atoms_total": env.get("shared_atoms_total"),
                                 "isolated_items": env.get("isolated_items")}}

    # 评测集卡片
    cards = ""
    for name, r in sorted(latest.items()):
        zh, desc = meta_for(name)
        m = r.get("metrics", {})
        pv = m.get("primary_value")
        cls = "ok" if (pv or 0) >= 0.99 else ("warn" if (pv or 0) >= 0.8 else "bad")
        metric_label = "AbstentionPrecision" if m.get("primary") == "abstention_precision" else "Recall@K"
        ci = (ci_map.get(name) or {}).get("abst" if metric_label == "AbstentionPrecision" else "recall")
        ci_s = f"CI [{ci[0]:.2f},{ci[1]:.2f}]" if ci else ""
        ci_cls = "ok" if (ci and ci[1] - ci[0] <= 0.05) else ("warn" if ci else "dim")
        created = (r.get("created_at") or "")
        created_s = escape(short_dt(created))
        per_type = r.get("per_type") or {}
        board_detail = {"主指标": f"{metric_label} {fmt_pct(pv)}", "MRR": fmt_pct(m.get("mrr")),
                        "nDCG@K": fmt_pct(m.get("ndcg_at_k")), "KeywordHit": fmt_pct(m.get("keyword_hit")),
                        "95%CI": ci_s or "区间未知", "协议": r.get("mode", "") or "-",
                        "Top-K": str(r.get("k", "-")), "运行 ID": r.get("run_id", "")}
        board_detail.update({f"分型 {k}": (f"recall {fmt_pct((v or {}).get('recall_at_k'))} · n={(v or {}).get('n', '-')}"
                                           if isinstance(v, dict) else str(v))
                             for k, v in per_type.items()})
        cards += (f"<tr data-detail='{detail_attr(board_detail)}'><td><span class='badge info'>{escape(zh)}</span> <span class='mono'>{escape(name)}</span>"
                  f"<div class='desc'>{escape(desc)}</div></td>"
                  f"<td class='num'>{case_counts.get(name, r.get('items_total', '-'))}</td>"
                  f"<td class='num'><b>{fmt_pct(pv)}</b> <span class='sub-num'>{metric_label}</span>"
                  f"<div class='desc'><span class='badge {ci_cls}'>{ci_s or '区间未知'}</span></div></td>"
                  f"<td class='num'>{fmt_pct(m.get('mrr'))}</td><td class='num'>{fmt_pct(m.get('ndcg_at_k'))}</td>"
                  f"<td class='num'>{fmt_pct(m.get('keyword_hit'))}</td>"
                  f"<td class='mono' style='font-size:11px'>{created_s}</td></tr>")

    # 热力格：评测集 × 指标
    heat_rows = []
    metric_names = [("recall_at_k", "Recall@K"), ("mrr", "MRR"), ("ndcg_at_k", "nDCG@K"),
                    ("keyword_hit", "KeywordHit"), ("abstention_precision", "Abstention")]
    for name, r in sorted(latest.items()):
        m = r.get("metrics", {})
        cells = []
        for mk, _ in metric_names:
            v = m.get(mk)
            cells.append({"v": (v if v is not None else 0),
                          "label": mk, "text": "" if v is None else f"{v:.2f}"[1:]})
        heat_rows.append({"name": name, "cells": cells})

    # multi_hop 对照：限定小规模记忆库轮次（≤100 atoms）。
    # 兼容两代环境字段：新存档 shared_atoms_total，早期存档 atoms_total；
    # 两者都缺失的运行视为"规模未知"并排除（不同池子规模的指标不可比）。
    def pool_size(r: dict):
        env = r.get("environment") or {}
        if env.get("shared_atoms_total") is not None:
            return env["shared_atoms_total"]
        if env.get("atoms_total") is not None:
            return env["atoms_total"]
        return None

    mh_runs = [r for r in runs if r.get("evalset_name") == "multi_hop"
               and (r.get("metrics") or {}).get("recall_at_k") is not None]
    mh_small = [r for r in mh_runs
                if pool_size(r) is not None and pool_size(r) <= 100]
    mh_default = (mh_small[0].get("metrics", {}).get("recall_at_k")) if mh_small else None
    mh_bridge = (mh_small[-1].get("metrics", {}).get("recall_at_k")) if mh_small else None

    # 大池消融对照：multi_hop_large 带边（最新常规运行） vs --no-edges 消融运行
    mh_large_with = (latest.get("multi_hop_large") or {}).get("metrics", {}).get("recall_at_k")
    mh_large_without = None
    for r in reversed(runs):
        if (r.get("evalset_name") == "multi_hop_large"
                and (r.get("environment") or {}).get("edges_disabled")):
            mh_large_without = (r.get("metrics") or {}).get("recall_at_k")
            break

    # 历史趋势（每评测集 recall_at_k 序列；只画全程有值的评测集，避免把缺席轮画成 0）
    hist_sets: dict[str, list] = {}
    for r in runs:
        v = (r.get("metrics") or {}).get("recall_at_k")
        if v is not None:
            hist_sets.setdefault(r.get("evalset_name"), []).append(v)
    n_rounds = max((len(v) for v in hist_sets.values()), default=0)
    hist_sets = {k: v for k, v in hist_sets.items() if len(v) == n_rounds}

    # 行式趋势卡：每行 = 迷你走势 + 最新分 + 状态评语（直观读"稳不稳、掉没掉"）
    def _pct(v):
        return f"{v * 100:.0f}%"

    hist_rows = ""
    spark_series = {}
    for name in sorted(hist_sets):
        vals = hist_sets[name]
        latest_v, best_v = vals[-1], max(vals)
        first_drop = next((i for i, v in enumerate(vals) if v < best_v - 0.001), None)
        if first_drop is None:
            if vals[0] < best_v - 0.001:
                vzh, vcls = f"提升至 {_pct(latest_v)}", "ok"
            else:
                vzh, vcls = f"稳定 {_pct(latest_v)}", "ok"
        elif latest_v >= best_v - 0.001:
            vzh, vcls = f"波动后回升 {_pct(latest_v)}", "warn"
        else:
            gap = (best_v - latest_v) * 100
            vzh = f"第{first_drop + 1}轮起 {_pct(latest_v)}（峰值 {_pct(best_v)}，-{gap:.0f}pp）"
            vcls = "bad" if gap > 10 else "warn"
        zh_name, _ = meta_for(name)
        detail = {f"第{i + 1}轮": _pct(v) for i, v in enumerate(vals)}
        detail["状态"] = vzh
        hist_rows += (f"<tr data-detail='{detail_attr(detail)}'>"
                      f"<td><span class='mono'>{escape(name)}</span>"
                      f"<div class='desc'>{escape(zh_name)}</div></td>"
                      f"<td><div id='sp-{escape(name)}' class='chart-box'></div></td>"
                      f"<td class='num'><b>{_pct(latest_v)}</b></td>"
                      f"<td><span class='badge {vcls}'>{escape(vzh)}</span></td></tr>")
        spark_series[name] = vals

    # 最近运行明细
    run_rows = ""
    for r in reversed(runs[-14:]):
        m = r.get("metrics", {})
        g = r.get("gate") or {}
        gs = g.get("status", "-")
        gcls = "ok" if gs in ("PASS", "no_baseline", "no_baseline_for_set") else "bad"
        rr_detail = {"评测集": r.get("evalset_name", ""), "门禁": gs,
                     "备注": (r.get("note") or "-")[:80],
                     "协议": (r.get("environment") or {}).get("protocol", "-"),
                     "记忆库规模": str((r.get("environment") or {}).get("shared_atoms_total", "-")),
                     "用时": f"{r.get('duration_ms', '?')} ms"}
        run_rows += (f"<tr data-detail='{detail_attr(rr_detail)}'><td class='mono'><div class='ellipsis' style='max-width:170px;font-size:11px'>{escape(r.get('run_id',''))}</div></td>"
                     f"<td>{escape(r.get('evalset_name',''))}</td>"
                     f"<td class='num'>{fmt_pct(m.get('recall_at_k'))}</td><td class='num'>{fmt_pct(m.get('mrr'))}</td>"
                     f"<td class='num'>{r.get('duration_ms','?')} ms</td>"
                     f"<td><span class='badge {gcls}'>{escape(gs)}</span></td>"
                     f"<td>{escape(r.get('note','') or '')}</td></tr>")

    # 门禁明细
    gate_rows = ""
    for name, res in sorted((gate_report.get("evalsets") or {}).items()):
        st = res.get("status")
        gcls = "ok" if st == "PASS" else ("dim" if st == "SKIP" else "bad")
        checks = res.get("checks") or []
        cdesc = "；".join(f"{c['metric']}={c['value']:.3f}≥{c['threshold']:.3f}" for c in checks[:3]) or "-"
        gate_rows += (f"<tr><td>{escape(name)}</td><td><span class='badge {gcls}'>{escape(str(st))}</span></td>"
                      f"<td class='mono' style='font-size:11px'>{escape(cdesc)}</td></tr>")

    # 评测集/数据集资产表
    evalset_asset_rows = ""
    for r in dwd_evalsets.get("records", []):
        evalset_asset_rows += (f"<tr><td class='mono' style='font-size:11px'>{escape(r.get('file',''))}</td>"
                               f"<td class='num'>{r.get('cases','?')}</td>"
                               f"<td><span class='badge {'info' if r.get('origin')=='公开基准转换' else 'dim'}'>{escape(r.get('origin',''))}</span></td>"
                               f"<td class='mono' style='font-size:11px'>{escape((r.get('sha256') or '')[:10])}…</td></tr>")
    dataset_asset_rows = ""
    for c in dwd_datasets.get("cards", []):
        dataset_asset_rows += (f"<tr><td>{escape(str(c.get('name','')))}</td>"
                               f"<td>{escape(str(c.get('scale','')))}</td>"
                               f"<td class='mono' style='font-size:11px'>{escape(str(c.get('sha256') or '')[:10])}…</td>"
                               f"<td><span class='badge ok'>已校验</span></td></tr>")

    # 答案级评测面板（LLM-judge 协议，读最新 ans-*.json）
    ans_block = ""
    if ans_latest:
        s = ans_latest.get("summary") or {}
        rows_a = (ans_latest.get("rows") or [])[-6:]
        dist = s.get("verdict_distribution") or {}
        dist_s = " ".join(f"<span class='badge {'ok' if v in ('correct', 'abstain_ok') else ('warn' if v == 'partial' else 'bad')}'>"
                          f"{escape(k)} {v}</span>" for k, v in sorted(dist.items()))
        def _why(r: dict) -> str:
            if r.get("picked") is not None:  # MC 多选题：显示所选与正确项
                return f"模型选 {r.get('picked')}，正确 {r.get('correct_letter')}"
            return str(r.get("reasoning", ""))[:90]

        sample_rows = "".join(
            f"<tr><td class='mono nowrap' style='font-size:11px'>{escape(r.get('id', ''))}</td>"
            f"<td title='{escape(str(r.get('query', '')))}'>{escape(str(r.get('query', ''))[:64])}</td>"
            f"<td class='num'>{r.get('token_f1', '-')}</td>"
            f"<td><span class='badge {'ok' if r.get('verdict') in ('correct', 'abstain_ok') else ('warn' if r.get('verdict') == 'partial' else 'bad')}'>{escape(str(r.get('verdict')))}</span></td>"
            f"<td class='desc' style='font-size:11px' title='{escape(str(r.get('reasoning', '')))}'>{escape(_why(r))}</td></tr>"
            for r in rows_a)
        ans_block = f"""
  <div class="panel">
    <h2>答案级评测（LLM-judge） <span class="tag">{escape(ans_latest.get('evalset', ''))} · {ans_latest.get('items_total', 0)} 题 · {escape(str(s.get('duration_s', '')))}s</span></h2>
    <div class="desc" style="font-size:12.5px">结论：<b>{escape(str(s.get('headline', '')))}</b>　判定分布：{dist_s}</div>
    <table class="tbl"><thead><tr><th>用例</th><th>问题</th><th>token_f1</th><th>判定</th><th>理由</th></tr></thead>
    <tbody>{sample_rows}</tbody></table>
    <div class="desc" style="margin-top:8px">完整逐题结果见 <code>评测中台/runs/{escape(ans_latest.get('run_id', ''))}.json</code>（Qwen3-4B 本地判分，诚实边界：LLM 判定可能有少量误判，抽样人工复核）。</div>
  </div>"""

    body = f"""
  <div class="kpis">
    {kpi('评测集', str(len(latest)), f'用例总数 {sum(case_counts.values()):,} 条', '', 'target', 'p-board')}
    {kpi('运行存档', str(len(runs)), '每次运行含环境与评测集指纹', '', 'layers', 'p-runs')}
    {kpi('最新主指标均值', fmt_pct(avg_primary), '混合协议口径：pooled 大池 + 会话级 + 逐题隔离', 'good' if (avg_primary or 0) >= 0.9 else 'warn', 'gauge')}
    {kpi('门禁状态', escape(gate_status), 'gate.py 对比 baseline.json', 'good' if gate_status == 'PASS' else ('dim' if gate_status == '未运行' else 'bad'), 'shield', 'p-gate')}
    {kpi('评测环境', 'TF-IDF', 'precision 口径 · 离线可复现 · :memory:', '', 'database')}
  </div>
  {ans_block}

  <div class="panel">
    <h2>评测执行链路 <span class="tag">对齐 OpenCompass 式「评测集 → 运行 → 榜单」三层抽象</span></h2>
    <div class="flow">
      <div class="stage"><h3>① 评测集 JSONL</h3><ul><li>{len(case_counts)} 个评测集 / {sum(case_counts.values()):,} 用例</li>
        <li>内建 + 生成 + 公开基准转换</li><li>指纹入库（数据中台管理）</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>② 注入 store</h3><ul><li>pooled：共享单 Agent 记忆库</li><li>isolated：逐题独立实例</li>
        <li>flush_index 跨会话建边</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>③ 召回 recall</h3><ul><li>mode=precision</li><li>Top-5</li><li>逐题可复现</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>④ 判分 judge</h3><ul><li>会话 ID 匹配</li><li>关键词校验</li><li>陷阱词拒答代理</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>⑤ 存档 + 门禁</h3><ul><li>runs/run-*.json</li><li>gate.py 基线对比</li></ul></div>
    </div>
  </div>

  <div class="panel">
    <h2>评测集榜单（最新运行） <span id="p-board" class="tag">主指标：Recall@K / 拒答集为 AbstentionPrecision</span></h2>
    <div class="tbl-wrap"><table class="tbl" data-enhance><thead><tr><th>评测集</th><th>用例</th><th>主指标</th><th>MRR</th><th>nDCG@K</th><th>KeywordHit</th><th>时间</th></tr></thead>
    <tbody>{cards}</tbody></table></div>
  </div>

  <div class="panel"><h2>评测集 × 指标热力格 <span class="tag">Recall / MRR / nDCG / KeywordHit / Abstention</span></h2>
    <div id="chart-heat" class="chart-box"></div>
  </div>

  <div class="grid g2">
    <div class="panel">
      <h2>对照实验 A：多跳召回改进 <span class="tag">小规模记忆库轮次（≤100 atoms）</span></h2>
      <div id="chart-ab" class="chart-box"></div>
      <div class="desc" style="margin-top:10px">默认配置下 multi_hop 仅 2/6 命中（查询与目标会话零词汇重叠，
      且 flush_index 未在中文短句间自动建边）。对同链显式建立「因果接续」边并启用 causal_bridge 后升至
      6/6 —— 小池子上边是决定性的。</div>
    </div>
    <div class="panel">
      <h2>对照实验 B：图边消融（大池 1500+ atoms） <span class="tag">同池同题 A/B</span></h2>
      <div id="chart-ab2" class="chart-box"></div>
      <div class="desc" style="margin-top:10px">multi_hop_large 180 题：带边与 --no-edges 消融**完全一致
      （0.239 = 0.239）**——大池子下多跳 0.239 的瓶颈不是"有没有边"，而是图游走在该规模上未生效；
      其余维度（basic/temporal/conflict/preference）带边与无边差异均在 ±3pp 噪声内，图边不增益也不减损。
      这是改进方向（PPR 种子/预算/游走深度）的直接证据。</div>
    </div>
  </div>

  <div class="panel"><h2>历史趋势 · 每个评测集稳不稳 <span class="tag">迷你线=逐轮 Recall@K · 悬浮看每轮数值 · 点行展开</span></h2>
    <div class="tbl-wrap"><table class="tbl" data-enhance><thead><tr><th>评测集</th><th style="width:190px">走势（第 1 → 最新轮）</th><th class="num">最新</th><th>状态</th></tr></thead>
    <tbody>{hist_rows}</tbody></table></div>
    <div class="desc" style="margin-top:8px">混合了小池子（42 atoms）与大池子（1500+ atoms）环境——跨环境比较请以 gate.py 的环境可比性警告为准。持续评测将在此累积趋势。</div>
  </div>

  <div class="grid g2">
    <div class="panel">
      <h2>最近运行明细 <span id="p-runs" class="tag">完整逐题结果见 runs/*.json</span></h2>
      <div class="tbl-wrap"><table class="tbl" data-enhance><thead><tr><th>运行 ID</th><th>评测集</th><th>Recall@K</th><th>MRR</th><th>耗时</th><th>门禁</th><th>备注</th></tr></thead>
      <tbody>{run_rows}</tbody></table></div>
    </div>
    <div>
      <div class="panel">
        <h2>回归门禁 <span id="p-gate" class="tag">基线 {escape((baseline.get('generated_at') or '')[:19])}</span></h2>
        <table class="tbl"><thead><tr><th>评测集</th><th>状态</th><th>阈值校验（前3项）</th></tr></thead>
        <tbody>{gate_rows}</tbody></table>
        <div class="desc" style="margin-top:8px">阈值 = 最新运行值 − 容忍带（0.05/拒答 0.10），故基线固化时刻的
        PASS 是定义使然；门禁的约束力体现在<b>下一次</b>运行（指标跌破阈值即 FAIL）。记忆库规模差 3 倍以上
        或协议不同时，gate.py 会输出「不可直接对比」警告。</div>
      </div>
      <div class="panel">
        <h2>指标口径说明</h2>
        <table class="tbl"><tbody>
          <tr><td>Recall@K</td><td>Top-K 内含任一标准会话的用例占比（主指标）</td></tr>
          <tr><td>MRR / nDCG@K</td><td>标准会话命中排名质量（二元相关折损累计增益）</td></tr>
          <tr><td>KeywordHit@K</td><td>首个命中会话正文含全部期望关键词（内容级校验）</td></tr>
          <tr><td>AbstentionPrecision</td><td>Top-K 不含陷阱词的用例占比（拒答代理，容忍带 0.10）</td></tr>
        </tbody></table>
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>评测集与数据集资产 <span class="tag">DWD 层 · {dwd_evalsets.get('file_count', 0)} 个评测集文件 / {dwd_datasets.get('file_count', 0)} 个数据集文件</span></h2>
    <div class="grid g2">
      <div>
        <div class="tbl-wrap"><table class="tbl" data-enhance><thead><tr><th>评测集文件</th><th>用例</th><th>来源</th><th>SHA-256</th></tr></thead>
        <tbody>{evalset_asset_rows}</tbody></table></div>
      </div>
      <div>
        <div class="tbl-wrap"><table class="tbl" data-enhance><thead><tr><th>公开基准数据集</th><th>规模</th><th>SHA-256</th><th>指纹卡</th></tr></thead>
        <tbody>{dataset_asset_rows}</tbody></table></div>
      </div>
    </div>
  </div>
"""

    page_js = """
(function(){
  var latest = DATA.latest, meta = DATA.meta;
  var names = Object.keys(latest).sort();
  var mks = ['recall_at_k','mrr','ndcg_at_k','keyword_hit','abstention_precision'];
  VMCharts.heatmap(document.getElementById('chart-heat'), names.map(function(n){
    var m = latest[n].metrics;
    return {name: n, cells: mks.map(function(k){
      var v = m[k];
      return {v: (v==null?0:v), label: k, text: v==null?'–':(''+Math.round(v*100))};
    })};
  }), {cell: 22, labelW: 160});
  var ab = DATA.ab;
  VMCharts.vbar(document.getElementById('chart-ab'), [
    {name:'默认配置', value: ab.default, color:'#ef4444'},
    {name:'显式因果建边', value: ab.bridged, color:'#059669'}
  ], {height: 220});
  var ab2 = DATA.ab2;
  if (ab2.without_edges != null) {
    VMCharts.vbar(document.getElementById('chart-ab2'), [
      {name:'带图边', value: ab2.with_edges, color:'#2563eb'},
      {name:'无边消融', value: ab2.without_edges, color:'#93c5fd'}
    ], {height: 220});
  } else {
    document.getElementById('chart-ab2').innerHTML = '<div class="desc">尚无消融运行（python run_eval.py --all --no-edges）</div>';
  }
  var spark = DATA.spark;
  Object.keys(spark).forEach(function(name){
    var el = document.getElementById('sp-' + name);
    if (el) VMCharts.spark(el, spark[name],
      {labels: spark[name].map(function(_, i){ return '第'+(i+1)+'轮'; }),
       fmt: function(v){ return Math.round(v*100)+'%'; }});
  });
})();
"""
    meta_zh = {n: meta_for(n)[0] for n in latest}
    return page_shell(
        "VibeMemory · 评测中台",
        "评测集 → 注入召回 → 判分 → 运行存档 → 回归门禁",
        body, datetime.now().strftime("%Y-%m-%d %H:%M"), page_key="eval",
    ).replace("__DATA__", jdump({
        "latest": {n: light(r) for n, r in latest.items()},
        "meta": meta_zh,
        "ab": {"default": mh_default, "bridged": mh_bridge},
        "ab2": {"with_edges": mh_large_with, "without_edges": mh_large_without},
        "history": [
            {name: hh[i] for name, hh in hist_sets.items() if i < len(hh)}
            for i in range(max((len(v) for v in hist_sets.values()), default=0))
        ],
        "spark": spark_series,
    })).replace("__PAGEJS__", page_js)


# ---------------------------------------------------------------- 测试中台页

def build_test_page() -> str:
    runs_dir = ROOT / "测试中台" / "runs"
    runs = []
    for p in sorted(runs_dir.glob("test-*.json")):
        d = load(p)
        if d:
            runs.append(d)
    runs.sort(key=lambda r: r.get("created_at") or "")
    latest = runs[-1] if runs else None
    s = (latest or {}).get("summary") or {}
    cov = (latest or {}).get("coverage") or {}
    gen_at = (latest or {}).get("created_at", "")[:19].replace("T", " ")

    run_rows = ""
    for r in runs:
        ss = r.get("summary") or {}
        g = r.get("gate") or {}
        gcls = "ok" if g.get("status") in ("PASS", "NO_BASELINE") else "bad"
        _rev = r.get("revision") or {}
        _rev_s = (f"{_rev.get('commit', '?')}（+{_rev.get('dirty_count', 0)} 处未提交）"
                  if _rev.get("git_available") else "未记录")
        tr_detail = {"范围": r.get("scope", ""), "运行 ID": r.get("run_id", ""),
                     "代码版本": _rev_s,
                     "通过": str(ss.get("passed", "?")), "失败": str(ss.get("failures", 0)),
                     "错误": str(ss.get("errors", 0)), "跳过": str(ss.get("skipped", 0)),
                     "行覆盖": fmt_pct((r.get("coverage") or {}).get("line_rate")),
                     "分支覆盖": fmt_pct((r.get("coverage") or {}).get("branch_rate")),
                     "执行时长": f"{ss.get('duration_s', '?')} s", "门禁": str(g.get("status"))}
        run_rows += (f"<tr data-detail='{detail_attr(tr_detail)}'><td class='mono'><div class='ellipsis' style='max-width:170px;font-size:11px'>{escape(r.get('run_id',''))}</div></td>"
                     f"<td><span class='badge dim'>{escape(r.get('scope',''))}</span></td>"
                     f"<td class='num'>{ss.get('total','?')}</td>"
                     f"<td class='num'>{fmt_pct(ss.get('pass_rate'))}</td>"
                     f"<td class='num'>{ss.get('failures', 0) + ss.get('errors', 0)}</td>"
                     f"<td class='num'>{ss.get('duration_s','?')}s</td>"
                     f"<td><span class='badge {gcls}'>{escape(str(g.get('status')))}</span></td>"
                     f"<td>{escape((r.get('note') or '')[:40])}</td></tr>")

    fail_rows = ""
    for f_ in (s.get("failures_detail") or []):
        fail_rows += (f"<tr><td class='mono' style='font-size:11px'>{escape(f_.get('test',''))}</td>"
                      f"<td>{escape(f_.get('message',''))}</td></tr>")
    fail_panel = (f"<table class='tbl'><thead><tr><th>失败用例</th><th>消息</th></tr></thead><tbody>{fail_rows}</tbody></table>"
                  if fail_rows else "<div class='desc'>最近一轮无失败用例。</div>")

    body = f"""
  <div class="kpis">
    {kpi('最新一轮用例', str(s.get('total', '?')), f"范围 {escape((latest or {}).get('scope',''))} · {escape(short_dt(gen_at))}", '', 'flask', 'p-history')}
    {kpi('通过率', fmt_pct(s.get('pass_rate')), f"通过 {s.get('passed','?')} / 失败 {s.get('failures', 0) + s.get('errors', 0)} / 跳过 {s.get('skipped', 0)}", 'good' if (s.get('pass_rate') or 0) >= 0.99 else 'bad', 'check')}
    {kpi('行覆盖率', fmt_pct(cov.get('line_rate')), f"分支 {fmt_pct(cov.get('branch_rate'))}", 'good', 'layers', 'p-cov')}
    {kpi('测试门禁', escape(str((latest or {}).get('gate', {}).get('status', '未运行'))), '与上一轮通过率对比（容忍带 2pp）', 'good' if (latest or {}).get('gate', {}).get('status') in ('PASS', 'NO_BASELINE') else 'bad', 'shield')}
    {kpi('执行时长', f"{s.get('duration_s', '?')}<span class='unit'>s</span>", f"墙钟 {(latest or {}).get('duration_wall_s', '?')}s", '', 'clock')}
  </div>

  <div class="panel">
    <h2>测试执行链路 <span class="tag">对齐测试中台「框架 → 平台 → 能力下沉」分层（调研见 docs/测试中台调研.md）</span></h2>
    <div class="flow">
      <div class="stage"><h3>① 被测仓库</h3><ul><li>VibeMemory tests/</li><li>pytest 回归层</li><li>本地未提交改动透明记录</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>② 执行 run_tests.py</h3><ul><li>仓库 .venv 内运行</li><li>junitxml + coverage xml</li><li>basetemp 隔离</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>③ 解析判分</h3><ul><li>用例数/通过率/失败明细</li><li>行/分支覆盖率</li><li>相对门禁（2pp）</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>④ 存档回流</h3><ul><li>runs/test-*.json</li><li>junit/cov 证据文件</li><li>数据中台 DWD 层</li></ul></div>
    </div>
  </div>

  <div class="grid g2">
    <div class="panel"><h2>回归通过率趋势</h2><div id="chart-test-trend" class="chart-box"></div>
      <div class="desc" style="margin-top:8px">通过率受本地未提交改动影响，仅代表当前工作区状态，不代表已推送 CI 结论。</div></div>
    <div class="panel" id="p-cov"><h2>覆盖率与规模</h2><div id="chart-test-cov" class="chart-box"></div>
      <div class="desc" style="margin-top:8px">覆盖率来自 pytest-cov（coverage xml），随测试运行同步产出。</div></div>
  </div>

  <div class="panel">
    <h2>运行历史 <span id="p-history" class="tag">{len(runs)} 轮</span></h2>
    <div class="tbl-wrap"><table class="tbl" data-enhance><thead><tr><th>运行 ID</th><th>范围</th><th>用例</th><th>通过率</th><th>失败/错误</th><th>耗时</th><th>门禁</th><th>备注</th></tr></thead>
    <tbody>{run_rows}</tbody></table></div>
  </div>

  <div class="panel">
    <h2>失败明细 <span class="tag">完整输出见 junitxml 证据文件</span></h2>
    {fail_panel}
  </div>
"""
    page_js = """
(function(){
  var runs = DATA.runs;
  var rates = runs.map(function(r){ return (r.summary||{}).pass_rate || 0; });
  var allPerfect = rates.length > 0 && rates.every(function(v){ return v >= 0.999; });
  VMCharts.line(document.getElementById('chart-test-trend'),
    runs.map(function(_, i){ return '第'+(i+1)+'轮'; }),
    [{name:'通过率', values: rates, color:'#059669'}],
    {max: 1, height: 210, showPoints: !allPerfect, fmt: function(v){ return Math.round(v*100)+'%'; }});
  if (allPerfect) {
    var tip = document.createElement('div');
    tip.className = 'desc'; tip.style.marginTop = '6px';
    tip.textContent = '最近 ' + rates.length + ' 轮全部 100% 通过，趋势线呈水平属正常（无退化即最好状态）。';
    document.getElementById('chart-test-trend').appendChild(tip);
  }
  var L = DATA.latest, cov = (L&&L.coverage)||{};
  VMCharts.bar(document.getElementById('chart-test-cov'), [
    {name:'行覆盖率', value: cov.line_rate||0, max:1, color:'#059669'},
    {name:'分支覆盖率', value: cov.branch_rate||0, max:1, color:'#34d399'},
    {name:'通过率', value: (L&&L.summary&&L.summary.pass_rate)||0, max:1, color:'#a7f3d0'}
  ], {fmt:function(v){return (v*100).toFixed(1)+'%';}, max:1});
})();
"""
    light_runs = [{"summary": r.get("summary"), "coverage": r.get("coverage"),
                   "run_id": r.get("run_id"), "scope": r.get("scope")} for r in runs]
    return page_shell(
        "VibeMemory · 测试中台",
        "pytest 回归 → 通过率/覆盖率 → 相对门禁 → 存档回流",
        body, gen_at, page_key="test",
    ).replace("__DATA__", jdump({"runs": light_runs, "latest": latest or {}})) \
     .replace("__PAGEJS__", page_js)


# ---------------------------------------------------------------- 监控中台页

def build_ops_page() -> str:
    runs_dir = ROOT / "监控中台" / "runs"
    runs = []
    for p in sorted(runs_dir.glob("obs-*.json")):
        d = load(p)
        if d:
            runs.append(d)
    runs.sort(key=lambda r: r.get("created_at") or "")
    latest = runs[-1] if runs else None
    wl = (latest or {}).get("workload") or {}
    st = (latest or {}).get("store") or {}
    rc = (latest or {}).get("recall") or {}
    g = (latest or {}).get("graph") or {}
    deg = (latest or {}).get("sdk_metrics") or {}
    gen_at = (latest or {}).get("created_at", "")[:19].replace("T", " ")

    run_rows = ""
    for r in runs:
        rr = r.get("recall") or {}
        created = (r.get("created_at") or "")
        created_s = escape(short_dt(created))
        obs_detail = {"运行 ID": r.get("run_id", ""), "seed": str((r.get("workload") or {}).get("seed", "-")),
                      "语料规模": str((r.get("workload") or {}).get("atoms_target", "-")),
                      "查询次数": str((r.get("workload") or {}).get("queries", "-")),
                      "store p50/p95": f"{(r.get('store') or {}).get('p50_ms', '-')} / {(r.get('store') or {}).get('p95_ms', '-')} ms",
                      "recall p50/p95": f"{rr.get('p50_ms', '-')} / {rr.get('p95_ms', '-')} ms",
                      "采集时间": r.get("created_at", "")}
        run_rows += (f"<tr data-detail='{detail_attr(obs_detail)}'><td class='mono'><div class='ellipsis' style='max-width:170px;font-size:11px'>{escape(r.get('run_id',''))}</div></td>"
                     f"<td class='num'>{(r.get('workload') or {}).get('atoms_target','?')}</td>"
                     f"<td class='num'>{rr.get('p50_ms','?')}</td><td class='num'>{rr.get('p95_ms','?')}</td>"
                     f"<td class='num'>{fmt_pct(rr.get('hit_rate'))}</td>"
                     f"<td class='num'>{fmt_pct(rr.get('zero_hit_rate'))}</td>"
                     f"<td class='mono' style='font-size:11px'>{created_s}</td></tr>")

    deg_rows = ""
    for k, v in sorted((deg.get("degradation") or {}).items(), key=lambda x: -x[1]):
        deg_rows += (f"<tr><td class='mono'>{escape(k)}</td><td class='num'>{v}</td></tr>")
    deg_panel = (f"<table class='tbl'><thead><tr><th>降级事件</th><th>次数</th></tr></thead><tbody>{deg_rows}</tbody></table>"
                 if deg_rows else "<div class='desc'>本次负载未触发降级事件。</div>")

    body = f"""
  <div class="kpis">
    {kpi('图规模', f"{g.get('total_atoms', '?')}<span class='unit'>atoms</span>", f"边 {g.get('total_edges', '?')} · 活跃 {g.get('active_atoms', '?')}", '', 'database', 'p-lat')}
    {kpi('store p95', f"{st.get('p95_ms', '?')}<span class='unit'>ms</span>", f"p50 {st.get('p50_ms','?')} / p99 {st.get('p99_ms','?')} ms · {st.get('count','?')} 次", '', 'clock')}
    {kpi('recall p95', f"{rc.get('p95_ms', '?')}<span class='unit'>ms</span>", f"p50 {rc.get('p50_ms','?')} / p99 {rc.get('p99_ms','?')} ms · {rc.get('count','?')} 次", 'good', 'pulse')}
    {kpi('已知查询命中率', fmt_pct(rc.get('hit_rate')), f"{rc.get('known_hits','?')}/{rc.get('known_queries','?')} 会话命中", 'good' if (rc.get('hit_rate') or 0) >= 0.8 else 'warn', 'target', 'p-quality2')}
    {kpi('未入库零命中率', fmt_pct(rc.get('zero_hit_rate')), f"precision 门控拒绝无关结果", 'good' if (rc.get('zero_hit_rate') or 0) >= 0.7 else 'warn', 'shield')}
  </div>

  <div class="panel">
    <h2>可观测链路 <span class="tag">确定性负载 → 指标采样 → 存档回流（监控中台定位：运行时健康，区别于评测的"质量"）</span></h2>
    <div class="flow">
      <div class="stage"><h3>① 负载构造</h3><ul><li>{wl.get('atoms_target', '?')} 条唯一语料</li><li>60%事实/25%决策/15%偏好</li><li>seed={wl.get('seed', '?')} 可复现</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>② 写入相位</h3><ul><li>逐条 store 计时</li><li>flush_index 建边</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>③ 检索相位</h3><ul><li>{wl.get('queries', '?')} 次（70%已知/30%未入库）</li><li>逐次计时 + 命中判定</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>④ 指标采样</h3><ul><li>延迟分位数</li><li>命中率/零命中率</li><li>降级事件（MetricsCollector）</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>⑤ 存档回流</h3><ul><li>runs/obs-*.json</li><li>数据中台 DWD 层</li></ul></div>
    </div>
  </div>

  <div class="grid g2">
    <div class="panel" id="p-lat"><h2>操作延迟分位数 <span class="tag">本机单线程，非并发 SLA</span></h2>
      <div id="chart-ops-lat" class="chart-box"></div></div>
    <div class="panel" id="p-quality2"><h2>检索质量 <span class="tag">1000 atoms 工作负载</span></h2>
      <div id="chart-ops-q" class="chart-box"></div>
      <div class="desc" style="margin-top:8px">命中率=已知答案查询中精确会话进入 Top-5 的比例；零命中率=未入库查询被 precision 门控拒绝返回任何结果的比例。</div></div>
  </div>

  <div class="panel">
    <h2>降级事件与图规模 <span class="tag">MetricsCollector 快照</span></h2>
    <div class="grid g2">
      <div>{deg_panel}</div>
      <div><table class="tbl"><tbody>
        <tr><td>总 atoms</td><td class="num">{g.get('total_atoms', '?')}</td></tr>
        <tr><td>总边</td><td class="num">{g.get('total_edges', '?')}</td></tr>
        <tr><td>活跃 atoms</td><td class="num">{g.get('active_atoms', '?')}</td></tr>
        <tr><td>检索总次数</td><td class="num">{((deg.get('operations') or {}).get('recall') if isinstance(deg.get('operations'), dict) else (deg.get('operations') or {}).get('total_recalls', '?')) if isinstance(deg.get('operations'), dict) else '?'}</td></tr>
      </tbody></table></div>
    </div>
  </div>

  <div class="panel">
    <h2>采集历史 <span class="tag">{len(runs)} 轮</span></h2>
    <div class="tbl-wrap"><table class="tbl" data-enhance><thead><tr><th>运行 ID</th><th>语料</th><th>recall p50</th><th>recall p95</th><th>命中率</th><th>零命中率</th><th>时间</th></tr></thead>
    <tbody>{run_rows}</tbody></table></div>
    <div class="desc" style="margin-top:8px">{escape((latest or {}).get('limits', ''))}</div>
  </div>
"""
    page_js = """
(function(){
  var L = DATA.latest;
  VMCharts.vbar(document.getElementById('chart-ops-lat'), [
    {name:'store p50', value:(L.store.p50_ms||0), color:'#93c5fd'},
    {name:'store p95', value:(L.store.p95_ms||0), color:'#2563eb'},
    {name:'recall p50', value:(L.recall.p50_ms||0), color:'#6ee7b7'},
    {name:'recall p95', value:(L.recall.p95_ms||0), color:'#059669'}
  ], {percent:false, height:230, max: Math.ceil((L.store.p99_ms||15)*1.15),
      fmt:function(v){return v.toFixed(1)+'ms';}});
  VMCharts.vbar(document.getElementById('chart-ops-q'), [
    {name:'已知命中率', value:(L.recall.hit_rate||0), color:'#059669'},
    {name:'未入库零命中率', value:(L.recall.zero_hit_rate||0), color:'#2563eb'}
  ], {height:230});
})();
"""
    light_runs = [{"workload": r.get("workload"), "recall": r.get("recall"),
                   "run_id": r.get("run_id"), "created_at": r.get("created_at")} for r in runs]
    return page_shell(
        "VibeMemory · 监控中台",
        "运行时负载 → 延迟/命中率/降级采样 → 存档回流",
        body, gen_at, page_key="ops",
    ).replace("__DATA__", jdump({"runs": light_runs, "latest": latest or {}})) \
     .replace("__PAGEJS__", page_js)


# ---------------------------------------------------------------- 门户

def build_index() -> str:
    assets_dir = ROOT / "数据中台" / "assets"
    catalog = load(assets_dir / "catalog.json") or {}
    quality = load(assets_dir / "quality_report.json") or {}
    weekly = load(assets_dir / "dws" / "weekly.json") or {}
    signals_doc = load(assets_dir / "signals.json") or {}
    runs = [load(p) for p in sorted((ROOT / "评测中台" / "runs").glob("run-*.json"))]
    runs = [r for r in runs if r]
    gate_report = load(ROOT / "评测中台" / "gate_report.json") or {}
    test_runs = [load(p) for p in sorted((ROOT / "测试中台" / "runs").glob("test-*.json"))]
    test_latest = test_runs[-1] if test_runs else {}
    obs_runs = [load(p) for p in sorted((ROOT / "监控中台" / "runs").glob("obs-*.json"))]
    obs_latest = obs_runs[-1] if obs_runs else {}
    latest = {}
    for r in runs:
        latest[r.get("evalset_name") or "?"] = r
    case_counts = {}
    for d in [ROOT / "评测中台" / "evalsets", ROOT / "评测中台" / "benchmarks"]:
        for p in d.glob("*.jsonl"):
            case_counts[p.stem] = sum(1 for ln in p.read_text(encoding="utf-8-sig").splitlines() if ln.strip())
    total_cases = sum(case_counts.values())
    src = catalog.get("sources", [])
    src_ok = sum(1 for s in src if s.get("status") == "ok")

    # 周对比 + 运行信号面板（质量闭环：这周比上周好吗 / 异常有信号）
    verdict_map = {"better": ("变好", "ok"), "same": ("持平", "info"),
                   "worse": ("变差", "bad"), "insufficient": ("数据不足", "dim")}
    lvl_map = {"ok": ("正常", "ok"), "warn": ("警告", "warn"), "bad": ("异常", "bad")}

    def _fmt_pct(v):
        return "-" if v is None else f"{v * 100:.1f}%"

    def _week_row(label, blk, key, dkey):
        v = blk.get("verdict", "insufficient")
        vzh, vcls = verdict_map.get(v, ("未知", "dim"))
        d = blk.get(dkey)
        dd = "-" if d is None else f"{d * 100:+.1f}pp"
        return (f"<tr><td>{label}</td>"
                f"<td class='num'>{_fmt_pct((blk.get('this') or {}).get(key))}</td>"
                f"<td class='num'>{_fmt_pct((blk.get('last') or {}).get(key))}</td>"
                f"<td class='num'>{dd}</td>"
                f"<td><span class='badge {vcls}'>{vzh}</span></td></tr>")

    weekly_block = ""
    if weekly.get("answer"):
        ev, ts = weekly.get("eval", {}), weekly.get("test", {})
        weekly_block = f"""
  <div class="panel">
    <h2>周对比 · 这周比上周好吗 <span class="tag">滚动 7 天 vs 前 7 天 · dws_weekly</span></h2>
    <div class="desc" style="font-size:12.5px">{escape(weekly.get('answer', ''))}</div>
    <table class="tbl"><thead><tr><th>指标</th><th>本周</th><th>上周</th><th>变化</th><th>判定</th></tr></thead>
    <tbody>{_week_row('评测命中率', ev, 'hit_rate', 'delta_hit_rate')}{_week_row('测试用例通过率', ts, 'case_pass_rate', 'delta_case_pass_rate')}</tbody></table>
  </div>"""

    sig_rows = ""
    for x in signals_doc.get("signals", []):
        lzh, lcls = lvl_map.get(x.get("level", "ok"), ("未知", "dim"))
        sig_rows += (f"<div class='sig-card lv-{escape(x.get('level', 'ok'))}'>"
                     f"<span class='dot'></span>"
                     f"<div class='txt'><div class='code'>{escape(x.get('code', ''))} "
                     f"<span class='badge {lcls}'>{lzh}</span></div>"
                     f"<div class='detail'>{escape(x.get('detail', ''))}</div></div></div>")
    overall = signals_doc.get("overall", "ok")
    ozh, ocls = lvl_map.get(overall, ("未知", "dim"))
    signals_block = ""
    if sig_rows:
        signals_block = f"""
  <div class="panel">
    <h2>运行信号 <span id="p-signals" class="tag">数据停更/来源/质量/门禁/服务 · signals.py</span></h2>
    <div class="desc">总体状态：<span class="badge {ocls}">{ozh}</span>（服务失联由 keepalive 自愈重启，事件记 signals.log）</div>
    <div class="sig-grid">{sig_rows}</div>
  </div>"""

    # 近 14 天数据活动（血缘事件逐日计数）：数据自己长的直观曲线
    from datetime import timedelta
    day_counts: dict[str, int] = {}
    try:
        for ln in (assets_dir / "lineage_events.jsonl").read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            ev = json.loads(ln)
            ev_ts = str(ev.get("eventTime") or ev.get("ts") or "")
            if not ev_ts:
                continue
            try:
                # 服务器时区是 UTC：按时间戳自带的时区转本机日期，凌晨产出不错位
                day = datetime.fromisoformat(ev_ts).astimezone().strftime("%Y-%m-%d")
            except ValueError:
                day = ev_ts[:10]
            if day:
                day_counts[day] = day_counts.get(day, 0) + 1
    except Exception:
        pass
    activity = []
    for i in range(13, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        activity.append({"day": d[5:], "n": day_counts.get(d, 0)})

    body = f"""
  <div class="kpis">
    {kpi('数据中台', f'{src_ok}/{len(src)}', '可信来源校验通过', '', 'database', 'entry-data')}
    {kpi('数据质量', f"{quality.get('summary', {}).get('overall', 0):.3f}", '六性综合分', 'good', 'gauge')}
    {kpi('评测中台', f"{len(latest)} 集合", f"{total_cases:,} 用例 · 门禁 {escape(str(gate_report.get('status', '未运行')))}", '', 'target', 'entry-eval')}
    {kpi('测试中台', fmt_pct(((test_latest.get('summary') or {}).get('pass_rate'))), f"{((test_latest.get('summary') or {}).get('total', 0)):,} 用例 · 行覆盖 {fmt_pct(((test_latest.get('coverage') or {}).get('line_rate')))}", 'good', 'flask', 'entry-test')}
    {kpi('监控中台', f"{((obs_latest.get('recall') or {}).get('p95_ms', '?'))}<span class='unit'>ms</span>", f"recall p95 · 命中率 {fmt_pct(((obs_latest.get('recall') or {}).get('hit_rate')))}", 'good', 'pulse', 'entry-ops')}
  </div>
  {weekly_block}
  {signals_block}

  <div class="panel">
    <h2>近 14 天数据活动 <span id="p-activity" class="tag">血缘事件逐日计数 · collect/build_assets 自动产出</span></h2>
    <div id="chart-activity" class="chart-box"></div>
    <div class="desc" style="margin-top:8px">每天定时流水线自动跑评测/测试/监控并回流存档，血缘事件随之逐日累积；连续有柱 = 数据底座在持续增长。</div>
  </div>

  <div class="grid g2">
    <div class="panel entry" id="entry-data">
      <div class="entry-head"><span class="entry-ico">{icon('database')}</span>
        <div><div class="entry-t">数据中台</div><div class="entry-en">Data Platform · 底座</div></div></div>
      <div class="desc">对齐信通院《数据中台实践指南》六层架构与阿里 OneData 方法论的轻量实现：
      汇聚（采集+指纹校验）→ 存储（ODS/DWD/DWS 分层资产）→ 开发（清洗/聚合）→
      资产治理（质量六性/目录/血缘）→ 服务（JSON 资产统一出口）→ 应用（可视化大屏）。</div>
      <ul>
        <li>可信数据来源：{len(src)} 类来源登记 + SHA-256 指纹 + 变更检测 + 溯源档案</li>
        <li>数据资产：{sum(a.get('rows') or 0 for a in catalog.get('assets', [])):,} 条记录 · {len(catalog.get('assets', []))} 个资产 · 质量六性 {quality.get('summary', {}).get('overall', 0):.3f}</li>
        <li>其他三中台的运行存档全部回流至此，统一目录与溯源</li>
      </ul>
      <a class="card-cta" href="data-platform.html">进入数据中台大屏 →</a>
    </div>
    <div class="panel entry" id="entry-eval">
      <div class="entry-head"><span class="entry-ico">{icon('target')}</span>
        <div><div class="entry-t">评测中台</div><div class="entry-en">Evaluation Platform</div></div></div>
      <div class="desc">对齐 OpenCompass 式「评测集 → 运行 → 榜单」三层抽象与 LoCoMo/LongMemEval
      记忆评测能力分类：内建冒烟 + 生成大样本 + 公开基准转换（双记忆协议 pooled/isolated）、
      规则判分 + Wilson 置信区间、运行存档可复现、基线门禁。</div>
      <ul>
        <li>评测集：{len(case_counts)} 个文件 / {total_cases:,} 用例（JSONL，指纹纳入数据资产管理）</li>
        <li>公开基准实跑：LoCoMo 十段 R@5 0.74–0.89 ｜ LongMemEval-S 子集 0.80（CI 同屏）</li>
        <li>答案级 LLM-judge：对抗纠偏 / 拒答 / PersonaMem 多选（本地 Qwen3-4B）</li>
      </ul>
      <a class="card-cta" href="eval-platform.html">进入评测中台大屏 →</a>
    </div>
    <div class="panel entry" id="entry-test">
      <div class="entry-head"><span class="entry-ico">{icon('flask')}</span>
        <div><div class="entry-t">测试中台</div><div class="entry-en">Test Platform</div></div></div>
      <div class="desc">对齐测试中台「能力下沉为基础设施」方法论（调研见 docs/测试中台调研.md）：
      执行仓库 pytest 回归（junitxml + coverage xml 证据化），产出通过率/覆盖率/失败明细，
      相对门禁（通过率容忍带 2pp）。</div>
      <ul>
        <li>首轮全量：{((test_latest.get('summary') or {}).get('total', 0)):,} 用例全部通过（{((test_latest.get('summary') or {}).get('duration_s', '?'))}s）</li>
        <li>覆盖率：行 {fmt_pct(((test_latest.get('coverage') or {}).get('line_rate')))} / 分支 {fmt_pct(((test_latest.get('coverage') or {}).get('branch_rate')))}（与数据中台 coverage.xml 来源互补）</li>
        <li>存档回流：test-*.json + junit/cov 证据文件入数据中台</li>
      </ul>
      <a class="card-cta" href="test-platform.html">进入测试中台大屏 →</a>
    </div>
    <div class="panel entry" id="entry-ops">
      <div class="entry-head"><span class="entry-ico">{icon('pulse')}</span>
        <div><div class="entry-t">监控中台</div><div class="entry-en">Observability Platform</div></div></div>
      <div class="desc">对 SDK 运行时执行确定性混合负载（唯一锚点语料，seed 可复现），采样
      操作延迟分位数、检索命中率、未入库零命中率（precision 门控）、MetricsCollector 降级事件。</div>
      <ul>
        <li>1000 atoms 负载：recall p95 {((obs_latest.get('recall') or {}).get('p95_ms', '?'))}ms、store p95 {((obs_latest.get('store') or {}).get('p95_ms', '?'))}ms</li>
        <li>已知查询命中率 {fmt_pct(((obs_latest.get('recall') or {}).get('hit_rate')))} ｜ 未入库零命中率 {fmt_pct(((obs_latest.get('recall') or {}).get('zero_hit_rate')))}</li>
        <li>监控回答"运行时健康"，与评测的"质量"互补（区分协议与环境）</li>
      </ul>
      <a class="card-cta" href="ops-platform.html">进入监控中台大屏 →</a>
    </div>
  </div>

  <div class="panel">
    <h2>四中台如何协同</h2>
    <div class="flow">
      <div class="stage"><h3>VibeMemory 仓库</h3><ul><li>results/ · coverage.xml · tests/</li><li>STATUS/CHANGELOG · git</li><li>SDK（store/recall/metrics）</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>数据中台（底座）</h3><ul><li>采集+指纹校验</li><li>三层资产+质量六性</li><li>资产目录+溯源</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>三大能力中台</h3><ul><li>评测：记忆质量 + 门禁</li><li>测试：回归健康 + 门禁</li><li>监控：运行时健康</li></ul></div>
      <div class="arrow">→</div>
      <div class="stage"><h3>统一回流与呈现</h3><ul><li>全部 runs 回流数据中台</li><li>四张大屏 · 统一门禁语义</li></ul></div>
    </div>
  </div>

  <div class="panel">
    <h2>资料索引 <span class="tag">docs/</span></h2>
    <table class="tbl"><thead><tr><th>文档</th><th>内容</th></tr></thead><tbody>
      <tr><td class="mono">docs/数据中台调研.md</td><td>定义起源 / 六层架构与六大能力域 / OneData·DAYU·DataOps / 主流产品对比 / 建设路径（19 条来源）</td></tr>
      <tr><td class="mono">docs/测试中台调研.md</td><td>测试平台三级辨析 / 四层架构与 11 能力域 / 大厂案例 / 开源工具全景（33 条来源）</td></tr>
      <tr><td class="mono">docs/评测中台调研.md</td><td>LLM 记忆评测基准（LoCoMo/LongMemEval）/ 指标体系 / 评测平台实践 / LLM-as-a-judge 偏差（66 条来源）</td></tr>
      <tr><td class="mono">docs/数据集资源清单.md</td><td>26 个公开基准 × 能力维度矩阵 / 许可证风险带 / 接入建议（2026-11-08 核实）</td></tr>
      <tr><td class="mono">docs/数据可靠性与证据力评估.md</td><td>可靠性三层判定 / 各评测集 Wilson 置信区间 / "数据量够不够"逐主张裁定 / 图边消融结论</td></tr>
      <tr><td class="mono">docs/免费域名渠道.md</td><td>免费域名渠道核实（DigitalPlat / eu.org / is-a.dev / pp.ua）+ 固定地址落地路径（2026-11-08 检索）</td></tr>
      <tr><td class="mono">docs/中台方案设计.md</td><td>本中台的完整方案：架构映射 / 可信来源 / 评测集设计 / 指标 / 门禁 / 四中台整合 / 边界声明</td></tr>
    </tbody></table>
  </div>
"""
    page_js = """
(function(){
  var act = DATA.activity;
  var maxN = Math.max.apply(null, act.map(function(a){ return a.n; }).concat([1]));
  VMCharts.vbar(document.getElementById('chart-activity'), act.map(function(a){
    return {name: a.day, value: a.n};
  }), {percent: false, height: 170, color: '#2563eb',
      max: Math.max(5, Math.ceil(maxN * 1.2)),
      fmt: function(v){ return String(Math.round(v)); }});
})();
"""
    return page_shell(
        "VibeMemory 四中台门户",
        "数据（底座）+ 评测 + 测试 + 监控 · 针对 VibeMemory 图记忆系统的轻量中台体系（统一数据底座 / 统一门禁 / 四张大屏）",
        body, datetime.now().strftime("%Y-%m-%d %H:%M"), page_key="index",
    ).replace("__DATA__", jdump({"activity": activity})).replace("__PAGEJS__", page_js)


def main() -> int:
    PAGES.mkdir(parents=True, exist_ok=True)
    pages = {
        "index.html": build_index(),
        "data-platform.html": build_data_page(),
        "eval-platform.html": build_eval_page(),
        "test-platform.html": build_test_page(),
        "ops-platform.html": build_ops_page(),
    }
    for name, html in pages.items():
        (PAGES / name).write_text(html, encoding="utf-8")
        print(f"[pages] {name}  {len(html)/1024:.0f} KB")
    print(f"[pages] 输出目录: {PAGES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
