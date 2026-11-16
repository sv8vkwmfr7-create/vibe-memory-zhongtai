#!/bin/bash
# VibeMemory 四中台 恢复脚本（instabox · systemd 用户服务版）
# 2026-11-09 改进：隧道恢复改为「健康检查驱动」——旧逻辑看日志里有没有 URL，
# 会被 trycloudflare 回收隧道（Unauthorized: Tunnel not found）卡死；现在先 curl
# 旧地址，通就复用，不通才杀进程重开（每次会拿到新 URL）。
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user start zhongtai.service archive.service keepalive.service autopull.service

recover_tunnel() {  # $1=日志文件 $2=本地端口
  local url
  url=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$1" 2>/dev/null | tail -1)
  if [ -n "$url" ] && curl -s -o /dev/null --max-time 8 "$url"; then
    echo "[tunnel:$2] 复用现有隧道 $url"
  else
    pkill -f "cloudflared tunnel --url http://localhost:$2" 2>/dev/null   # 只杀对应端口的隧道（脚本文件不含该字符串，不会误杀自身）
    sleep 1
    : > "$1"
    setsid nohup /data/home/cloudflared tunnel --url "http://localhost:$2" --no-autoupdate </dev/null > "$1" 2>&1 &
  fi
}
recover_tunnel /data/home/cloudflared.log 8080
recover_tunnel /data/home/cloudflared-8081.log 8081
sleep 8
echo "服务状态:"; systemctl --user --no-pager --lines=0 list-units "zhongtai*" "archive*" "keepalive*" "autopull*" | grep -E "active|failed"
echo "大屏:  $(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" /data/home/cloudflared.log | tail -1)"
echo "全档:  $(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" /data/home/cloudflared-8081.log | tail -1)"
echo "数据API 示例:  $(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" /data/home/cloudflared.log | tail -1)/api/data/overview"
