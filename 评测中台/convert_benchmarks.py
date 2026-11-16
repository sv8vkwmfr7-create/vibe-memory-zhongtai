#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测中台 · 公开基准数据集转换器

把 datasets/ 下的公开基准转换为评测中台的 JSONL 评测集（benchmarks/ 目录）。

协议说明（诚实边界）：
- 会话级证据协议：把官方 turn 级证据（LoCoMo dia_id / LongMemEval answer_session_ids）
  归并到其所属会话，判分口径为"Top-K 是否命中证据会话"。这与仓库
  experiments/locomo_retrieval.py（turn 级）不同：本转换器为会话级，成本更低、
  适合回归门禁；论文级 turn 级结论仍以仓库适配器为准。
- LoCoMo：每段对话一个评测集文件（对话之间相互隔离，需逐文件运行）；
  时间锚点以文本日期前缀注入（[YYYY-MM-DD]），不修改 SDK 的 created_at。
- LongMemEval：每题独立记忆库（isolate: true，逐题建 :memory: 实例）；
  _abs 弃答题不纳入本转换（其判分需要答案级协议），数量记录在清单。

用法：
    python convert_benchmarks.py locomo  [--conv 1-10]      # 全部 10 段
    python convert_benchmarks.py longmemeval --max-questions 30
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASETS = HERE / "datasets"
BENCH = HERE / "benchmarks"
LOCOMO_TYPE = {1: "locomo_single_hop", 2: "locomo_multi_hop",
               3: "locomo_temporal", 4: "locomo_open_domain"}


LINE_SEP = re.compile(r"[\u2028\u2029\u0085\v\f\x1c-\x1e\r]")


def sanitize(text: str) -> str:
    """把 Unicode 行分隔符/杂控制字符替换为空格。

    这些字符 json.dumps 不转义（JSON 合法），但按行读取评测集时会被
    splitlines() 拆断 JSON，必须从源头净化。
    """
    return LINE_SEP.sub(" ", text or "")


def parse_date(s: str) -> str:
    """'1:56 pm on 8 May, 2023' -> '2023-05-08'（失败返回原文）。"""
    m = re.search(r"(\d+)\s+\w+,\s*(\d{4})", s or "")
    if not m:
        return (s or "")[:10]
    months = {name: i for i, name in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], 1)}
    day, mon, year = int(m.group(1)), m.group(0).split()[1], int(m.group(2))
    mi = months.get(mon.strip())
    return f"{year:04d}-{mi or 1:02d}-{day:02d}"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def convert_locomo(conv_range: str) -> int:
    src = DATASETS / "locomo" / "locomo10.json"
    data = json.loads(src.read_text(encoding="utf-8-sig"))
    BENCH.mkdir(parents=True, exist_ok=True)
    lo, hi = (int(x) for x in conv_range.split("-"))
    total_items = 0
    written = []
    for idx, sample in enumerate(data, start=1):
        if not (lo <= idx <= hi):
            continue
        conv = sample["conversation"]
        session_keys = sorted(
            (k for k in conv if re.fullmatch(r"session_\d+", k)),
            key=lambda k: int(k.split("_")[1]))
        session_ids: dict[str, str] = {}   # dia_id -> 会话ID
        sessions_out: list[dict] = []
        for k in session_keys:
            n = int(k.split("_")[1])
            sid = f"c{idx:02d}-s{n}"
            date = parse_date(conv.get(f"{k}_date_time", ""))
            lines = [f"[{date}] {t.get('speaker','')}: {sanitize(t.get('text',''))}" for t in conv[k]]
            sessions_out.append({"session_id": sid, "text": "\n".join(lines)})
            for t in conv[k]:
                session_ids[t.get("dia_id")] = sid
        items, skipped = [], {"no_evidence": 0, "missing_evidence": 0, "adversarial": 0}
        qas = sample.get("qa") or []
        for qi, qa in enumerate(qas):
            cat = qa.get("category")
            ev = [e for e in (qa.get("evidence") or []) if e in session_ids]
            if cat == 5 or not (qa.get("evidence") or []):
                skipped["adversarial" if cat == 5 else "no_evidence"] += 1
                continue
            if len(ev) != len(qa.get("evidence") or []):
                skipped["missing_evidence"] += 1
                continue
            typ = LOCOMO_TYPE.get(cat)
            if typ is None:
                skipped["missing_evidence"] += 1
                continue
            expected = sorted({session_ids[e] for e in ev})
            item = {"id": f"LC{idx:02d}-Q{qi:03d}", "type": typ,
                    "sessions": sessions_out if not items else [],
                    "query": sanitize(qa.get("question", "")),
                    "expected_memory_ids": expected,
                    "notes": f"LoCoMo conv{idx} category={cat}; 会话级证据协议"}
            items.append(item)
        if not items:
            print(f"[locomo] conv{idx}: 无可用 QA，跳过")
            continue
        out = BENCH / f"locomo_c{idx:02d}.jsonl"
        out.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
                       encoding="utf-8")
        total_items += len(items)
        written.append({"file": out.name, "cases": len(items), "skipped": skipped,
                        "sessions": len(sessions_out), "sha256": sha256_file(out)})
        print(f"[locomo] {out.name}: {len(items)} 题（跳过 {skipped}），"
              f"{len(sessions_out)} 会话")
    manifest_path = BENCH / "locomo_manifest.json"
    manifest = {
        "dataset": "LoCoMo locomo10.json",
        "source_sha256": sha256_file(src),
        "protocol": "会话级证据检索（turn 级证据归并到会话；时间锚点为文本日期前缀）",
        "isolation": "每段对话一个评测集文件，对话间相互隔离（逐文件运行）",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_items": total_items, "files": written,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[locomo] 合计 {total_items} 题 -> {manifest_path.name}")
    return 0


def convert_longmemeval(max_questions: int) -> int:
    src = DATASETS / "longmemeval_s" / "longmemeval_s_cleaned.json"
    print("[longmemeval] 加载 265MB 数据集（约需数十秒）...")
    cases = json.loads(src.read_text(encoding="utf-8-sig"))
    BENCH.mkdir(parents=True, exist_ok=True)
    answerable = [c for c in cases if not c["question_id"].endswith("_abs")]
    abs_count = len(cases) - len(answerable)
    picked = answerable[:max_questions]
    items = []
    for c in picked:
        qid = c["question_id"]
        sessions_out = []
        for si, turns in enumerate(c["haystack_sessions"]):
            raw_sid = (c.get("haystack_session_ids") or ["", "", ""])[si] or f"s{si}"
            sid = f"{qid}:{raw_sid}"
            date = (c.get("haystack_dates") or [""] * len(c["haystack_sessions"]))[si]
            text = sanitize("\n".join(f"[{date}] {t.get('role','')}: {t.get('content','')}" for t in turns))
            sessions_out.append({"session_id": sid, "text": text})
        expected = [f"{qid}:{s}" for s in (c.get("answer_session_ids") or [])]
        items.append({"id": f"LME-{qid}", "type": f"longmemeval_{c.get('question_type','unknown')}",
                      "isolate": True, "sessions": sessions_out,
                      "query": sanitize(c.get("question", "")),
                      "expected_memory_ids": expected,
                      "notes": f"LongMemEval-S 清理版；question_type={c.get('question_type')}"})
    out = BENCH / f"longmemeval_s_subset{len(items)}.jsonl"
    out.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
                   encoding="utf-8")
    manifest = {
        "dataset": "LongMemEval-S cleaned (xiaowu0162/longmemeval-cleaned)",
        "source_sha256": sha256_file(src),
        "protocol": "会话级证据检索；每题独立 :memory: 实例（isolate），会话文本带日期前缀",
        "isolation": "逐题隔离（isolate: true）",
        "subset": f"官方顺序前 {len(items)} 题可作答题（_abs 弃答题 {abs_count} 道未纳入，"
                  f"其判分需答案级协议）",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_items": len(items), "sha256": sha256_file(out),
    }
    (BENCH / f"longmemeval_s_subset{len(items)}_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[longmemeval] {out.name}: {len(items)} 题（_abs 跳过 {abs_count}）")
    return 0


def _locomo_sessions(conv: dict, idx: int):
    """对话 → (sessions_out, dia_id->会话ID 映射)。convert_locomo/convert_adversarial 共用。"""
    session_keys = sorted(
        (k for k in conv if re.fullmatch(r"session_\d+", k)),
        key=lambda k: int(k.split("_")[1]))
    session_ids: dict[str, str] = {}
    sessions_out: list[dict] = []
    for k in session_keys:
        n = int(k.split("_")[1])
        sid = f"c{idx:02d}-s{n}"
        date = parse_date(conv.get(f"{k}_date_time", ""))
        lines = [f"[{date}] {t.get('speaker','')}: {sanitize(t.get('text',''))}" for t in conv[k]]
        sessions_out.append({"session_id": sid, "text": "\n".join(lines)})
        for t in conv[k]:
            session_ids[t.get("dia_id")] = sid
    return sessions_out, session_ids


def convert_adversarial(conv_range: str) -> int:
    """LoCoMo category 5 对抗题 → locomo_adversarial.jsonl（答案级协议，带参考答案）。"""
    src = DATASETS / "locomo" / "locomo10.json"
    data = json.loads(src.read_text(encoding="utf-8-sig"))
    BENCH.mkdir(parents=True, exist_ok=True)
    lo, hi = (int(x) for x in conv_range.split("-"))
    items, no_ref = [], 0
    for idx, sample in enumerate(data, start=1):
        if not (lo <= idx <= hi):
            continue
        sessions_out, session_ids = _locomo_sessions(sample["conversation"], idx)
        for qi, qa in enumerate(sample.get("qa") or []):
            if qa.get("category") != 5:
                continue
            ref = qa.get("adversarial_answer") or qa.get("answer")
            if not ref:
                no_ref += 1
                continue
            ev = [e for e in (qa.get("evidence") or []) if e in session_ids]
            expected = sorted({session_ids[e] for e in ev})
            items.append({"id": f"LCA{idx:02d}-Q{qi:03d}", "type": "locomo_adversarial",
                          "sessions": sessions_out if not items else [],
                          "query": sanitize(qa.get("question", "")),
                          "answer": sanitize(str(ref)),
                          "expected_memory_ids": expected,
                          "notes": f"LoCoMo conv{idx} category=5 对抗题；答案级 LLM-judge 协议"})
    out = BENCH / "locomo_adversarial.jsonl"
    out.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
                   encoding="utf-8")
    manifest = {
        "dataset": "LoCoMo locomo10.json category=5",
        "source_sha256": sha256_file(src),
        "protocol": "答案级 LLM-judge 对抗协议：问题含错误前提，adversarial_answer 是误导性答案（诱饵），"
                    "正确行为=依据证据纠正前提；采纳诱饵=wrong",
        "isolation": "每段对话一个来源（sessions 首条携带）",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_items": len(items), "no_reference": no_ref, "sha256": sha256_file(out),
    }
    (BENCH / "locomo_adversarial_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[adversarial] {out.name}: {len(items)} 题（无参考答案跳过 {no_ref}）")
    return 0


def convert_abs() -> int:
    """LongMemEval _abs 拒答题 → longmemeval_abs.jsonl（30 题，expected_abstain 协议）。"""
    src = DATASETS / "longmemeval_s" / "longmemeval_s_cleaned.json"
    print("[abs] 加载 265MB 数据集（约需数十秒）...")
    cases = json.loads(src.read_text(encoding="utf-8-sig"))
    abs_cases = [c for c in cases if c["question_id"].endswith("_abs")]
    BENCH.mkdir(parents=True, exist_ok=True)
    items = []
    for c in abs_cases:
        qid = c["question_id"]
        sessions_out = []
        for si, turns in enumerate(c["haystack_sessions"]):
            raw_sid = (c.get("haystack_session_ids") or ["", "", ""])[si] or f"s{si}"
            sid = f"{qid}:{raw_sid}"
            date = (c.get("haystack_dates") or [""] * len(c["haystack_sessions"]))[si]
            text = sanitize("\n".join(f"[{date}] {t.get('role','')}: {t.get('content','')}" for t in turns))
            sessions_out.append({"session_id": sid, "text": text})
        items.append({"id": f"LMEABS-{qid}", "type": "longmemeval_abstain",
                      "isolate": True, "sessions": sessions_out,
                      "query": sanitize(c.get("question", "")),
                      "answer": sanitize(str(c.get("answer", ""))),
                      "expected_abstain": True,
                      "notes": f"LongMemEval-S 拒答题（haystack 内无该信息，正确行为是弃答）；"
                               f"question_type={c.get('question_type')}"})
    out = BENCH / "longmemeval_abs.jsonl"
    out.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
                   encoding="utf-8")
    manifest = {
        "dataset": "LongMemEval-S cleaned（_abs 弃答题全量）",
        "source_sha256": sha256_file(src),
        "protocol": "答案级 LLM-judge：被测系统基于召回会话生成回答，判定是否恰当弃答（对比参考弃答话术）",
        "isolation": "逐题隔离（isolate: true）",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_items": len(items), "sha256": sha256_file(out),
    }
    (BENCH / "longmemeval_abs_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[abs] {out.name}: {len(items)} 题")
    return 0


def convert_personamem() -> int:
    """PersonaMem v2 本地缓存（persona 988/730/782）→ personamem_val9.jsonl。

    协议：偏好问答（问法隐含用户画像/历史偏好），正确答案在 val.csv；
    会话 = chat_history 按 10 turns 分块（历史远超 LLM 窗口，走召回协议）；
    证据 = related_conversation_snippet 文本定位到分块（能定位的才有 expected_memory_ids）。
    规模说明：本地缓存仅覆盖 3 个 persona / 9 题；全量 2,061 题需下载完整
    chat_history_32k 数据（~500 persona，许可证未核实），另行决策。
    """
    import ast

    src_dir = DATASETS / "personamem_v2"
    val_path = src_dir / "val.csv"
    import csv
    rows = list(csv.DictReader(val_path.open(encoding="utf-8-sig", newline="")))
    personas = sorted({r["persona_id"] for r in rows} & {"988", "782", "730"})
    BENCH.mkdir(parents=True, exist_ok=True)
    CHUNK_TURNS = 10

    items = []
    for pid in personas:
        hist = json.loads((src_dir / f"history{pid}.json").read_text(encoding="utf-8-sig"))
        turns = hist.get("chat_history") or []
        # 分块：每 10 turns 一个会话（历史远超 LLM 窗口，必须走召回）
        chunks = []
        for ci in range(0, len(turns), CHUNK_TURNS):
            part = turns[ci:ci + CHUNK_TURNS]
            text = sanitize("\n".join(f"[{t.get('role','')}] {t.get('content','')}" for t in part))
            chunks.append({"session_id": f"p{pid}-s{ci // CHUNK_TURNS + 1:02d}", "text": text,
                           "_raw": "\n".join(str(t.get("content", "")) for t in part)})
        for r in rows:
            if r["persona_id"] != pid:
                continue
            try:
                q = ast.literal_eval(r["user_query"])
                query = str(q.get("content", "")).strip()
            except Exception:
                query = r["user_query"]
            if not query:
                continue
            snippet = " ".join(
                str(t.get("content", "")) for t in json.loads(r["related_conversation_snippet"])
            ) if r.get("related_conversation_snippet", "").startswith("[") else \
                r.get("related_conversation_snippet", "")
            expected = [c["session_id"] for c in chunks
                        if snippet and snippet[:80] and snippet[:80] in c["_raw"]]
            items.append({"id": f"PM-{pid}-{len(items) + 1:02d}",
                          "type": f"personamem_{r['pref_type']}",
                          "sessions": chunks if not any(i.get("sessions") for i in items
                                                        if i['id'].startswith(f'PM-{pid}')) else [],
                          "query": sanitize(query),
                          "answer": sanitize(r["correct_answer"]),
                          "expected_memory_ids": expected,
                          "notes": f"PersonaMem v2 persona{pid} pref_type={r['pref_type']}; "
                                   f"updated={r['updated']}; 个性化偏好问答（答案级协议）"})
    out = BENCH / "personamem_val.jsonl"
    out.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
                   encoding="utf-8")
    manifest = {
        "dataset": "PersonaMem v2 本机缓存（persona 988/730/782）",
        "source_sha256": sha256_file(val_path),
        "protocol": "答案级 LLM-judge：个性化偏好问答（召回分块历史 → 生成 → 对比正确答案判分）",
        "isolation": "pooled（同 persona 共享分块历史；分块 10 turns/会话）",
        "scale_note": "本地缓存仅覆盖 9 题/3 persona；全量 2,061 题需完整 chat_history_32k 数据"
                      "（~500 persona，许可证未核实），规模扩展另行决策",
        "pref_type_coverage": sorted({r["type"].replace("personamem_", "")
                                      for r in items if r["type"].startswith("personamem_")}),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_items": len(items), "sha256": sha256_file(out),
    }
    (BENCH / "personamem_val_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[personamem] {out.name}: {len(items)} 题（3 persona，"
          f"pref_type 覆盖 {len(manifest['pref_type_coverage'])}/7）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="公开基准转换器")
    ap.add_argument("target", choices=["locomo", "longmemeval", "adversarial", "abs", "personamem"])
    ap.add_argument("--conv", default="1-10", help="LoCoMo 对话编号范围，如 1-3")
    ap.add_argument("--max-questions", type=int, default=30, help="LongMemEval 抽题数")
    args = ap.parse_args()
    if args.target == "locomo":
        return convert_locomo(args.conv)
    if args.target == "longmemeval":
        return convert_longmemeval(args.max_questions)
    if args.target == "adversarial":
        return convert_adversarial(args.conv)
    if args.target == "personamem":
        return convert_personamem()
    return convert_abs()


if __name__ == "__main__":
    sys.exit(main())
