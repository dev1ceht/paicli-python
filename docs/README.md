# 文档导航

[返回项目首页](../README.md)

## 使用与开发

- [使用指南](usage.md)：模型配置、交互命令、MCP、Runtime API、SDK 与执行边界。
- [开发指南](../CONTRIBUTING.md)：环境准备、检查与贡献约定。
- [领域术语与工程上下文](../CONTEXT.md)：核心概念及设计约束。
- [架构决策记录](adr/)：功能演进中的设计选择；历史决策可能被后续记录替代。
- [实现对齐记录](parity.md)：Python 实现的能力清单与历史对齐情况。

## 评测与历史设计

- [Harbor 评测](../benchmark/harbor/README.md)：适配器安装与容器评测流程。
- [本地上下文压力评测设计](specs/local-smoke-v1.md)。
- [历史实施方案](plans/)。

本地评测脚本依赖的 `benchmarks/` 数据集未随仓库分发；相关方案用于说明实验设计，
不是克隆后即可运行的快速开始。运行这些脚本前需自行准备对应 manifest、fixture 和验收数据。
评测日志、运行结果和本地 IDE 配置不纳入版本控制。
