# API 后端持久化

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

项目消息仍使用现有 `/messages/stream` endpoint，只是带上 `project_id` 和
`conversation_id`。项目模式下，后端拒绝客户端传入 `state` 和 `workspace_root`；
后端从 API store 读取上一轮 `RunState`，并把项目 workspace 传给
`Runner.stream(..., workspace_path=...)`。

## 存储内容

本地后端通过 `api/storage/` 使用 SQLite：

- `projects`：预留多租户字段的项目元数据和 `workspace_uri`。
- `conversations`：无项目会话和项目会话、owner 元数据、workspace URI、会话
  `kind` 和 `last_run_id`。普通 chat、动态 DAG、静态 DAG 是不同 kind，不会在
  endpoint 之间复用。
- `runs`：某个 run 当前权威的 `RunState` 快照；静态 DAG run 可以带 saved DAG 引用。
- `run_streams`：一次 HTTP stream/resume 执行尝试。
- `run_events`：带数据库 event id 的持久 SSE 事件历史。
- `reviews`：pending/resolved review 元数据。review state 在 `runs.state_json`
  里，不在这张表里重复保存。
- `saved_dags`：保存的静态 DAG spec、layout 元数据、revision 和 project 归属。
- `orchestration_sessions`：绑定到同 kind conversation 的动态/静态编排编辑器状态。

运行 artifact 不在 SQL 里重复保存。后端从 `RunState.trace` 和 workspace 文件系统派生
run artifact 列表。保存的静态 DAG 输入上传会写入 API 配置目录下的磁盘目录，API 进程重启后
仍可在后续静态 DAG run workspace 中 materialize。

本地 SQLite schema 是 API/WebUI 存储 schema，不是公开 SDK 数据 contract。检测到不兼容的
未发布本地旧库时，后端会重建数据库，而不是添加兼容 shim 或迁移层。

## 编排历史

编排历史通过现有 API 持久化对象管理。动态编排历史保存为 `dynamic_dag`
conversation，并绑定 `orchestration_sessions` 和 runs。静态编排历史保存为
`saved_dags`，运行历史通过 `saved_dag_id` 关联到 runs。

WebUI 使用这些 endpoint 管理编排历史：

```text
PATCH /conversations/{conversation_id}
PATCH /projects/{project_id}/conversations/{conversation_id}
GET /conversations/{conversation_id}/runs
GET /orchestration-sessions/{session_id}/runs
GET /saved-dags/{dag_id}/runs
```

## Resume 和重启行为

项目 review resume 使用：

```text
POST /projects/{project_id}/reviews/{review_id}/resume
```

后端读取 `runs.state_json`，重建 `RunState`，然后调用
`Runner.resume_stream(decision, state=run_state)`。hosted/project 模式下客户端不发送
state。

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
