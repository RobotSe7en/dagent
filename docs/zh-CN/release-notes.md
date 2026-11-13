# 版本说明

## 0.11.0：ToolLoop 持久化流 V2

使用 `pip install --upgrade 'dagent-ai==0.11.0'` 安装。本版本包含持久化回调、默认工作目录
及文件存储的破坏性变更；依赖这些契约的宿主请先完成 [0.11.0 迁移步骤](migration.md#0110)。
ConversationState V4、RunState V6 和 RunCheckpoint V9 记录仍可读取。

- `stream/resume_stream` 新增可选 StreamPersistenceOptions；一条混合流、显式确认/拒绝和无界有序事件队列。
- 现有 ConversationChangeBatch 补充终态字段和现有审核 checkpoint，不新增 RunSummary 或第二种 checkpoint。
- ToolLoop 和委派 tool agent 不构建 trace，通过公开批次保存错误、用量、未知执行状态和资源引用。
- 新增 checkpoint 公开审核、隔离的剩余工具调用和只读校验入口，上传工具公开于 dagent.artifacts。
- 支持宿主重建上下文；新模式 input=None 重试已保存请求，停用尾部未闭合工具组，不伪造结果、不重放副作用。
- 审核记录 ACK 后释放重状态；普通终态继续服从显式保留策略。
- 修复取消与 ACK 交错时的已确认会话修订号；终态开始交付后 cancel 返回 False，
  审核交付关闭或被拒绝时也释放本地审核状态。
- 持久化 run 的工具内部未传 `persistence` 的 Runner 调用作为独立普通 run 执行，
  不再加入或破坏外层 run 的交付。
- 无限保留时不再记录已用 run id，未保存状态就结束的 run 可再次用原 `run_id` 重试。
- 持久化事件直接入队，不等待读取方、不合并增量，只有事实批次等待宿主 ACK。
  移除草案中的 max_buffer_events、max_buffer_bytes、stall_timeout_seconds 配置、
  溢出处理及队列排空等待；保留 ack_timeout_seconds。
- 审核最终批次确认后即可恢复，无需等待上一条流结束。
- 用户消息之后的响应重试原样重发同一请求，不再插入第二条相邻的用户消息。
- 投递归属绑定生产任务，旧流不再阻碍普通续审取消；过期 ACK 返回 False，不确认终态。
- 持久化上下文及子 agent 失败摘要保留最终纯文本失败回复，丢弃的尝试和失败调用只入审计。
- steer 替代尾部内部重试提示；design_dag 隔离外层作用域；关闭 Runner 时持久化读流正常结束，
  主动取消读取任务仍传播取消异常。
- 使用每次交付的小容量缓存去重已确认、有摘要的资源引用；无摘要/可变资源及新交付仍通知宿主。
- ToolLoop 内部显式传递执行模式，统一清理流的登记；公开接口及序列化记录保持不变。
- 新增可选 ToolAgent.response_retries / AutoAgent.response_retries，默认关闭；开启后流式
  输出视为临时内容，丢弃的回复通过 response.discarded 事件通知宿主。
- 新增可选 Runner(max_retained_runs=...)，释放已结束的根运行状态及其拥有的 DAG 子会话；
  默认仍无限保留。
- 跨模型轮次重复使用 call id 时保留独立工具 trace 节点；同一回复内重复 id 以
  invalid_tool_call 拒绝且不执行工具，包括自定义 provider 返回的回复。
- 减少上下文准备、能力定义复制和归档写入的重复工作，增量提交工具循环消息，保持已发布消息的归属。
- **破坏性变更：工作目录与文件。** 会话内的 run 按 ConversationState.id 共用工作目录，
  静态 DAG 仍按 run 分配。SDK 不再跨目录携带、恢复或校验文件，删除 ConversationResourceError；
  宿主迁移会话时需自行复制文件。
- 工具输出文件改为尽力保存，且仅在模型能读取时写入。ContentReference.sha256、Attachment.sha256
  及冻结展示的恢复引用改为可选；新工具输出、产物和上传引用不带摘要。压缩归档缺失时降级上下文，
  展示文件写入失败时报告存储告警。宿主需接受可选字段并自行执行文件完整性检查。
- ToolAgent 和动态 DAG 中的工具接收普通容器参数的拷贝；通过返回值传递数据，不再依赖修改参数。
- **已明确授权的破坏性变更：** 删除四个入口的 on_conversation_change、专属 Handler/Failure/Error
  及失败事件回调字段；删除 DAG 回调专用钩子。其他默认执行、结果、provider 和 V4/V6/V9
  持久化契约保持，V1 批次仍可读取。参见[迁移说明](migration.md#0110)。

验证覆盖实际 Chat Completions/Responses 请求一致性、公开结果与续聊契约、审核恢复、
重试/压缩、保存失败、慢消费者及内存释放。发布验证：Python 全量测试（dev、MCP）为
2535 passed、3 skipped；离线持久化与状态保留示例均通过。128 项前端测试、前端构建、
wheel/sdist 构建、Twine 元数据、版本一致性、锁文件和差异格式检查均通过。跳过两项未启用的
真实 MiniMax 测试和一项缺少运行中 daemon 的 Docker 测试；真实 provider 和 Docker 验收需要相应环境。

边界：展示增量之外仍交付最终权威消息；V9 审核 checkpoint 保留完整状态及已有重复字段；
不拆帧、不承诺固定帧上限。普通无限保留和防重放 ID 增长仍显式存在。
宿主集成细节参见[持久化指南](conversation-changes.md)。
