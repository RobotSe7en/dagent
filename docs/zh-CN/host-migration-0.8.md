# SDK 0.8 Host 迁移

本文是基于达智的 host 实施规范。内置 API 是一份参考实现；enterprise 和第三方
host 仍需在自己的持久化与鉴权层落实这些边界。

## 持久化边界

分别持久化两个不同对象：

- 会话存储：`RunResult.conversation` 返回的完整有界 `ConversationState`；
- 待恢复 run 存储：`RunResult.requires_review` 为 true 时返回的完整
  `RunCheckpoint`。

不要从 UI 消息、trace 或数据库离散字段重建这两个对象，也不要把单独的 `RunState`
当作可恢复对象。

租户/用户/项目 key、鉴权、脱敏、加密、保留策略和乐观并发仍由 host 负责。可以使用
`ConversationState.id` 和 `revision` 作为 SDK 层身份与版本，但不能把它们当作鉴权边界。
`revision` 是状态版本，不是 item 数量。review resume 再次到达审核边界时，替换
conversation 的 revision 必须严格大于上一个边界的 revision。相等 revision 属于 stale
状态，host 不得增加 equality fallback。

## 请求映射

新的聊天轮次映射为：

```python
result = await runner.run(
    resolved_agent,
    input=request.input,
    conversation=stored_conversation,
    input_uploads=uploads,
)
```

run 成功后，使用 `result.conversation` 整体替换已存会话。不要追加
`result.new_items`；返回会话已经包含被接受的有界状态。

审核批准/拒绝映射为：

```python
result = await runner.resume(
    decision,
    checkpoint=stored_checkpoint,
)
```

一个 checkpoint 只能消费一次。如果恢复后又到达新的审核门，持久化新的 checkpoint。
恢复时还会校验当前 capability definition 与 checkpoint 中冻结的指纹。恢复前必须注册
相同的 tool/MCP definition；语义发生变化时应创建新 run。

## 数据库和 API 切换

1. 增加 V3 conversation document/blob 和 V3 checkpoint document/blob 字段，完整保存
   Pydantic JSON payload。
2. 整体替换 conversation 时增加 revision compare-and-swap。
3. run 请求从 `messages`/`state` 改为 `input` 加 host conversation id。
4. review endpoint 必须解析并提交完整 V3 checkpoint。
5. 删除 host 自行追加 assistant/tool message 的逻辑。
6. 删除 state-only resume，以及普通聊天对同一 run checkpoint 的复用。
7. 明确拒绝 V1/V2 记录。如需保留历史数据，应只读保存或通过离线、有版本的 job 迁移，
   不要在 SDK runtime 中增加兼容 shim。

内置 SQLite host 会记录 conversation schema 标记。升级时，没有合法且 identity 匹配的
V3 `ConversationState` 的旧行会被标记为 legacy，并在读取旧 `last_run_id` 前返回
HTTP 409；新会话则明确创建为 V3。其他 host 也应保留同样的显式区分，不要让旧会话和
新建会话同时使用 `NULL` state 加 revision `0`。

## 推理与审计

`RunResult.new_items` 是完整的当前 run 审计增量，可能包含
`AssistantMessage.reasoning`、内部 planner/router/validator item、工具结果和 provider
usage。产品需要完整回放时，应把它写入 host 的审计/事件系统。

虽然 reasoning 会出现在审计 item 和近期会话状态中，但绝不会进入后续 provider 输入。
向用户展示 reasoning 前，由 host 应用脱敏和访问策略。

新增流事件：

- `response.reasoning.delta`；
- `context.compaction.started`；
- `context.compaction.finished`。

## 外置结果

`ContentReference.path` 是 workspace 相对路径，并带有大小和 SHA-256。SDK 会把
conversation 保留的资源复制到 Runner workspace 下的 content-addressed store，再为下一
个 run workspace materialize 并改写路径，因此 Runner workspace 必须持久保存。替换本地
存储的 host 必须上传引用字节并保留其类型化 provenance。SDK 不生成公开 URL，也不实现
host 的保留策略。

不要信任客户端提交的 reference path。只能在记录的 run workspace 内解析，并校验
checksum。

## 上线验证

- 两轮会话只发送 system、摘要/近期历史和本轮输入，不包含 reasoning 字段；
- 截断或压缩后，每个 assistant tool call 仍有一个对应 tool result；
- 超长输入在 provider 调用前失败；
- 大型工具/MCP 输出被引用，并能随 workspace 上传长期存储；
- 进程重启后，仅依赖持久化 V3 checkpoint 即可恢复审核；
- review continuation 再次到达审核边界时，即使可见 items 不变，完整 conversation
  revision 也会推进；
- 重复或过期 conversation revision 被拒绝；
- V1/V2 resume 明确返回版本错误。
