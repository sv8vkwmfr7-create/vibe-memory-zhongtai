#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据中台 · 定时产出引擎（验收标准第 3 条「数据自己长」的落地）

每天一轮：
  ① 评测  run_eval.py --all            → 评测中台/runs/run-*.json
  ② 测试  run_tests.py --scope full    → 测试中台/runs/test-*.json（+ 仓库 coverage.xml）
  ③ 监控  collect_runtime_metrics.py   → 监控中台/runs/obs-*.json
  ④ 同步  当日新增存档 scp → instabox 服务器，并触发大屏重建（POST /api/refresh）

幂等：某类存档当天已有产出则自动跳过该件（防计划任务重复触发/手动重复跑）；
同步失败不阻断（数据留在本机，下一轮补传）。

用法：
    python daily_runs.py             # 正常轮次（幂等跳过）
    python daily_runs.py --dry-run   # 只打印计划，不执行
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                  # VibeMemory-中台（scheduled → scripts → 根）
LOGS = ROOT / "logs"
MARKER = LOGS / ".sync_state.json"
REPO = Path("C:/Users/ASYS/Desktop/VibeMemory")
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"

SSH_PORT = "2222"
SSH_TARGET = "ibssh_dwcyxcxj4istw7cc5rmkov4dsy@ssh.instabox.sh"
REMOTE_ROOT = "/data/home/VibeMemory-中台"

RUN_DIRS = {                              # 相对路径：本机 runs 目录 → 服务器对应目录
    "评测中台/runs": "评测中台/runs",
    "测试中台/runs": "测试中台/runs",
    "监控中台/runs": "监控中台/runs",
}

# 新接入项目的数据同步（消除手动 scp）：只扫顶层文件（与 collect 口径一致，
# 增量按 marker 过滤——项目无变化时 0 文件，秒过）。注意 知识库 含巨大子目录，禁止递归。
PROJECT_DATA_DIRS = [
    (Path("C:/Users/ASYS/Desktop/知识库"), "/data/home/projects/知识库"),
    (Path("C:/Users/ASYS/Desktop/codex-zcode-team"), "/data/home/projects/codex-zcode-team"),
]


LOG_F = None  # main() 里赋值；log() 同时落文件，计划任务无控制台也能看到「跳过/同步」行


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if LOG_F is not None:
        LOG_F.write(line + "\n")
        LOG_F.flush()


def find_tool(name: str) -> str:
    import shutil
    p = shutil.which(name)
    if p:
        return p
    fallback = Path("C:/Windows/System32/OpenSSH") / f"{name}.exe"
    if fallback.exists():
        return str(fallback)
    raise SystemExit(f"[daily] 找不到 {name}（需要 Windows OpenSSH 客户端）")


def today_prefix() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def ran_today(run_dir: Path) -> bool:
    """该中台今天是否已有新存档（幂等判断，不看文件名只看 mtime）。"""
    if not run_dir.exists():
        return False
    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day).timestamp()
    return any(p.stat().st_mtime >= midnight for p in run_dir.glob("*.json"))


def run_step(name: str, cmd: list[str], cwd: Path, dry: bool, log_f) -> bool:
    head = f"===== {name} ====="
    log(head)
    log_f.write(head + "\n")
    if dry:
        log(f"(dry-run) {' '.join(str(c) for c in cmd)}  (cwd={cwd})")
        return True
    proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=1800)
    out = (proc.stdout or "") + (proc.stderr or "")
    log_f.write(out[-4000:] + "\n")
    tail = out.strip().splitlines()[-5:] if out.strip() else []
    for ln in tail:
        log("  " + ln)
    if proc.returncode != 0:
        log(f"  ⚠️ {name} 返回码 {proc.returncode}（继续后续步骤）")
        return False
    return True


def load_marker() -> float:
    try:
        import json
        return float(json.loads(MARKER.read_text(encoding="utf-8"))["last_sync"])
    except Exception:
        return 0.0


def save_marker(ts: float) -> None:
    import json
    MARKER.write_text(json.dumps({"last_sync": ts}, ensure_ascii=False), encoding="utf-8")


def pending_files() -> list[tuple[Path, str]]:
    """上次同步后新增/变更的存档 → (本机路径, 服务器目标目录)。"""
    since = load_marker()
    out = []
    for local_rel, remote_rel in RUN_DIRS.items():
        d = ROOT / local_rel
        if not d.exists():
            continue
        for p in d.iterdir():
            if p.is_file() and p.suffix.lower() in (".json", ".xml") and p.stat().st_mtime > since:
                out.append((p, remote_rel))
    for local_dir, remote_dir in PROJECT_DATA_DIRS:
        if not local_dir.exists():
            continue
        for p in local_dir.iterdir():        # 只顶层，不递归（知识库子目录巨大）
            if p.is_file() and p.stat().st_mtime > since:
                out.append((p, remote_dir))
    return out


def sync_to_server(log_f) -> bool:
    scp, ssh = find_tool("scp"), find_tool("ssh")
    files = pending_files()
    if not files:
        log("同步：无新增存档")
        return True
    log(f"同步：{len(files)} 个存档 → 服务器")
    ok = True
    for p, remote_rel in files:
        # runs 是相对 REMOTE_ROOT 的相对路径；项目数据目录本身就是绝对远端路径
        remote_dir = remote_rel if remote_rel.startswith("/") else f"{REMOTE_ROOT}/{remote_rel}"
        r = subprocess.run([scp, "-P", SSH_PORT, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                            str(p), f"{SSH_TARGET}:{remote_dir}/"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            ok = False
            log_f.write(f"scp fail {p.name}: {r.stderr[-300:]}\n")
            break
    if not ok:
        log("同步：scp 失败（服务器可能睡眠），下轮补传")
        return False
    r = subprocess.run([ssh, "-p", SSH_PORT, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                        SSH_TARGET, "curl -s -X POST http://localhost:8080/api/refresh | head -c 120"],
                       capture_output=True, text=True, timeout=600)
    log(f"同步：scp 完成，大屏重建触发 → {(r.stdout or '').strip()[:80] or '(无响应)'}")
    save_marker(datetime.now().timestamp())
    return True


def main() -> int:
    global LOG_F
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    LOGS.mkdir(exist_ok=True)
    log_path = LOGS / f"scheduled_{today_prefix()}.log"
    with log_path.open("a", encoding="utf-8") as log_f:
        LOG_F = log_f
        log_f.write(f"\n===== 定时产出 {datetime.now().isoformat(timespec='seconds')} =====\n")
        log(f"定时产出开始（dry-run={args.dry_run}），日志：{log_path}")

        if ran_today(ROOT / "评测中台" / "runs"):
            log("① 评测：今天已有存档，跳过")
        else:
            run_step("① 评测 run_eval.py --all",
                     [VENV_PY, "run_eval.py", "--all"], ROOT / "评测中台", args.dry_run, log_f)
        if ran_today(ROOT / "测试中台" / "runs"):
            log("② 测试：今天已有存档，跳过")
        else:
            run_step("② 测试 run_tests.py --scope full",
                     [VENV_PY, "run_tests.py", "--scope", "full"], ROOT / "测试中台", args.dry_run, log_f)
        if ran_today(ROOT / "监控中台" / "runs"):
            log("③ 监控：今天已有存档，跳过")
        else:
            run_step("③ 监控 collect_runtime_metrics.py",
                     [VENV_PY, "collect_runtime_metrics.py"], ROOT / "监控中台", args.dry_run, log_f)

        if not args.dry_run:
            sync_to_server(log_f)
        log("定时产出结束")
        log_f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
