# SWE-bench Lite A/B 评测手册

这套流程把「PaiCLI 生成 patch」和「官方 Docker harness 判定 patch」分成两个独立阶段。PaiCLI 不安装、不启动 Docker，也不会把 Agent 自测结果当作 pass@1。

## 1. 准备数据

使用已有的官方格式 JSON（推荐，可离线执行）：

```powershell
python scripts/evaluate_swebench.py import-dataset `
  --source D:\path\to\swebench-lite.json
```

或者先安装可选依赖，再显式授权下载固定 revision：

```powershell
pip install -e ".[swebench]"
python scripts/evaluate_swebench.py fetch-dataset `
  --revision <HUGGING_FACE_COMMIT> `
  --allow-network
```

命令会输出 `snapshot_dir`。完整数据只保存在忽略提交的 `artifacts/` 中；固定子集、来源和 SHA-256 会写入 snapshot 元数据，同时生成 `benchmarks/swebench-lite-v1/selections/*.json`。正式生成前需要审查并提交这两个有序 ID 清单。

## 2. 准备仓库镜像

首次运行需要网络；之后默认只使用本地 bare mirror：

```powershell
python scripts/evaluate_swebench.py prepare `
  --snapshot-dir <SNAPSHOT_DIR> `
  --selection context-stress-10 `
  --allow-network
```

## 3. 串行生成 A/B predictions

正式运行要求 PaiCLI 工作树干净。当前实现固定每个任务运行一次、两个变体交替排序，并且不提供 worker 数配置：

```powershell
python scripts/evaluate_swebench.py generate `
  --snapshot-dir <SNAPSHOT_DIR> `
  --selection context-stress-10 `
  --output-dir artifacts\swebench-lite\runs\<EXPERIMENT_ID>
```

输出目录中包含 `full-history/predictions.jsonl`、`optimized/predictions.jsonl`、逐任务证据，以及 `harness-command.txt`。中断后可以用同一条命令补齐尚未开始的任务；已完成任务不会覆盖，停留在 `running` 的任务会被拒绝并要求人工审计。

## 4. 手动运行官方 harness

先按[官方 SWE-bench 快速入门](https://github.com/SWE-bench/SWE-bench)安装并验证 Docker 环境。建议先运行官方 gold 单任务验证：

```powershell
python -m swebench.harness.run_evaluation `
  --predictions_path gold `
  --max_workers 1 `
  --instance_ids sympy__sympy-20590 `
  --run_id validate-gold
```

然后逐条执行实验目录里的 `harness-command.txt`。两次运行必须使用同一个官方 harness commit 或包版本；不要修改已经冻结的 predictions。

## 5. 导入结果并比较

分别导入两个官方运行目录：

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

最终查看 `comparison.json` 和 `report.md`。只有任务集合、官方结果、harness 身份和 provider 输入 token 都完整，并且 optimized 同时提高 pass@1、降低平均输入 token 时，报告才会生成可直接使用的简历表述。
