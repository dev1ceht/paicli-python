# 开发指南

## 准备环境

使用 Python 3.11+，在仓库根目录执行：

```bash
uv sync --extra dev
```

运行真实模型任务时，参考 [README](README.md#快速开始) 配置 `.env`。
不要提交 API Key、个人配置或会话数据。

## 检查变更

```bash
uv run python -m ruff check src tests
uv run python -m ruff format --check src tests
uv run python -m pytest
uv build
```

只验证某个模块时可以指定测试文件，例如：

```bash
uv run python -m pytest tests/test_config.py
```

CLI 基本检查：

```bash
uv run paicli --version
uv run paicli --help
```

部分本地评测测试依赖未分发的 `benchmarks/` 数据集，缺少数据时无法完成这些测试。
容器评测另见 [Harbor 指南](benchmark/harbor/README.md)，需单独准备 Harbor 与 Docker。

## 提交约定

- 聚焦一个问题，说明变更目的、用户可见行为与验证结果。
- 修改命令、配置或使用方式时，同步更新 README 或使用指南。
- 修复功能问题时，优先补充能复现问题的测试。
- 影响架构的变更可参考 [领域上下文](CONTEXT.md) 与 [架构决策](docs/adr/)。
- 保留 `uv.lock`，依赖变更时同步更新锁文件。
- 不提交虚拟环境、IDE 状态、评测产物或凭据。
