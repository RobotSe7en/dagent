# API 后端持久化

> 版本说明：本页描述内置的 0.8 host 实现。其他 host 需要遵循的 contract 参见
> [0.8 Host 迁移](host-migration-0.8.md)。

公开 Python SDK 仍然不做持久化。`Runner`、`ToolAgent`、`DagAgent`、`Dag`
和 `RunState` 继续是声明式/运行时对象；它们不打开数据库、不拥有用户，也不管理项目。
持久化属于顶层 `api/` FastAPI 后端。

## 本地项目模式

Web UI 和 API 后端支持项目与会话：

- 一个项目拥有一个共享 workspace 目录：
  `.dagent/projects/<project_id>/workspace`。
- 无项目会话使用独立目录：
  `.dagent/projects/_conversations/<conversation_id>/workspace`。
- 一个项目可以包含多个会话。
- 项目目录不做全局锁。
- 单个会话是单写者：同一时间只能有一个 stream 或 resume 驱动它。第二个写入者会收到
  `409`。会话锁是 lease，进程崩溃不会让会话永久 busy。
- 同一项目里的不同会话可以并发运行，也可能同时改同一个项目文件。

消息流使用 `/messages/stream`，请求只包含一个 `input` 字符串和
`conversation_id`；项目会话还包含 `project_id`。后端拒绝已删除的客户端
`messages` 和 `state` 字段，从 API store 读取有界 `ConversationState`，再通过
`Runner.stream(..., conversation=...)` 传入。会话或项目 workspace 由 host
单独提供。

## 存储内容

本地后端通过 `api/storage/` 使用 SQLite：

- `projects`：预留多租户字段的项目元数据和 `workspace_uri`。
- `conversations`：无项目会话和项目会话、owner 元数据、workspace URI、会话
  `kind`、`last_run_id`，以及完整的有界 `ConversationState` 和用于
  compare-and-swap 的 revision。普通 chat、动态 DAG、静态 DAG 是不同 kind，
  不会在 endpoint 之间复用。
- `runs`：某个 run 当前权威的 `RunState` 快照、审核待处理期间的完整
  `RunCheckpoint`；静态 DAG run 可以带 saved DAG 引用。每个普通会话轮次都会创建
  不同的 run id。
- `run_streams`：一次 HTTP stream/resume 执行尝试。
- `run_events`：带数据库 event id 的持久 SSE 事件历史。
- `conversation_messages`：投影后的可见 user/assistant 消息时间线；用于普通
  chat conversation，以及明确标记为 `smart_workbench` 或
  `orchestration_workspace` surface 的动态 DAG conversation。
- `reviews`：pending/resuming/resolved review 元数据。review row 会被原子 claim；
  可恢复执行状态保存在 `runs.checkpoint_json`。
- `saved_dags`：保存的静态 DAG spec、layout 元数据、revision 和 project 归属。
- `orchestration_sessions`：绑定到同 kind conversation 的动态/静态编排编辑器状态。

运行 artifact 不在 SQL 里重复保存。后端从 `RunState.trace` 和 workspace 文件系统派生
run artifact 列表。保存的静态 DAG 输入上传会写入 API 配置目录下的磁盘目录，API 进程重启后
仍可在后续静态 DAG run workspace 中 materialize。

本地 SQLite schema 是 API/WebUI 存储 schema，不是公开 SDK 数据 contract。0.8
新增字段通过明确的 SQLite schema migration 添加；SDK payload version 仍严格校验，
不会在 runtime 中自动转换。

## 编排历史

编排历史通过现有 API 持久化对象管理。动态编排历史保存为 `dynamic_dag`
conversation，并绑定 `orchestration_sessions` 和 runs。在编排工作区里，动态编排
session 是无项目 conversation，使用无项目 conversation workspace，不使用项目
workspace。项目范围的 DAG conversation 仍属于智能工作台的项目流程。静态编排历史保存为
`saved_dags`，运行历史通过 `saved_dag_id` 关联到 runs。

WebUI 使用这些 endpoint 管理编排历史：

```text
PATCH /conversations/{conversation_id}
PATCH /projects/{project_id}/conversations/{conversation_id}
GET /conversations/{conversation_id}/runs
GET /orchestration-sessions/{session_id}/runs
GET /saved-dags/{dag_id}/runs
DELETE /runs/{run_id}
```

`DELETE /runs/{run_id}` 表示删除一条运行历史。它会删除 run 记录、stream/event/state
记录、该 run 的 review 记录、专属 run workspace，以及 `run_id` 匹配该 run 的可见
`conversation_messages`。等待审核中的 run 也可以删除；这样做会有意丢弃对应的 pending
review 和该 run 的可见会话记录。

## Resume 和重启行为

持久化 review resume 使用：

```text
POST /projects/{project_id}/reviews/{review_id}/resume
```

后端先把 review 从 `pending` 原子更新为 `resuming`，读取关联 run 的完整
`RunCheckpoint`，然后调用
`Runner.resume_stream(decision, checkpoint=checkpoint)`。客户端不会发送 conversation
state 或 checkpoint。非持久化 `/messages/resume` 请求通过 `run_id` 定位进程内
checkpoint。

如果续跑又到达审核门，后端会保存替换后的 checkpoint；完成、拒绝、取消或失败都会清除
checkpoint。重复 resume 会返回冲突，不会重复执行已审核 capability。

Trace 和 artifact endpoint 会先读取数据库里的 `RunState`，没有数据库 state 时才回退到
进程内 runner。只要 workspace 文件仍可访问，API 进程重启后，completed 和
awaiting-review 的项目 run 仍然可以查看 trace 和 artifact。

动态和静态编排通过 `orchestration_sessions` 恢复。Review resume 会用最终
`RunState` 回写关联 session 的 draft；WebUI 重新进入匹配 kind 的 conversation 时，会从
session 恢复编排草稿。

## 企业化路径

三层抽象保持可替换：

- `Store`：本地 SQLite；多实例/worker 部署使用 Postgres。
- `WorkspaceStore`：本地 `file://` 目录；服务器容器使用 S3/GCS 等对象存储。
- Execution：当前在 API 进程内执行；后续可切换为 queued worker 执行。

服务器容器部署时，不要依赖容器临时磁盘保存项目文件。本地部署可以用持久卷；企业部署应使用
对象存储，并由 worker 在运行前后执行 `sync_in`/`sync_out`。
