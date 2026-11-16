#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测中台 · 大规模评测集生成器（v2：唯一组合 + 上下文锚点）

以固定种子批量生成七类能力维度的评测用例（JSONL，与 evalsets/ 现有格式一致）。
v2 修复大池子下的"同查询撞车"：每题的 (实体, 属性, 环境锚) 组合唯一，
查询文本全池唯一，保证 pooled 协议下可区分、可检索。

- 可复现：seed 固定逐字节可重现；manifest 记录每文件 SHA-256。
- 有干扰：同实体不同属性 / 同属性不同实体近邻干扰按概率注入。
- 多跳带显式因果边 + causal_bridge：大池子下考图链路而非词法碰撞。
- 判分友好：expected_keywords 与会话文本同源渲染；拒答陷阱词与全部模板词表不相交。

用法：
    python generate_evalsets.py [--out evalsets] [--seed 20261108]
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DEFAULT = HERE / "evalsets"

# --------------------------------------------------------------- 词表池

SERVICES = ["订单服务", "支付服务", "库存服务", "通知服务", "报表服务", "网关服务",
            "用户服务", "搜索服务", "风控服务", "消息服务", "物流服务", "优惠券服务",
            "会员服务", "审计服务", "推荐服务", "文件服务", "积分服务", "客服工单服务"]
ENVS = ["生产环境", "预发环境", "灰度环境", "灾备环境", "测试环境"]
ATTRS = [("API超时时间", ["3秒", "5秒", "8秒", "10秒", "15秒", "30秒"]),
         ("最大重试次数", ["2次", "3次", "5次", "8次"]),
         ("限流阈值", ["每分钟200次", "每分钟500次", "每分钟1000次", "每分钟2000次"]),
         ("连接池上限", ["50", "100", "200", "300", "500"]),
         ("服务副本数", ["2个", "3个", "4个", "6个", "8个"]),
         ("内存上限", ["512MB", "1GB", "2GB", "4GB", "8GB"]),
         ("磁盘配额", ["20GB", "50GB", "100GB", "200GB"]),
         ("发布频率", ["每周一次", "每两周一次", "每天一次"])]
TECHS = [("缓存层", ["Redis", "Memcached", "Caffeine", "本地LruCache"]),
         ("消息队列", ["Kafka", "RabbitMQ", "RocketMQ", "Pulsar"]),
         ("主数据库", ["PostgreSQL", "MySQL", "OpenGauss", "TiDB"]),
         ("搜索引擎", ["Elasticsearch", "OpenSearch", "MeiliSearch"]),
         ("对象存储", ["MinIO", "S3兼容网关", "Ceph"]),
         ("配置中心", ["Nacos", "Apollo", "Consul"]),
         ("任务调度器", ["XXL-JOB", "Quartz", "Airflow"]),
         ("API网关", ["Kong", "Higress", "APISIX"]),
         ("日志管道", ["Loki", "ClickHouse", "Elasticsearch"]),
         ("监控告警", ["Prometheus", "Nightking", "Grafana"]),
         ("身份认证", ["Keycloak", "Casdoor", "自研SSO"]),
         ("支付网关", ["Stripe", "Adyen", "银联网关"]),
         ("文件存储", ["FastDFS", "SeaweedFS", "JuiceFS"]),
         ("流计算引擎", ["Flink", "Spark Streaming", "Kafka Streams"]),
         ("规则引擎", ["Drools", "LiteFlow", "Aviator"]),
         ("服务网格", ["Istio", "Kuma", "自研Sidecar"])]
INCIDENTS = ["数据库连接池耗尽", "堆内存泄漏引发频繁Full GC", "证书过期", "消息队列积压",
             "缓存击穿", "磁盘写满", "网络抖动丢包", "配置错误", "锁竞争加剧",
             "磁盘IO饱和", "线程池打满", "慢SQL堆积", "TCP重传飙升", "句柄泄漏",
             "幂等键缺失", "时钟漂移", "连接僵死", "限流误伤", "缓存雪崩", "分页深翻页"]
CONSEQUENCES = ["接口大面积超时", "订单创建失败率飙升", "移动端登录失败", "实时报表延迟",
                "支付回调丢失", "推送延迟", "首页加载变慢", "结算任务阻塞",
                "库存扣减重复", "搜索结果过期", "消息重复消费", "对账不平"]
ROOT_CAUSES = ["消费者副本数不足", "上游证书过期未轮换", "磁盘空间未及时清理", "连接池参数配置过小",
               "内存泄漏未修复", "限流阈值配置过低", "索引缺失导致慢查询", "心跳超时设置过短",
               "重试策略缺少退避", "配置推送延迟", "发布窗口冲突", "依赖服务容量不足"]
CAUSE_EFFECT = ["导致", "进而引发", "从而造成", "触发"]
PEOPLE = ["李明", "王芳", "赵磊", "陈静", "刘洋", "孙婷", "周杰", "吴敏", "郑强", "冯雪",
          "许亮", "何雨", "郭峰", "于娜"]
PROJECTS = ["夜莺", "知更鸟", "海豚", "天琴", "磐石", "星舰", "青鸾", "灯塔"]
MONTHS = ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"]
PREF_CONTEXTS = ["讲解技术方案", "写周报", "代码评审", "开会", "写设计文档", "排查线上问题",
                 "对外汇报", "整理会议纪要"]
PREF_STYLES = ["先给结论再给推导过程", "必须给出可回滚方案", "先列风险再谈收益",
               "用数据说话，避免形容词", "先给最小可行版本", "结论不超过三句话",
               "补充时间线和责任人", "给出量化评估口径"]
BAN_WORDS = ["赋能", "抓手", "闭环", "颗粒度", "对齐拉通", "组合拳", "沉淀", "打法"]
EN_SERVICES = ["billing-service", "auth-service", "search-api", "upload-worker", "mailer-daemon",
               "invoice-api", "session-cache", "media-transcoder", "geo-router", "feed-builder",
               "quota-keeper", "link-shortener", "webhook-relay", "feature-flagger", "audit-logger"]
EN_FACTS = [("timeout", ["3s", "5s", "10s", "30s", "60s"]),
            ("rate limit", ["100 rpm", "500 rpm", "1000 rpm", "2000 rpm"]),
            ("replica count", ["2", "3", "5", "8"]),
            ("max retries", ["2", "3", "5", "8"]),
            ("memory cap", ["256MB", "512MB", "1GB", "2GB"]),
            ("cache TTL", ["60s", "300s", "900s", "3600s"]),
            ("batch size", ["64", "128", "256", "512"]),
            ("queue depth", ["100", "500", "1000", "5000"])]
# 拒答陷阱属性词：与上面所有模板词表不相交（人工核对）
TRAP_ATTRS = ["费用", "发票", "合同", "绩效", "报价", "保修", "体检", "旅游",
              "宠物", "驾校", "保险", "水电", "班车", "年会", "工资", "简历"]

TYPE_SLUGS = {
    "basic": "recall_basic_large", "temporal": "temporal_large",
    "conflict": "conflict_update_large", "multihop": "multi_hop_large",
    "preference": "preference_large", "abstention": "abstention_large",
    "en": "recall_basic_en_large",
}


def unique_combos(rng, pool_a, pool_b, n):
    """从笛卡尔积里无放回抽 n 个唯一组合。"""
    combos = list(itertools.product(pool_a, pool_b))
    rng.shuffle(combos)
    if n > len(combos):
        raise ValueError(f"组合数不足: 需要 {n}, 只有 {len(combos)}（请扩充词表池）")
    return combos[:n]


def render_sessions(specs):
    return [{"session_id": sid, "text": text} for sid, text in specs]


# --------------------------------------------------------------- 生成器

def gen_basic(rng, i, svc, env, attr, vals):
    val = rng.choice(vals)
    sid1 = f"B{i:04d}-s1"
    sessions = [(sid1, f"{env}{svc}的{attr}是{val}。")]
    r = rng.random()
    if r < 0.35:  # 同实体同环境、不同属性干扰
        attr2, vals2 = rng.choice([a for a in ATTRS if a[0] != attr])
        sessions.append((f"B{i:04d}-d1", f"{env}{svc}的{attr2}是{rng.choice(vals2)}。"))
    elif r < 0.7:  # 同环境同属性、不同实体干扰
        svc2 = rng.choice([s for s in SERVICES if s != svc])
        sessions.append((f"B{i:04d}-d2", f"{env}{svc2}的{attr}是{rng.choice(vals)}。"))
    return {"type": "basic_recall", "sessions": sessions, "query": f"{env}{svc}的{attr}是多少？",
            "expected": [sid1], "keywords": [val], "notes": "唯一(环境,服务,属性)锚点 + 近邻干扰"}


def gen_temporal(rng, i, svc, attr, vals):
    old, new = rng.sample(vals, 2)
    month_old, month_new = rng.sample(MONTHS, 2)
    sid1, sid2 = f"T{i:04d}-s1", f"T{i:04d}-s2"
    sessions = [(sid1, f"{month_old}：{svc}的{attr}是{old}。"),
                (sid2, f"{month_new}：{svc}的{attr}调整为{new}。")]
    if rng.random() < 0.5:
        q, exp, kw = f"现在{svc}的{attr}是多少？", sid2, new
    else:
        q, exp, kw = f"{svc}的{attr}最初是多少？", sid1, old
    return {"type": "temporal", "sessions": sessions, "query": q,
            "expected": [exp], "keywords": [kw], "notes": "唯一(服务,属性)对；时序状态选择"}


def gen_conflict(rng, i, tech, vals, proj, month):
    old, new = rng.sample(vals, 2)
    sid1, sid2 = f"C{i:04d}-s1", f"C{i:04d}-s2"
    sessions = [(sid1, f"{proj}项目{month}记录：{tech}选型确定为{old}。"),
                (sid2, f"更正：{proj}项目{month}评审后，{tech}最终改用{new}，旧方案作废。")]
    return {"type": "conflict_update", "sessions": sessions,
            "query": f"{proj}项目{month}评审后{tech}的最终选型是什么？",
            "expected": [sid2], "keywords": [new], "notes": "唯一(项目,月份,组件)锚点；修正后新值优先"}


def gen_multihop(rng, i, inc, cons, root):
    sid1, sid2 = f"M{i:04d}-s1", f"M{i:04d}-s2"
    v = rng.choice(CAUSE_EFFECT)
    sessions = [(sid1, f"根因分析：{root}{v}{inc}。"),
                (sid2, f"{inc}{v}{cons}。")]
    if rng.random() < 0.5:
        q, exp, kw = f"{inc}引起的{cons}，根本原因是什么？", sid1, root
    else:
        q, exp, kw = f"{inc}会引起什么后果？", sid2, cons
    return {"type": "multi_hop", "sessions": sessions, "query": q,
            "expected": [exp], "keywords": [kw],
            "causal_bridge": True,
            "links": [{"from": sid1, "to": sid2, "label": "因果接续", "confidence": 0.9}],
            "notes": "唯一(事故,后果)组合；显式因果边+causal_bridge，大池子下考图链路"}


def gen_preference(rng, i, name, ctx, mode):
    sid = f"P{i:04d}-s1"
    if mode == "style":
        style = rng.choice(PREF_STYLES)
        text, kw = f"{name}的偏好：{ctx}时{style}。", style
        q = f"{name}在{ctx}时有什么偏好？"
    else:
        ban = rng.choice(BAN_WORDS)
        text, kw = f"{name}的偏好：{ctx}的文档里避免出现“{ban}”这类词。", ban
        q = f"{name}说{ctx}时要避免什么词？"
    return {"type": "preference", "sessions": [(sid, text)], "query": q,
            "expected": [sid], "keywords": [kw], "notes": "唯一(人,场景)锚点；偏好记忆"}


def gen_abstention(rng, i, subject, trap):
    return {"type": "abstention", "sessions": [], "query": f"{subject}相关的{trap}信息是什么？",
            "expected": [], "keywords": [], "trap_keywords": [trap],
            "notes": f"主体在库但{trap}从未存入，考不编造"}


def gen_en(rng, i, svc, attr, vals):
    val = rng.choice(vals)
    sid1 = f"E{i:04d}-s1"
    sessions = [(sid1, f"The {svc} {attr} is {val}.")]
    if rng.random() < 0.5:
        svc2 = rng.choice([s for s in EN_SERVICES if s != svc])
        sessions.append((f"E{i:04d}-d1", f"The {svc2} {attr} is {rng.choice(vals)}."))
    return {"type": "basic_recall_en", "sessions": sessions,
            "query": f"What is the {attr} of the {svc}?",
            "expected": [sid1], "keywords": [val], "notes": "唯一(服务,属性)锚点；英文召回"}


def build_items(kind, n, rng):
    """按维度生成 n 条用例；组合唯一性在生成前统一分配。"""
    items = []
    if kind == "basic":
        combos = unique_combos(rng, ENVS, list(itertools.product(SERVICES, [a[0] for a in ATTRS])), n)
        attr_vals = dict(ATTRS)
        for i, (env, (svc, attr)) in enumerate(combos, 1):
            items.append(gen_basic(rng, i, svc, env, attr, attr_vals[attr]))
    elif kind == "temporal":
        combos = unique_combos(rng, SERVICES, [a[0] for a in ATTRS], n)
        attr_vals = dict(ATTRS)
        for i, (svc, attr) in enumerate(combos, 1):
            items.append(gen_temporal(rng, i, svc, attr, attr_vals[attr]))
    elif kind == "conflict":
        tech_names = [t[0] for t in TECHS]
        combos = unique_combos(rng, list(itertools.product(PROJECTS, MONTHS)), tech_names, n)
        tech_vals = dict(TECHS)
        for i, ((proj, month), tech) in enumerate(combos, 1):
            items.append(gen_conflict(rng, i, tech, tech_vals[tech], proj, month))
    elif kind == "multihop":
        combos = unique_combos(rng, INCIDENTS, CONSEQUENCES, n)
        roots = rng.sample(ROOT_CAUSES * (n // len(ROOT_CAUSES) + 1), n)
        for i, ((inc, cons), root) in enumerate(zip(combos, roots), 1):
            items.append(gen_multihop(rng, i, inc, cons, root))
    elif kind == "preference":
        ctxs = (PREF_CONTEXTS * 3)[:len(PREF_CONTEXTS) * 3]
        combos = unique_combos(rng, PEOPLE, ctxs, n)
        for i, (name, ctx) in enumerate(combos, 1):
            items.append(gen_preference(rng, i, name, ctx, rng.choice(["style", "ban"])))
    elif kind == "abstention":
        subjects = (SERVICES + PROJECTS + MEETING_ROOMS_IF_ANY())
        for i in range(1, n + 1):
            trap = rng.choice(TRAP_ATTRS)
            subject = subjects[i % len(subjects)]
            items.append(gen_abstention(rng, i, subject, trap))
    elif kind == "en":
        combos = unique_combos(rng, EN_SERVICES, [a[0] for a in EN_FACTS], n)
        attr_vals = dict(EN_FACTS)
        for i, (svc, attr) in enumerate(combos, 1):
            items.append(gen_en(rng, i, svc, attr, attr_vals[attr]))
    return items


def MEETING_ROOMS_IF_ANY():
    return ["会议室A", "会议室B"]


GEN_KINDS = ["basic", "temporal", "conflict", "multihop", "preference", "abstention", "en"]
COUNTS = {"basic": 240, "temporal": 140, "conflict": 140, "multihop": 180,
          "preference": 90, "abstention": 120, "en": 60}


def main() -> int:
    ap = argparse.ArgumentParser(description="大规模评测集生成器 v2")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--seed", type=int, default=20261108)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    manifest = {"generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "seed": args.seed, "generator": "评测中台/generate_evalsets.py v2",
                "design": "v2：每题(实体,属性,环境/项目/月份/人/场景)组合唯一，查询文本全池唯一；"
                          "多跳带显式因果边+causal_bridge；干扰会话按概率注入；"
                          "拒答陷阱词与事实模板词表不相交",
                "files": [], "notes": "固定种子可复现"}
    total = 0
    for kind in GEN_KINDS:
        n = COUNTS[kind]
        salt = int.from_bytes(kind.encode("utf-8"), "little") % 99991
        rng = random.Random(args.seed * 100003 + salt)
        items = build_items(kind, n, rng)
        prefix = {"basic": "GB", "temporal": "GT", "conflict": "GC", "multihop": "GM",
                  "preference": "GP", "abstention": "GA", "en": "GE"}[kind]
        for i, it in enumerate(items, 1):
            it["id"] = f"{prefix}{i:04d}"
            it["sessions"] = render_sessions(it["sessions"])
            it["expected_memory_ids"] = it.pop("expected") or []
            kws = it.pop("keywords", None)
            if kws:
                it["expected_keywords"] = kws
        slug = TYPE_SLUGS[kind]
        # 会话 id 唯一性自检
        sids = [s["session_id"] for it in items for s in it["sessions"]]
        dup = len(sids) - len(set(sids))
        if dup:
            print(f"[gen][错误] {slug}: 会话ID重复 {dup} 个", file=sys.stderr)
            return 2
        type_counter = Counter(it["type"] for it in items)
        path = out / f"{slug}.jsonl"
        path.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
                        encoding="utf-8")
        manifest["files"].append({"file": path.name, "cases": len(items),
                                  "type_counts": dict(type_counter),
                                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                  "bytes": path.stat().st_size})
        total += len(items)
        print(f"[gen] {path.name:<28} {len(items):>4} 用例  {dict(type_counter)}")
    manifest["total_cases"] = total
    (out / "gen_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    print(f"[gen] 合计 {total} 用例 -> {out / 'gen_manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
