# 使用指南

[返回项目首页](../README.md)

## 配置

配置按以下顺序覆盖，越靠后优先级越高：

1. 内置默认值
2. `~/.paicli/config.json`
3. `<project>/.paicli/config.json`
4. `<project>/.env`
5. 当前进程环境变量
6. CLI 参数

常用环境变量：

| 变量 | 说明 |
| --- | --- |
| `PAICLI_PROVIDER` | 模型服务商，默认 `deepseek` |
| `PAICLI_MODEL` | 模型名称，默认 `deepseek-v4-flash` |
| `PAICLI_API_KEY` | 通用 API Key |
| `PAICLI_BASE_URL` | 自定义 OpenAI-compatible API 地址 |
| `PAICLI_MAX_TOKENS` | 单次响应 token 上限 |
| `PAICLI_CONTEXT_WINDOW` | 覆盖模型上下文窗口 |
| `PAICLI_TEMPERATURE` | 采样温度 |
| `PAICLI_TYPEWRITER` | 是否启用交互式打字机效果，默认 `true` |
| `PAICLI_TYPEWRITER_CPS` | 基础显示速度，默认每秒 `80` 字符 |
| `PAICLI_TYPEWRITER_MAX_CPS` | 积压时最高显示速度，默认每秒 `320` 字符 |
| `PAICLI_TYPEWRITER_FPS` | 界面刷新帧率，默认 `30` |
| `PAICLI_HITL` | 审批模式：`always`、`auto` 或 `never` |

除通用 Key 外，也支持 `DEEPSEEK_API_KEY`、`GLM_API_KEY`、
`STEP_API_KEY`、`KIMI_API_KEY`、`QWEN_API_KEY` 和
`DASHSCOPE_API_KEY` 等 provider-specific Key。

连接自定义 OpenAI-compatible 服务：

```dotenv
PAICLI_PROVIDER=openai-compatible
PAICLI_BASE_URL=http://127.0.0.1:11434/v1
PAICLI_MODEL=qwen2.5-coder
PAICLI_API_KEY=local-key
```

完整 JSON 配置示例：

```json
{
  "llm": {
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "context_window": 64000
  },
  "typewriter_enabled": true,
  "typewriter_chars_per_second": 80,
  "typewriter_max_chars_per_second": 320,
  "typewriter_frame_rate": 30,
  "policy": {
    "hitl_mode": "auto"
  },
  "retry": {
    "enabled": true
  }
}
```

## 常用交互命令

进入 `uv run paicli` 后，可使用 slash command：

| 命令 | 用途 |
| --- | --- |
| `/help`、`/config`、`/tools` | 查看帮助、配置和工具 |
| `/clear`、`/reset`、`/context`、`/compact` | 管理界面、会话和上下文 |
| `/plan <task>` | 规划任务 |
| `/memory`、`/save <fact>` | 查询或保存长期记忆 |
| `/index [path]`、`/search <query>` | 建立索引并搜索代码 |
| `/mcp`、`/skill`、`/model` | 管理扩展与当前模型 |
| `/task`、`/task add <task>` | 管理后台任务 |
| `/checkpoint list`、`/checkpoint create [label]` | 查看或创建独立工作区 checkpoint |
| `/checkpoint restore <id>` | 恢复工作区文件 |
| `/exit` | 退出 |

Git 工作区默认使用 `refs/paicli/checkpoints/*` 保存 checkpoint；非 Git 工作区使用
`~/.paicli/snapshots/` 下的 Side-Git 兜底。可通过
`PAICLI_SNAPSHOT_BACKEND=auto|git|side-git` 覆盖默认选择。

常用快捷键：

| 快捷键 | 用途 |
| --- | --- |
| `Enter` / `Shift+Enter` | 发送 / 换行 |
| `Ctrl+C` | 中断运行；空闲时退出 |
| `Ctrl+Q` | 立即退出 |
| `Ctrl+L` | 清屏 |
| `Tab` | 补全 slash command |

## MCP

初始化项目级 Chrome DevTools MCP：

```bash
uv run paicli mcp init-chrome --scope project
uv run paicli mcp list
```

将 PaiCLI 作为 MCP server 运行：

```bash
uv run paicli mcp serve --transport stdio
uv run paicli mcp serve --transport http --port 3000
```

MCP 配置保存在用户级 `~/.paicli/mcp.json` 或项目级
`.paicli/mcp.json`。不要将包含敏感账号或生产数据的浏览器会话授权给 Agent。

## Runtime API

Runtime API 提供持久化 thread、turn、事件和后台任务接口。通过进程环境变量设置 `PAICLI_RUNTIME_API_KEY`（此变量不会从项目 `.env` 自动加载）。

PowerShell：

```powershell
$env:PAICLI_RUNTIME_API_KEY = "dev-key"
uv run paicli serve --http --port 8080
```

Bash / Zsh：

```bash
PAICLI_RUNTIME_API_KEY=dev-key uv run paicli serve --http --port 8080
```

下面的 `dev-key` 需替换为你配置的 API Key。

创建 thread 并发送 turn：

```bash
curl -X POST http://127.0.0.1:8080/v1/threads \
  -H "x-api-key: dev-key"

curl -X POST http://127.0.0.1:8080/v1/threads/<thread_id>/turns \
  -H "content-type: application/json" \
  -H "x-api-key: dev-key" \
  -d '{"message":"总结当前项目"}'
```

完整 Session 默认按工作区保存在 `~/.paicli/sessions/<workspace-hash>/*.jsonl`；后台任务生命周期、检查点和审批状态保存在 `~/.paicli/runtime/tasks.db`。

## Python SDK

```python
from paicli.sdk import create_default_engine

engine = create_default_engine(cwd=".")
result = engine.ask_complete("解释这个项目")
print(result.text)
```

## 安全说明

PaiCLI 会对工作区写入、命令执行、MCP 调用和快照恢复等操作应用审批、
路径保护与审计策略。审计日志默认写入
`~/.paicli/audit/audit-YYYY-MM-DD.jsonl`。

Agent 工具不是操作系统级沙箱。运行未知命令、连接外部 MCP server 或开放
Runtime API 前，请确认工作区、权限和网络边界符合预期。

## 推理控制

推理等级是固定选项，不支持自定义：`off`、`on`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`。

- `thinking_level` 缺省为 `auto`；默认 `.env` 不写等级。
- `PAICLI_THINKING_BUDGET` 默认 `8192`，可用 `null` 或 `none` 禁用。
- `max_tokens` 默认 `16384`；budget 不会自动映射为 max tokens。
- CLI 可使用 `--thinking high`；交互界面可使用 `/thinking`、`/thinking high`、`/thinking auto`。
- 状态栏显示当前等级，不显示 budget。

当前模型只展示自己支持的等级。Qwen 使用 `enable_thinking` / `thinking_budget`，DeepSeek 使用 `thinking` / `reasoning_effort`；这些字段由模型能力表决定，与 provider 无关。

