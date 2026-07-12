# dagent 中文文档

本目录是 dagent 的简体中文用户文档。项目根目录的
[`README.md`](../../README.md) 仍然是项目门面，包含项目介绍和核心架构；
这里的页面聚焦安装、SDK 使用、功能指南和已发布行为。

英文文档入口见 [docs/en/README.md](../en/README.md)。

## 从这里开始

- 第一次使用 dagent：阅读[快速开始](quick-start.md)。
- 配置环境：阅读[安装](installation.md)。
- 先理解模型：阅读[核心概念](concepts.md)。
- 查询公开 SDK 名称：阅读 [Python SDK 参考地图](python-sdk.md)。

## 功能指南

- [Runner 和配置](runner-and-configuration.md)：provider、`Runner(...)`、
  `Runner.from_config(...)`、validation、MCP 注册和运行时 capability 管理。
- [Capabilities](capabilities.md)：Python 工具、MCP capability id、结构化结果、
  policy 和 boundary。
- [Agents](agents.md)：什么时候使用 `ToolAgent`、`AutoAgent` 或 `DagAgent`。
- [静态 DAG](static-dag.md)：类型化 graph input、节点输出引用、artifact、显式边、
  控制流、子图和循环。
- [Skills](skills.md)：skill roots、managed installs、`SkillStore`、关联文件和
  agent 级 skill 可见性。
- [结果、流式输出和 Review](results-streaming-review.md)：`RunResult`、
  `RunCheckpoint`、streaming events、共享 execution budgets、resume 和持久化。
- [API 后端持久化](api-backend-persistence.md)：项目、会话、run state 存储、
  review resume 和企业化存储路径。

## 运维和升级

- [故障排查](troubleshooting.md)：常见安装、provider、MCP、capability、DAG
  validation 和 review 问题。
- [迁移说明](migration.md)：已发布接口变化和升级说明。
- [示例](../../examples/README.md)：可运行脚本以及对应的文档页面。

## 文档原则

- 这里描述的公开 SDK 行为视为面向用户的契约。
- 除非页面另有说明，示例都应从仓库根目录运行。
- 功能页应链接回 SDK 参考地图，而不是复制过长的参考表。
- 公开行为变化时，应在同一次变更里更新相关文档和示例。
