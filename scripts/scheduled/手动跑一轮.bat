@echo off
rem 数据中台 · 手动跑一轮定时产出（评测/测试/监控 + 同步服务器）
rem 双击即可；幂等——当天已跑过的件会自动跳过
chcp 65001 >nul
title VibeMemory 数据中台 - 手动产出
"C:\Users\ASYS\Desktop\VibeMemory\.venv\Scripts\python.exe" -u "C:\Users\ASYS\Desktop\VibeMemory-中台\scripts\scheduled\daily_runs.py"
echo.
echo ===== 完成，日志见 VibeMemory-中台\logs\ =====
pause
