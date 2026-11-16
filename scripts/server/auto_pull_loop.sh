#!/bin/bash
# GitHub 轮询循环：每 15 分钟 fetch 一次，发现新提交即跑数据流水线
while true; do
  cd /data/home/vibe-memory
  git fetch -q origin 2>/dev/null
  L=$(git rev-parse HEAD 2>/dev/null)
  R=$(git rev-parse origin/main 2>/dev/null)
  if [ -n "$L" ] && [ -n "$R" ] && [ "$L" != "$R" ]; then
    echo "[$(date '+%F %T')] 检测到新提交 $R，运行流水线" >> /data/home/pipeline.log
    bash /data/home/VibeMemory-中台/scripts/server/server_pipeline.sh >> /data/home/pipeline.log 2>&1
    echo "[$(date '+%F %T')] 流水线结束" >> /data/home/pipeline.log
  fi
  sleep 900
done
