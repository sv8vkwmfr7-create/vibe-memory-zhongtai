# VibeMemory 开发中使用四中台

本流程让中台为 VibeMemory 的开发提供基线、验证结果和历史对照。实际代码以当前检出的仓库及其未提交改动为准。

1. 开发前检查 `C:/Users/ASYS/Desktop/VibeMemory` 的 `git status` 和 `git rev-parse HEAD`。从 `C:/Users/ASYS/Desktop/VibeMemory-中台/` 下的 `评测中台/runs`、`测试中台/runs`、`监控中台/runs` 中读取与任务相关的近期存档，记录时间、评测集和环境；缺失或过期时注明，不能当作当前版本的结果。
2. 根据改动选择验证：代码逻辑先跑相关 pytest；涉及检索质量时选择相关评测集，并在条件一致时比较改动前后的逐题结果；涉及性能时对照相同数据和环境的监控结果。规则判分、真实回答质量和用户使用验收分开报告。
3. 中台测试入口为 `C:/Users/ASYS/Desktop/数据中台与测试中台/测试中台/run_tests.py`，支持 `--repo`、`--scope quick|full` 和 `--no-cov`。需要开发期存档时，可把该脚本复制到独立目录后运行，结果保存在副本旁的 `runs/`，避免将快速测试混入日常运行区。检查 pytest 返回码、用例数、失败数及 JUnit；该脚本的相对门禁可能跨 quick/full 比较，且无结果时退出码未必非零，不能只看门禁或退出码。
4. 中台评测入口为 `C:/Users/ASYS/Desktop/VibeMemory-中台/评测中台/run_eval.py`。运行前在同一 Python 环境打印 `vibe_memory.__file__`，确认导入的确实是待测检出；`--repo` 不能覆盖已经成功导入的其他安装版本。记录代码提交、脏工作区、评测集指纹、协议、配置和结果；不把合成数据或规则代理当成真实答案质量证明。
5. 交付开发结果时，说明本次证据、失败和未验证边界。运行区的每日任务负责定时产出及同步；开发期独立存档不会自动进入运行区和大屏。只有需要发布结果时才另行处理同步，避免覆盖既有数据或触发无关的云端操作。

示例：在 PowerShell 中将测试执行器复制到新的独立目录，运行快速测试且不写仓库覆盖率文件：

```powershell
$probe = Join-Path $env:TEMP ('vibememory-dev-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $probe | Out-Null
Copy-Item 'C:\Users\ASYS\Desktop\数据中台与测试中台\测试中台\run_tests.py' $probe
& 'C:\Users\ASYS\Desktop\VibeMemory\.venv\Scripts\python.exe' (Join-Path $probe 'run_tests.py') --repo 'C:\Users\ASYS\Desktop\VibeMemory' --scope quick --no-cov --note '开发期独立快速测试'
```

此测试覆盖固定的五个文件，并非全量回归；保留输出目录供核对。不同检出若使用 `--repo`，也应显式传入对应的 `--python`，避免解释器和代码来源不一致。
