# 在工具调用内等待用户输入

**未发布，显式启用。** 用户交互保留原 local 根 ToolLoop、执行协程、工具调用、
模型、能力范围和步骤预算。无需 Enterprise、UI 框架、HTTP、数据库或真实模型。
完整离线宿主示例：

```bash
uv run python -m examples.user_interactions
```

## 为本次执行提供接收方

```python
import asyncio
import dagent

requests: asyncio.Queue[dagent.UserInteractionRequest] = asyncio.Queue()
options = dagent.UserInteractionOptions(
    on_request=requests.put_nowait,
    on_end=lambda event: print(event.reason, event.invalidated),
    timeout_seconds=None,
)
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=[*dagent.ui_tools()],
)
task = asyncio.create_task(runner.run(agent, input="询问用户", interactions=options))

request = await requests.get()  # 例如 confirm 请求
await runner.respond_to_interaction(dagent.UserInteractionAnswer(
    run_id=request.run_id,
    invocation_id=request.invocation_id,
    interaction_id=request.interaction_id,
    value=False,
))
result = await task
```

上述 `runner` 是已配置的 Runner。宿主应持续读取请求队列，并处理各类请求；
离线示例提供完整循环。两个通知回调必须同步快速返回，异步操作由宿主另行调度。
响应提交到所属 Runner 的执行事件循环；多 worker 宿主负责路由到持有 Run 的实例。

`run`、`stream`、`resume`、`resume_stream` 均可选传入 `interactions`。
没有 Runner 默认接收方或隐式继承。工具内部独立启动的新 Run 必须自行显式配置。
审核续跑也重新提供配置；接收方不写入 checkpoint。

仅支持 local 根 ToolLoop，包括 AutoAgent 的工具分支。DAG、sandbox 显式配置交互
时在执行前拒绝；AutoAgent 的 DAG 分支在进入 DAG 前拒绝。`agent.*` 子智能体
不继承交互许可。不支持的工具上下文调用抛出 `UserInteractionUnsupported`，
沿用既有工具失败处理，不建立等待。

## 四种交互

`ui_tools()` 返回可独立注册的 binding，稳定 ID 为 `tool.ui_confirm`、
`tool.ui_select`、`tool.ui_input`、`tool.ui_editor`。它们复用既有 catalog、
能力选择、禁用及执行机制，不自动加入默认工具集。

| 上下文调用 | 请求参数 | 正常返回 |
| --- | --- | --- |
| `await context.ui.confirm(title, description=None)` | 标题、可选说明 | `bool` |
| `await context.ui.select(title, options)` | 标题、非空选项；字符串 ID 唯一，具有标签 | 选中 ID，`str` |
| `await context.ui.input(title, prompt="", initial="")` | 标题、提示、初始文本 | 完整提交字符串 |
| `await context.ui.editor(title, content, format="text")` | 标题、原文、`text/markdown/html` 格式 | 完整提交字符串 |

选项使用 `UserInteractionOption(id="draft", label="草稿")`。SDK 严格校验响应
类型及选项归属，`False` 和 `""` 都是正常结果。HTML 的展示、编辑与序列化由宿主
负责；SDK 完整传递字符串，不做内容变换。

自定义异步工具无需注册四个模型工具即可使用公开上下文：

```python
@dagent.tool(supports_context=True)
async def draft(*, context: dagent.ToolContext, callbacks=None) -> str:
    prefix = "作者："
    name = await context.ui.input("姓名", initial="Ada")
    try:
        await context.ui.confirm("加入附录？")
    except dagent.UserInteractionCancelled:
        return prefix + name
    return prefix + name + "\n包含附录。"
```

保留既有 `context=`、`callbacks=` 注入方式，工具局部变量在等待后继续使用。
禁用内置模型工具限制模型对该工具的直接调用；本次接收配置和执行范围独立控制
自定义 `context.ui`。调用结束后上下文句柄失效，逃逸的后台工作不能继续使用。

## 响应、放弃和终止

每个请求包含协议版本 1、SDK 生成的 `interaction_id`，以及执行绑定的 `run_id`、
`invocation_id`、`assistant_message_id`、`capability_id`。模型工具参数不能设置
这些身份。既有 invocation ID 仍沿用 provider call ID；跨轮重复 call ID 时，
同时使用 assistant 消息身份关联。模型定义位于 `dagent.schemas.user_interactions`，
主要公共契约也由 `dagent` 导出。

`UserInteractionRequest` 是带类型化 `parameters` 的可判别联合；
`UserInteractionResponse` 联合 `UserInteractionAnswer` 与
`UserInteractionAbandonment`。宿主可用 Pydantic `TypeAdapter` 解码自己的传输
数据，再提交类型化对象。

仅放弃当前交互：

```python
await runner.respond_to_interaction(dagent.UserInteractionAbandonment(
    run_id=request.run_id,
    invocation_id=request.invocation_id,
    interaction_id=request.interaction_id,
))
```

自定义工具收到 `UserInteractionCancelled`，与整个 Run 的取消分开。内置工具
返回 completed 工具结果，包含值 `{"status": "cancelled"}` 和明确取消标记，
允许模型继续。

终止受影响的 Run：

```python
await runner.abort_interaction(
    run_id=request.run_id,
    invocation_id=request.invocation_id,
    interaction_id=request.interaction_id,
    reason="unavailable",  # 或 "timeout"
    message="宿主编辑器已不可用。",
)
```

`timeout` 以 `UserInteractionTimeout` 失败，`unavailable` 以
`UserInteractionUnavailable` 失败，通知回调失败以 `UserInteractionDeliveryError`
失败。这些致命错误穿过普通工具异常处理并阻止后续派发。普通调用抛出类型化错误并
携带失败的 `result`；流式入口沿用既有失败交付方式。不产生可恢复交互 checkpoint。

`UserInteractionResponseRejected.code` 为 `not_pending`、`identity_mismatch`
或 `invalid_value`。不存在、错配、重复、过期和迟到响应均拒绝；无效响应不消费
有效等待。进程内匹配和单次消费不提供跨 HTTP 重试的持久化幂等。

## 等待、超时和持久化

同一 Run 最多一个 pending；并发第二次请求抛出 `UserInteractionBusy`。
现有工具批次在当前调用处顺序等待，结束前不派发后续工具或模型，完成的工具不重放。
不同 Run 相互隔离。

单调时钟截止时间从登记请求、发出通知前开始计算。`timeout_seconds=None` 不设
SDK 时限；宿主可传入自己的值，例如 `1800`，也可自行计时并调用
`abort_interaction(reason="timeout")`。30 分钟不是 SDK 默认值。
交互等待超时与持久化 ACK 超时相互独立。

等待期间新的 `Runner.steer` 以 `RunNotSteerableError` 拒绝；已接受的 steer
保留到既有处理边界。`Runner.cancel` 沿用各入口原有取消表现。停止、关闭流或
Runner、调用异常退出均使 pending 失效并释放等待和定时器。关闭 Runner 时同步
调用结束回调，即使宿主已不读取流，也能获知失效。

`user_interaction.requested` 和 `user_interaction.ended` 是类型化流事件。
结束记录含身份、`reason` 和 `invalidated`。原因包括 `submitted`、`cancelled`、
`timeout`、`unavailable`、`run_cancelled`、`runner_closed`、`invocation_ended`
及 `delivery_error`。宿主可据回调或事件分别管理用户等待及普通无进展计时。
生命周期通知是临时通知，不新增持久化 Run 状态。

内置用户值作为原 invocation 的工具结果返回，自定义工具返回自己的最终结果。
两者继续使用既有存储、长内容外置、`ContentReference`、模型展示和恢复规则。
启用 `StreamPersistenceOptions` 后，结果批次保存并 ACK 后才能派发下一工具或模型。
接受响应或结束等待不表示结果已保存。

请求的 `assistant_message_id`、`invocation_id` 分别关联已有 `tool_execution`
观察的 `assistant_message_id`、`call_id`，再用 `result_item_id` 定位结果消息。
ACK 前保存必需资源。宿主无需拼接 provider 消息或伪造工具结果。详见
[会话变化](conversation-changes.md)和[工具结果恢复](tool-result-recovery.md)。
