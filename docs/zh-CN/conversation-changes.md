# 增量保存会话变化

此能力自 SDK **0.10.4** 起可用。`Runner.run`、`stream`、`resume` 和
`resume_stream` 共用可选的 `on_conversation_change` 异步回调。省略它时，保持已有
执行行为、事件序列化、模型请求、压缩策略及结果/checkpoint 格式。

SDK 交付实际采用的状态变化。宿主持久化后构造现有 `ConversationState` 续聊，无需
重写压缩算法或上下文组装。SDK 不管理数据库、权限、宿主事务或宿主文件存储。

## 订阅与确认保存

```python
import dagent


async def save_changes(batch: dagent.ConversationChangeBatch) -> None:
    await host.save_batch(batch)  # 宿主提交成功后才返回。


result = await runner.run(
    agent,
    input="继续调查。",
    conversation=previous_state,
    on_conversation_change=save_changes,
)
```

这里的 `host.save_batch` 是应用代码。回调必须返回 awaitable。SDK 等它完成后才进入
后续模型或工具执行。正文和推理仍通过现有流事件增量输出，不逐 token 回调整份消息。
宿主可以在回调内部设置超时；SDK 不设置持久化超时，也不自动重试。文件保存等待不占用
压缩器的超时预算。

其他三个入口使用相同参数。审核续跑仍须另存并传入 `RunCheckpoint`，会话变化记录
不提供任意中断点的执行恢复能力。

离线运行示例：

```bash
uv run python -m examples.conversation_changes
```

示例将批次写入宿主 JSONL 日志、复制通知的文件、保存普通审核 checkpoint、审核续跑，
再读取日志重建状态并续聊，不额外保存 `result.conversation`。

## 批次顺序与身份

`ConversationChangeBatch` V1 包含：

| 字段 | 含义 |
| --- | --- |
| `delivery_id`、`sequence`、`batch_id` | 每次 run/resume 独立交付会话；序号从 1 开始；批次身份用于宿主去重 |
| `run_id`、`conversation_id` | 运行与公开会话身份 |
| `base_revision`、`revision` | 本批应用前后的 SDK revision，允许跳号 |
| `conversation_schema_version` | 重建公开状态的版本，目前为 4 |
| `messages` | 完整类型化消息，或明确标记的部分观察记录 |
| `context` | 公开有效成员、摘要与压缩状态变化 |
| `observations` | 响应生命周期和实际已知的工具执行事实 |
| `resources` | 已写完的类型化文件引用与文件定位参数 |

审核恢复保留 Run ID，但创建新的交付 ID。流事件序号与批次序号相互独立。仅审计或
文件变化不增加会话 revision。原本没有公开会话的静态 DAG，其会话 ID、状态版本和
两个 revision 均为 `None`，不产生公开 `context` 变化，但仍可保存内部审计和文件。

宿主按交付顺序应用批次，检查 `base_revision`，并在自身事务中保存变更与批次收据。
回调成功返回才表示 SDK 收到保存确认。宿主可能已经提交，然后在返回前报错，因此
交付失败不代表批次一定未落盘。同一会话的并发写入协调由宿主负责。

## 分别保存消息与有效成员

消息记录使用 `(origin.id, item.id)` 作为键，包含完整的 `UserMessage`、
`AssistantMessage` 或 `ToolResultMessage`、来源、可选 `response_id` 和 `partial`
标记。公开有效成员使用 `origin.id` 等于公开会话 ID 的记录。规划器、验证器和子
agent 的审计来源独立，不自动成为公开历史。

存在 `context` 时，作为一次完整变化应用：

1. 将 `messages` upsert 到结构化记录表。
2. 从有效顺序中移除 `deactivated_item_ids`，以及摘要变化中的
   `summary_change.replaced_item_ids`。
3. 根据 `updated_item_ids` 用完整公开记录替换原有效消息，位置不变。
4. 按 `added` 顺序插入消息，使用前述操作后列表中的零基索引。
5. 应用完整摘要、压缩状态替换和批次 revision。

退出有效上下文不会删除审计记录。摘要替换和对应消息退出在同一批次中交付。后来被
撤销的内部压缩候选不会移除公开消息。单独收紧已有摘要时，被替代消息 ID 可以为空。

`summary_change=None` 表示未变化；`summary_change.value=None` 表示明确清空。
`compaction_state_change` 使用同样规则。允许只有 revision 变化。结果展示更新交付
完整消息，包括冻结展示、恢复引用、retention 和审核反馈。未变化的历史消息不重发。

从保存的有效记录构造：

```python
previous_state = dagent.ConversationState(
    id=conversation_id,
    revision=saved_revision,
    items=tuple(saved_active_items),
    summary=saved_summary,
    compaction_state=saved_compaction_state,
)
```

继续传入现有 `conversation=`。在已有会话上开始订阅时，应保留初始状态及其现有引用
文件。system prompt 每次独立生成，不进入历史。内部审计不能代替内部线程 checkpoint。

### 对象所有权

每个批次（包括 `ConversationChangeError` 报告的失败批次）都是拷贝：宿主可以随意修改收到的
对象，不会影响 run。对 `run` 和 `stream`，开始交付时 SDK 会把传入的 `conversation` 复制一次，
并在这份拷贝上运行，所以 run 期间修改宿主自己的对象也不会影响 SDK；审批续跑在 checkpoint
上运行，`resume` 使用期间请把 checkpoint 当作只读。SDK 不会原地修改已发布的消息：tool agent
和动态 DAG 的工具拿到的是参数的拷贝，结果按返回那一刻记录。正因
如此，交付时才能按引用保存之前的状态，而不必每个批次都复制整个会话。

## 关联流式内容和保留中断证据

启用后，响应开始、正文/推理增量和响应结束事件携带生成前分配的 SDK `message_id`。
批次的响应观察记录和最终结构化消息使用同一身份及 `response_id`。工具调用保留自己
的 ID，工具结果通过 `call_id` 关联。

取消或意外流失败时，已经观察到的正文和推理作为一次部分消息及终态观察交付。是否
进入有效上下文完全由 `context` 决定，并保留现有模型失败行为；宿主不能自动追加
部分观察记录。
如果连接失败时 SDK 失败消息没有包含已经输出的文字，终态观察通过
`observed_content` / `observed_reasoning` 保留差异。应同时保存观察记录；这些字段用于
展示和审计，不能覆盖 SDK 实际采用的有效消息。

工具 `starting` 表示执行派发前已经保存的调用意图，不证明副作用已经发生。
`completed` 和 `failed` 表示已知结果；派发被中断且没有返回值时，用
`outcome_unknown` 保留不确定性。不补造工具结果，也不自动重放工具调用。
`MapNode` 的每次调用遵循相同的确认和结果交付规则。保存失败阻止尚未派发的工作；
已经执行中的调用仍可能完成，其执行证据会保留。
如果工具已返回，但必需的结果文件保存失败，SDK 仍会在传播存储错误前交付已知的
`completed` 或 `failed` 观察记录，DAG 节点和 Map 调用也遵循此规则。该观察记录
不代表结果文件已保存；结果规范化失败时，不交付完整的工具结果消息。

取消阻止后续推进，并等待正在执行的回调和必要收尾交付，因此可能等待宿主持久化。
这不承诺进程崩溃时逐 token 持久化，也不回滚工具副作用。

## 保存引用文件

每个资源通知包含完整的 `ContentReference` 或 `Attachment`、`workspace_path` 和
实际使用的 `runtime_directory`。交付时写入已结束。使用已有公共方法定位：

```python
for resource in batch.resources:
    path = runner.resolve_runtime_path(
        resource.reference.path,
        workspace_path=resource.workspace_path,
        runtime_directory=resource.runtime_directory,
    )
    await host.save_file(path, resource.reference)
```

资源通知只带引用，不带文件内容：宿主要按路径自己读取文件，因此需要能访问 run workspace
（同一台机器或共享卷）。交付时通知的文件已写完，通知不扫描整个目录。通知的文件有三类：

- **压缩归档**（源归档、压缩记录和旧版结果索引，带 `sha256`），保存被压缩消息的原文；
- **工具输出溢出文件**（不带 `sha256`），尽力保存，SDK 不校验；
- **上传附件**（`Attachment`），在 **Unreleased** SDK 中新上传的附件不带摘要。

在 **Unreleased** SDK 中，这些文件宿主都可以不存。同一会话的 run 共用 workspace，之后的
轮次在原处就能找到之前写的文件，SDK 不在 workspace 之间复制任何文件。绝对 runtime 目录下，
引用仍使用逻辑路径。可变附件继续采用现有上传时元数据语义。

## 把会话移到另一个 workspace

在另一个 workspace 续聊（显式传入与之前不同的 `workspace_path`）时，之前的文件都不在。
续聊照常进行：会话状态和摘要都在，但压缩会把读不到的历史标为未完全覆盖，模型也打不开
之前的归档。需要保留时，在收到通知时保存压缩归档，并在新 workspace 第一次运行前写回原
逻辑路径：

```python
async def on_change(batch):
    for resource in batch.resources:
        reference = resource.reference
        if isinstance(reference, dagent.ContentReference) and reference.sha256:
            path = runner.resolve_runtime_path(
                reference.path,
                workspace_path=resource.workspace_path,
                runtime_directory=resource.runtime_directory,
            )
            await host.save_archive(conversation_id, reference.path, path.read_bytes())

# 在新 workspace 第一次运行前：
for logical_path, data in await host.load_archives(conversation_id):
    target = runner.resolve_runtime_path(logical_path, workspace_path=new_workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
```

需要上传文件和溢出文件时，按同样方式复制。SDK 不提供恢复 API，也不改写路径。

## 处理保存失败

`run` 和 `resume` 抛出 `dagent.ConversationChangeError`，它是
`RunExecutionError` 的子类，提供 `failed_batch`、`acknowledged_sequence`、
`acknowledged_revision`、`failure` 和可审计的失败 `result`。原始保存错误保留在
`__cause__`。该结果不产生可续跑 checkpoint。

`stream` 和 `resume_stream` 发出一次 `run.failed`，其中
`error_type="ConversationChangeError"`，`data.result` 提供失败结果，
`data.conversation_change_failure` 提供同样的类型化交付失败信息。SDK 停止后续工作，
包括质量验证重试。即使 Run 因保存失败而失败，已完成的工具仍在执行证据中如实保留。
这也包括子图、循环体和委派 agent 内已经完成的调用。
如果交付在质量验证重试期间失败，失败结果及保存的运行状态会保留之前各次尝试和
当前重试的执行 trace。

默认不启用新回调。`result.conversation`、`new_items`、审核 checkpoint 和完整的
`run.finished` 结果继续支持。增量大小保证针对新增通知，不包含原有完整终态结果；
需要精简前端传输时，由宿主筛选。
