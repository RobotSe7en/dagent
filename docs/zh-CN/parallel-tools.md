# 并行执行工具

**尚未发布。** ToolLoop 可以同时执行一次模型回复中的多个独立工具调用。
`max_tool_concurrency` 默认为 `1`，现有应用继续串行执行。模型需要一次返回多个调用
才能获得并行收益；该设置不会改变 provider 请求字段，也不会强制模型返回多个工具。

## 启用工具并行

```python
import dagent


@dagent.tool(parallel_safe=True)
async def lookup(query: str) -> dict[str, str]:
    return await search_service.lookup(query)


agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=[lookup],
    max_tool_concurrency=4,
)
result = await runner.run(agent, input="查询三个主题。")
```

`max_tool_concurrency` 接受正整数，不接受布尔值。`1` 保持串行，大于 `1` 启用滚动任务池：
一个调用完成后，即使更早的慢调用仍未结束，也可以启动下一个调用。上限属于各自的工具
循环；独立运行的 Agent 使用各自的上限。`AutoAgent` 在选择工具执行路径时使用同名设置。
已注册 `ToolAgent`（包括静态 DAG Agent 节点）的内部工具使用该 Agent 自己的设置。
图调度器和父级 Agent 委派调用保持既有行为。

运行无需模型凭据的示例：

```bash
uv run python -m examples.parallel_tools
```

## 声明哪些工具可以同时执行

`@tool(parallel_safe=True)` 表示函数可以与其他已声明安全的工具同时执行；共享客户端
和可变状态必须支持并发调用。函数应通过 SDK 返回结果，不直接修改父会话，也不请求用户
交互。该声明可选，默认为 `False`。直接注册能力时使用
`CapabilityDefinition.parallel_safe`；注册表工具使用 `ToolRegistry.register` 的同名参数。

内置 `read_file`、`list_files` 和 `grep` 已声明可并行。写入、编辑、shell、UI 工具、Agent
委派及其他未声明能力独占执行：先等待前面的调用结束，再执行独占调用，结束后才启动
后续调用。例如：

```text
read(A), read(B), write(A), read(C)
   同时执行        独占      写入后执行
```

风险等级和审核要求与并行安全独立。已批准的路径边界覆盖调用也独占执行，因为它们会修改
当前 Run 的边界。

异步函数在事件循环上执行。并行派发的同步 Python 工具在线程中执行，包括 `@tool` 包装的
同步函数；上下文变量和 `supports_context=True` 继续有效。串行装饰函数保留原执行位置。
这种方式主要改善 I/O 任务；线程不保证纯 Python CPU 密集代码能获得多核加速。

线程中的并行工具触发 `callbacks.on_token` 或 `callbacks.on_event` 时，回调会投递到执行
事件循环。普通流和持久化流都能在工具仍运行时收到进度。取消后丢弃尚未送达的回调，
包括工作线程已经排入队列的回调。

## 启用 MCP 连接并发

由 MCP **服务端**在 `tools/list` 返回的每个工具上声明：

```json
{
  "name": "search",
  "inputSchema": {"type": "object"},
  "_meta": {"dagent/parallelSafe": true}
}
```

这是 dagent 扩展，并非 MCP 标准 annotation。缺省或 `false` 表示独占；非布尔值使注册明确
失败。`readOnlyHint`、风险、破坏性和幂等性均不会自动推导为并行安全。

宿主单独配置连接容量：

```python
runner.add_mcp_server("docs", {
    "command": "python",
    "args": ["docs_server.py"],
    "max_concurrency": 4,
})
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["mcp.docs.search"],
    max_tool_concurrency=4,
)
```

`max_concurrency` 默认为 `1`，接受正整数。同一连接的多个 Run 共享该容量。未声明工具
独占连接；排队中的独占工具也阻止后来的并行调用越过它。取消和超时会释放调度名额。
stdio 和 Streamable HTTP 共用此调度逻辑。MCP 快照保存工具声明，支持延迟注册和重新加载。

## 结果、审核与中断

完成事件（`capability.call.completed` / `capability.call.failed`）和持久化
`tool_execution` 观察按实际完成顺序到达；会话结果和持久化工具消息仍按模型原始顺序排列。
因此快工具的完成观察可能早于其结果消息，`result_item_id` 可为空。下一次模型请求等待
本轮处理完成。

整批审核仍在派发工具前完成。执行前再次发现边界审核要求时，停止派发，等待已启动调用，
提交已完成前缀，再保存 checkpoint。恢复不会重放此前缀。并行计划冻结并发数及安全声明，
包括已注册子 Agent 的工具；声明变化会使恢复校验失败。缺少新字段的旧 checkpoint 继续
串行执行并保留原有指纹。

持久化运行仍等待每个工具的 starting 观察确认后才执行对应工具；会话版本及发布继续串行
提交。参见[持久化](conversation-changes.md)。

普通工具错误只生成该调用的 failed 结果，不取消同批其他调用。steer 停止新增派发，等待
已启动调用结束，将未启动调用标为 skipped，再应用新指令，即使本批调用已经全部启动
也会处理。若没有剩余模型步数，则以 `step_limit_exhausted` 明确丢弃指令并使运行失败。
取消、持久化拒绝和必需结果存储失败会停止派发并取消剩余任务。中断且结果无法确认的
调用记录 `outcome_unknown`；
持久化拒绝时无法承诺这些观察已经保存。Python 线程和远端副作用无法强制回滚，也不会
自动重放这些调用。

UI 工具独占执行。并行派发的自定义工具调用 `context.ui` 时抛出
`UserInteractionUnsupported`，不建立交互请求。自定义交互工具应保持默认
`parallel_safe=False`。参见[用户交互](user-interactions.md)。
