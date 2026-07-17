# SWE-bench Lite A/B 评测手册

这套流程把“PaiCLI 生成 patch”和“官方 Docker harness 判定 patch”分成两个独立阶段。PaiCLI 不安装、不启动 Docker，也不会把 Agent 自测或本地 `git apply --check` 当作 pass@1。

当前正式口径是固定的 `context-stress-5-v1`：5 个 SWE-bench Lite 任务、每个任务的 `full-history` 与 `optimized` 各运行 1 次，共 10 个串行 attempt。它只能表述为“固定五任务 SWE-bench Lite 上下文压力子集”，不能表述为完整 SWE-bench Lite 分数。

## 1. 准备数据

优先导入已有的官方格式 JSON：

```powershell
python scripts/evaluate_swebench.py import-dataset `
  --source D:\path\to\swebench-lite.json
```

或者显式授权下载固定 revision：

```powershell
pip install -e ".[swebench]"
python scripts/evaluate_swebench.py fetch-dataset `
  --revision <HUGGING_FACE_COMMIT> `
  --allow-network
```

命令会输出 `snapshot_dir`。完整数据只保存在忽略提交的 `artifacts/` 中；正式五任务和 Flask 开发任务的有序 ID 清单位于 `benchmarks/swebench-lite-v1/selections/`。

## 2. 先跑 Flask 单任务开发实验

`pallets__flask-5063` 不在正式五任务中，用来验证真实模型、工具和恢复流程：

```powershell
python scripts/evaluate_swebench.py prepare `
  --snapshot-dir <SNAPSHOT_DIR> `
  --selection flask-pilot-1-v1 `
  --allow-network

python scripts/evaluate_swebench.py generate `
  --snapshot-dir <SNAPSHOT_DIR> `
  --selection flask-pilot-1-v1 `
  --development `
  --output-dir artifacts\swebench-lite\runs\flask-pilot-001
```

仓库 mirror 已包含目标 `base_commit` 时，即使提供 `--allow-network` 也不会无条件 fetch。Windows 上所有 Git 子进程都启用命令级 `core.longpaths=true`，不会修改全局 Git 配置。

## 3. 正式生成固定五任务 A/B predictions

正式运行要求 PaiCLI 工作树干净，模型固定为已配置的 `qwen/qwen3.6-flash`、temperature 为 0，输入预算为 32K、输出预留为 4K：

```powershell
python scripts/evaluate_swebench.py prepare `
  --snapshot-dir <SNAPSHOT_DIR> `
  --selection context-stress-5-v1 `
  --allow-network

python scripts/evaluate_swebench.py generate `
  --snapshot-dir <SNAPSHOT_DIR> `
  --selection context-stress-5-v1 `
  --output-dir artifacts\swebench-lite\runs\<EXPERIMENT_ID>
```

生成始终串行，不接受 `--max-workers`。Windows Agent 的 `execute_command` 明确使用 Windows PowerShell 5.1；命令不要使用 `&&`、`sed`、`head` 等 Bash 专用语法。

同一命令可以用于恢复：

- `completed` 和 `agent_error` attempt 会跳过；
- `generation_frozen` 只重跑本地 apply-check，不会再次调用模型；
- 遗留的 `model_running` 会固化为 `interrupted`、空 patch，然后继续后续任务；
- 同一实验目录有活动进程时会拒绝并发运行。

旧的 `experiment-001` 属于诊断产物，不迁移、不用于正式声明。请为修复后的五任务实验使用全新的输出目录。

## 4. 手动运行官方 Docker harness

先按[官方 SWE-bench 仓库](https://github.com/SWE-bench/SWE-bench)安装并验证 Docker 环境。随后分别执行实验目录中为 `full-history` 和 `optimized` 生成的官方命令。两次运行必须使用相同的 harness commit 或包版本，并保持 `--max_workers 1`。

PaiCLI 只生成 predictions 和命令，不会替你运行官方 harness。

## 5. 导入官方结果并比较

```powershell
python scripts/evaluate_swebench.py report `
  --experiment-dir artifacts\swebench-lite\runs\<EXPERIMENT_ID> `
  --variant full-history `
  --harness-results-dir <FULL_HISTORY_HARNESS_DIR> `
  --harness-revision <HARNESS_COMMIT_OR_VERSION>

python scripts/evaluate_swebench.py report `
  --experiment-dir artifacts\swebench-lite\runs\<EXPERIMENT_ID> `
  --variant optimized `
  --harness-results-dir <OPTIMIZED_HARNESS_DIR> `
  --harness-revision <SAME_HARNESS_COMMIT_OR_VERSION>

python scripts/evaluate_swebench.py compare `
  --experiment-dir artifacts\swebench-lite\runs\<EXPERIMENT_ID>
```

查看 `comparison.json` 和 `report.md`。只有两个变体的五任务官方结果完整、provider 输入 token 完整、optimized 的 pass@1 更高且平均输入 token 更低时，才满足自动声明门槛。

推荐表述：

> 在固定 5 任务 SWE-bench Lite 上下文压力子集、32K 输入预算下，将 PaiCLI pass@1 从 xx% 提升至 xx%，平均模型输入 token 消耗降低 xx%。

这句话必须保留“五任务子集”和“32K 输入预算”两个限定。
