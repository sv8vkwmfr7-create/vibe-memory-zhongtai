#!/bin/bash
# 持续轻载保活 + 服务失联自愈（2026-11-09 升级）
# 每 10 秒自访问 8080/8081（CPU ~1%，防 instabox 睡眠）；
# 连续 6 次失败（≈1 分钟）自动重启对应服务并记 signals.log；
# 每 10 秒写 service_heartbeat.json 供 signals.py 检测服务失联信号。
FAIL_8080=0
FAIL_8081=0
N=0
HB=/data/home/VibeMemory-中台/数据中台/assets/service_heartbeat.json
mkdir -p "$(dirname "$HB")"
while true; do
  DOWN=""
  if curl -s -m 2 -o /dev/null http://127.0.0.1:8080/index.html; then
    FAIL_8080=0
  else
    FAIL_8080=$((FAIL_8080+1))
    DOWN="$DOWN 8080"
    if [ $FAIL_8080 -ge 6 ]; then
      echo "[$(date '+%F %T')] 8080 连续 $FAIL_8080 次失联，自动重启 zhongtai" >> /data/home/signals.log
      systemctl --user restart zhongtai.service
      FAIL_8080=0
    fi
  fi
  if curl -s -m 2 -o /dev/null http://127.0.0.1:8081/; then
    FAIL_8081=0
  else
    FAIL_8081=$((FAIL_8081+1))
    DOWN="$DOWN 8081"
    if [ $FAIL_8081 -ge 6 ]; then
      echo "[$(date '+%F %T')] 8081 连续 $FAIL_8081 次失联，自动重启 archive" >> /data/home/signals.log
      systemctl --user restart archive.service
      FAIL_8081=0
    fi
  fi
  printf '{"ts": "%s", "down": [%s]}\n' "$(date -Iseconds)" \
    "$(for d in $DOWN; do printf '"%s",' "$d"; done | sed 's/,$//')" > "$HB.tmp" && mv "$HB.tmp" "$HB"
  touch /data/home/.keepalive
  N=$((N+1))
  if [ $((N % 6)) -eq 0 ]; then date "+%F %T 持续轻载心跳" >> /data/home/keepalive.log; fi
  sleep 10
done
