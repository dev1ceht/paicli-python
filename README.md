# PaiCLI Python

**在终端里与 AI 协作，完成代码阅读、修改和自动化任务。**

PaiCLI 是一个基于 Python 的终端 AI Agent，通过 OpenAI-compatible 模型服务理解任务，
调用本地工具，并在审批策略约束下执行操作。提供 Textual 交互界面、单次命令、Python SDK
和 Runtime HTTP API，适合本地开发与 Agent 工程学习。

[快速开始](#快速开始) · [使用指南](docs/usage.md) · [开发指南](CONTRIBUTING.md) · [架构与文档](docs/README.md)

## 能做什么

| 能力 | 说明 |
| --- | --- |
| 代码协作 | 读取与编辑文件、搜索代码、执行命令、建立代码索引 |
| 交互与规划 | 流式输出、任务规划、人工审批、可配置推理等级 |
| 模型接入 | 内置多种 provider 配置，也可连接自定义 OpenAI-compatible 服务 |
| 工具扩展 | MCP client / server、Skills、网页搜索与抓取、图片输入 |
| 持续工作 | 持久化会话、长期记忆、后台任务、上下文压缩 |
| 操作保护 | 命令与路径检查、工具审计、工作区 checkpoint 与恢复 |

## 快速开始

需要 **Python 3.11+** 和 [uv](https://docs.astral.sh/uv/)。推荐使用现代终端；
Windows 推荐 Windows Terminal + PowerShell 7。

### 1. 安装

```bash
git clone https://github.com/dev1ceht/paicli-python.git
cd paicli-python
uv sync
```

### 2. 配置模型

复制 [`.env.example`](.env.example) 为 `.env`，填入你自己的 API Key：

```dotenv
PAICLI_PROVIDER=deepseek
PAICLI_MODEL=deepseek-v4-flash
DEEPSEEK_API_KEY=your_key_here
```

以上模型名沿用项目默认配置，请按所用服务的可用模型调整。
其他服务商、自定义 API 地址及配置优先级见[配置说明](docs/usage.md#配置)。

### 3. 开始使用

```bash
# 启动交互界面
uv run paicli

# 执行一次性任务
uv run paicli -p "总结这个项目的核心模块"

# 指定工作目录
uv run paicli --cwd /path/to/your/project

# 检查本地环境与配置
uv run paicli doctor --cwd .
```

配置按目标工作目录加载；使用 `--cwd` 时，在目标项目放置 `.env`，
或使用进程环境变量 / `~/.paicli/config.json` 配置模型。

## 交互示例

在交互界面中直接输入任务，或使用 slash command：

```text
解释这个项目的入口和主要模块
为这个函数补充边界条件测试
/plan 为项目增加一个健康检查接口
/help
```

常用命令包括 `/model`、`/thinking`、`/context`、`/compact`、`/memory`、`/task`
和 `/checkpoint`。完整命令与快捷键见[使用指南](docs/usage.md#常用交互命令)。

## 集成与扩展

- [MCP](docs/usage.md#mcp)：连接外部工具，或将 PaiCLI 作为 MCP server。
- [Runtime API](docs/usage.md#runtime-api)：通过 HTTP 管理 thread、turn 和后台任务。
- [Python SDK](docs/usage.md#python-sdk)：在 Python 程序中调用查询引擎。
- [Harbor 评测](benchmark/harbor/README.md)：在隔离容器中运行编码任务评测。

## 项目结构

```text
src/paicli/       应用源码：Agent、工具、模型、TUI、会话与 Runtime
tests/          单元与集成测试
benchmark/      Harbor 评测适配器
scripts/        本地评测辅助脚本
docs/           使用说明、架构决策与评测设计
```

开发安装、检查命令及贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 执行边界

PaiCLI 可以修改文件和执行命令，审批、路径保护和审计不等于操作系统级沙箱。
请在权限合适的工作区运行；详细策略与数据存储位置见[使用指南](docs/usage.md#安全说明)。

## 相关项目与许可

本仓库基于 [itwanger/PaiCLI-Python](https://github.com/itwanger/PaiCLI-Python)。

采用 [MIT License](LICENSE)。
