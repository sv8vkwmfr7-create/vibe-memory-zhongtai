#!/bin/bash
# 服务器端数据流水线：git pull → 采集 → 加工 → 质量检查 → 重建大屏 → 异常信号
cd /data/home/vibe-memory && git pull --ff-only -q
cd /data/home/VibeMemory-中台
export PYTHONIOENCODING=utf-8
python3 数据中台/collect.py --repo /data/home/vibe-memory
python3 数据中台/build_assets.py
python3 数据中台/quality.py
python3 scripts/build_pages.py
python3 数据中台/signals.py || true
echo "== 流水线完成 $(date '+%F %T') =="
