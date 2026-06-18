# 迁移说明

dagent 已经发布公开 SDK contracts。本页记录升级时可能需要用户采取行动的面向用户变化。

## 当前发布线

当前包版本是 `0.4.2`。

## 0.4.2

- 内置 shell 命令 capability 现在是 `tool.shell`，DAG DSL 调用写作
  `shell(command="...", cwd=".")`。升级前请把已保存的 `tool.run_command`
  capability ids 和 `run_command(...)` plan calls 改成新名字。不会注册旧名
  兼容别名。
- Sandbox 执行现在会对不支持的 targets 和 capabilities fail closed。只有内置 tool
  capabilities 会在 `execution="sandbox"` 中执行。Python function tools、raw registered
  capabilities、MCP、skills、memory、agents、DAG、`DAGSpec` 和 `DagAgent` 在获得 sandbox
  支持前都必须使用 `execution="local"`；当 sandbox run 处于活动状态时，它们不再回退到
  host 执行。

## 公开 Surface 预期

以下行为视为已发布行为：

- package install name: `dagent-ai`
- Python import name: `dagent`
- [Python SDK 参考地图](python-sdk.md)中列出的 public exports
- Python tool capability ids 使用 `tool.<name>`
- MCP stdio capability ids 使用 `mcp.<server>.<tool>`
- 显式 `Runner(...)` inputs
- 通过 `Runner.from_config(...)` 加载配置文件
- 静态 DAG 显式 dependency 要求
- 通过 `Runner.resume(...)` 进行 review-safe continuation

## Capability Ids

Python function tools 使用 `tool.<name>`。不要依赖旧的或内部的 capability id prefix 作为
兼容别名。

## Runner 配置

`Runner(...)` 使用显式 SDK inputs，不会隐式读取 `config.yaml`。加载 provider settings、
MCP servers、validation 或 profile directories 时，使用 `Runner.from_config(...)`。

## Profiles

内置 profiles 是 `dagent/resources/profiles/<name>.md` 下的打包 Markdown 资源。用户
profile directories 必须通过 `profile_root` 显式传入。

## 静态 DAG Dataflow

静态 DAG 要求显式 dependencies。像 `node.output.title` 这样的 value reference 不会创建
edge。请使用 `dag.add_edge(...)` 添加依赖。

## 未来条目

未来 release 改变已文档化的公开行为时，请添加：

- affected version
- old behavior
- new behavior
- migration steps
- related examples or docs
