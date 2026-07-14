# 上下文成本脚本评测方案

## 状态

已实现脚本模式 runner，并已在项目内五个 fixture 上完成一次 smoke 运行；live 模式尚未实现。

## 目标与结论边界

本评测验证 PaiCLI 在合成上下文压力下，三种上下文策略的 prompt token 消耗、LLM 摘要净收益和脚本任务验收结果。它使用固定脚本模型回放，因此所有 token 均是 `estimated_proxy`；即使 verifier 通过，也不能将结果表述为真实 provider 成本优化。

后续 live 评测才会调用 DeepSeek、Qwen 等真实 provider，并使用 provider 返回的 usage 作为 `actual` 数据。

## 参考实现与范围

方案参考 Pico 的 `pico/evaluation/context_cost.py`：保留其独立脚本入口、隔离工作区、脚本模型、成对比较、净收益公式和报告结构。

不直接复制其运行时实现。PaiCLI 的评测仅新增 benchmark 脚本及其测试、任务和 fixture；不新增普通 CLI 命令，不改变普通 Agent 的默认运行路径，也不依赖 `D:\project\pico` 的绝对路径。

## 术语

- **脚本上下文成本评测**：脚本模型固定回放工具调用，PaiCLI 在隔离副本中真实执行调用并运行 verifier 的评测。
- **上下文策略变体**：同一任务的受控策略。唯一允许变化的是上下文处理方式。
- **合成压力历史**：为了稳定触发 `tier3_summary` 而在任务前注入的统一历史；它不代表真实用户会话。
- **实际用量**：provider 返回的 usage，标记为 `actual`。
- **估算代理用量**：脚本模式使用固定、未校准的 `TokenEstimator` 计算的用量，标记为 `estimated_proxy`。

## 任务与工作区

将 Pico 的任务定义和 fixture 适配并迁入当前仓库：

```text
benchmarks/
  long_session_tasks.json
  fixtures/
    long_session_multi_file_refactor/
    long_session_debug_fix/
    long_session_health_endpoint/
    long_session_config_migration/
    long_session_dependency_upgrade/
```

每个 run 都从 fixture 创建新副本：

```text
artifacts/context-cost/runs/<task-id>/<variant>/<repeat>/workspace/
```

因此预录的写文件、补丁和命令调用只会改变副本，绝不改变源 fixture。每个任务保留初始 prompt、允许工具、step budget、期望产物及 verifier。

现有 Pico 任务中的 `patch_file`、`run_shell`、`search` 等调用会适配为 PaiCLI 实际支持的工具协议和名称，例如 `read_file`、`write_file`、`bash`、`grep`、`glob`。`write_file` 的脚本参数必须携带完整且确定的目标内容，避免依赖未实现的补丁工具。

## 三个策略变体

| 变体 | 历史与裁剪 | 摘要来源 | 摘要成本 |
|---|---|---|---|
| `no_context_reduction` | 保留完整历史；禁用工具结果压缩、压力裁剪和会话压缩 | 无 | 0 |
| `full_orchestrator` | 启用工具结果压缩与压力分级；触发时压缩旧历史 | `deterministic_compact()` | 0 |
| `full_orchestrator_with_llm_handoff` | 与 `full_orchestrator` 相同 | 预录的结构化 LLM 摘要 | 计入 fixture 声明的 input/output token |

评测脚本会在 Agent 实例上安装 benchmark 专用的上下文策略适配器。适配器只在脚本进程中替换该实例的 `context_manager`，不会修改生产运行路径：

- 基线适配器原样返回 role-preserving history；
- 确定性适配器复用现有压力、工具结果处理和 `deterministic_compact()`；
- handoff 适配器复用相同压力与裁剪规则，并将压缩请求交给脚本 LLM 返回预录摘要。

这样基线不会残留当前 `ContextManager` 的工具结果压缩或压力裁剪，三个变体才真正只相差上下文策略。

## 合成压力历史与预算

所有 task × variant × repeat 在相同 Agent 系统提示词、同一合成压力历史和同一任务脚本下开始。

脚本使用 PaiCLI 正常配置的 prompt 预算，不设置每任务专用预算。启动时根据该预算与固定、未校准的 `TokenEstimator` 生成合成 user/assistant 历史，直到其原始 role-preserving token 量达到 tier3 阈值。最近受保护的 turn 保持完整，较早的 turn 是压缩候选。

当前生产 assembler 会在压力计算前按 section 配额截断 history。benchmark 专用适配器因此按截断前的历史判断是否触发摘要，然后复用现有摘要器；这一规则仅存在于独立脚本实例，普通 Agent 路径不变。

这保证压缩分支被稳定覆盖，但报告必须注明它是“合成上下文压力下的方向性证据”。

## 脚本 LLM 与结构化摘要

脚本 LLM 有两类固定响应：

1. 对普通 Agent 请求，按任务 `scripted_outputs` 顺序返回固定工具调用或最终文本。
2. 对 compaction 请求，只有 handoff 变体返回任务 fixture 中预录的结构化摘要及其声明 usage；确定性变体不会调用模型。

任务 schema 为 handoff 增加等价于下列结构的数据：

```json
{
  "llm_handoff": {
    "summary": "## Goal\\n...",
    "usage": {"input_tokens": 0, "output_tokens": 0}
  }
}
```

其中 `summary` 应先通过现有结构化摘要 prompt 生成并人工审阅，再冻结到 fixture。`usage` 是脚本模式的固定代理成本；若后续 schema 记录 compaction prompt 的估算输入 token，报告须清楚区分“回放声明值”和“当前估算值”，不得伪装成 provider usage。

## token、成本与净收益

脚本模式不读取 provider usage。每个 run 创建新的 `TokenEstimator`，不接受历史校准，所有行统一记录：

```json
{"usage_source": "estimated_proxy"}
```

逐轮 trace 记录发送给模型的 role-preserving request 的估算输入 token、压力层级、是否压缩、摘要模式和摘要 usage。对于 handoff 变体，净收益公式为：

```text
net_benefit_tokens = baseline_input_tokens
                   - optimized_input_tokens
                   - compact_call_total_tokens
```

`compact_call_total_tokens` 是预录 handoff 摘要的 input 与 output token 之和。净收益允许为负，报告不得截断为 0。

配对汇总按 `task_id + repeat` 比较，分别统计：

- 全历史与确定性摘要；
- 确定性摘要与 LLM handoff 摘要；
- 每组的总输入 token、压缩调用 token、净收益和中位变化率。

脚本报告不使用 provider 价格得出美元或“真实成本”结论；如展示配置价格，只能标注为估算代理。

## 真实工具执行与质量门槛

脚本模式不是纯事件回放。脚本模型的工具调用会由 PaiCLI 在隔离副本中真实执行，随后运行 task verifier。

一个 run 仅在以下条件同时满足时为 `passed`：

1. 每个预录工具调用执行成功；
2. Agent 正常以最终文本结束；
3. verifier 在隔离工作区的退出码为 0。

任一条件失败即为 `failed`。汇总报告必须给出每个变体的通过率；在脚本模式中，这是固定轨迹的质量验收，不表示模型在自由推理下的质量无回归。

## 可复现性门槛

每个 `task × variant` 连续运行两次。第二次不用于统计学采样，而用于确定性校验：同一对 run 的规范化 JSONL trace 和核心汇总指标必须一致，否则脚本失败并指出首个差异事件。

每个 run 的工作区、trace 和报告都保留，便于定位工具调用、压缩触发和 verifier 失败原因。

## 产物与入口

入口是独立脚本：

```text
scripts/evaluate_context_cost.py
```

脚本默认读取项目内 `benchmarks/long_session_tasks.json`，并将产物写入：

```text
artifacts/context-cost/
  runs/
  traces/
  results.json
  report.md
```

`results.json` 包含逐 run 行、策略、重复次数、验证状态、`usage_source`、输入/输出 token、摘要成本及净收益。`report.md` 必须突出以下结论边界：

- `estimated_proxy` 是可复现的成本方向性证据，不是账单数据；
- 脚本 verifier 只覆盖固定轨迹；
- 只有 live 模式的 `actual` usage 加上 verifier 质量门槛，才能支持真实成本优化主张。

## 实现与验证顺序

1. 迁入并适配任务 manifest、fixture 和 PaiCLI 工具调用格式。
2. 实现独立的脚本 LLM、benchmark 上下文策略适配器和隔离工作区 runner。
3. 实现 trace、token 记账、净收益、配对汇总和 Markdown/JSON 报告。
4. 为三种变体、摘要成本、基线无裁剪、fixture 隔离、verifier 失败和双运行确定性添加自动测试。
5. 运行脚本并人工检查产物中的策略差异、压缩触发和质量门槛。

## 非目标

- 不在本阶段调用真实 provider 或估算真实账单；
- 不改变 PaiCLI 普通 Agent、REPL、TUI 或 Runtime API 路径；
- 不将脚本模式结果描述为真实模型质量或真实成本优化；
- 不依赖 Pico 作为运行时依赖。
