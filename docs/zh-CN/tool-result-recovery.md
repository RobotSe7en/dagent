# 恢复较大的工具结果

文件就绪通知与宿主增量保存方式，见[会话变化](conversation-changes.md)。

自 0.9.18 起，工具结果采用独立的单条展示上限，新增结果不再挤占历史结果的展示额度。
这同时覆盖 ToolAgent 回复和结构化 DAG 规划观察。

## 何时溢出写入文件

在 **Unreleased** SDK 中，只有能读回剩余内容的模型，才会看到被截断的结果：

- **具备内置 `read_file` 工具**（DagAgent 为能力列表中包含 `tool.read_file`）时，超过展示
  上限的结果会被截断，完整文本写入运行目录 `results/` 下的溢出文件，展示中告诉模型文件位置。
  ToolAgent 还要求本次运行的读取边界允许 runtime 的 `results/` 和 `history/` 目录；只检查
  路径，不打开文件。
- **没有 `read_file`**，或读取边界不允许 runtime 目录时，不写文件。结果仍按展示上限截断，
  被截断的展示标 `[RECOVERY_UNAVAILABLE: no saved original]`。紧急的仅引用切换不会进行，因为它会移除模型读不回的正文。
- **含 NUL 的文本**：对具备 `read_file` 的模型，从不截断，也不写文件，因为 `read_file`
  拒绝读取这类文件；它保持完整内联。
- `ValidatorAgent` 等 profiled agent 没有工具：仍截断以保持上下文有界，但不为它们写文件，
  被截断的内联结果标 `[RECOVERY_UNAVAILABLE: no saved original]`。

溢出文件尽力写入。SDK 原子写入，但不保存摘要（`ContentReference.sha256` 为 `None`），
不校验、不回读，也不复制到其他 run workspace。文件之后被改，读到的就是改后的内容；
被删除时，由文件工具报告不存在。压缩既不检查也不复制这些文件。大的结构化值、二进制值
和 MCP 产物属于数据而非展示文本，仍按大小写入文件，同样不带摘要；静态 DAG 节点读取
上游值时，只有文件读不到才报错。

## 分别配置展示与存储

```python
import dagent

context = dagent.ContextPolicy(
    max_tool_result_tokens=2048,
)
storage = dagent.ResultStoragePolicy(
    max_inline_bytes=256 * 1024,
    max_shell_output_bytes=64 * 1024 * 1024,
)
```

将 `context` 传给 agent，将 `result_storage_policy=storage` 传给 `Runner`。
每条结果独立使用 `max_tool_result_tokens`，包含状态、摘录、截断标记和恢复引用。
这个确定性的上限不受其它结果数量或上下文剩余空间影响；结果、策略、token counter 和
恢复可用性相同时，后续请求产生相同文本，保留这部分缓存前缀。生成摘录不会额外调用模型。

`max_total_tool_result_tokens` 自 0.9.18 起弃用且不再生效；仍接受并序列化该字段，
以便直接读取已保存策略。工具结果不再设独立总预算，历史摘要只由整体上下文压力触发。
大窗口模型不再仅因工具结果超过旧默认值 16,384 tokens 而提前摘要。

上下文窗口压力仍使用历史摘要和推理省略，不再对工具结果进行二次缩短。必要输入仍超过
硬输入预算时明确失败。请按模型窗口配置单条上限，并为并行结果、系统提示、工具定义、
用户输入和输出容量预留空间。内联结果首次被截断时写入溢出文件。
写入失败时保留存储告警，后续投影不会重试该次失败的写入或重复追加告警。

内联阈值独立生效：具备 `read_file` 时，超过 `max_inline_bytes` 的结果在接收时就写入文件，
早于展示截断。
展示摘录不会替换工具的结构化值，静态 DAG 值表达式仍读取恢复后的完整数据。

## 读取原文

需要模型恢复文本时，显式启用 `tool.read_file`；结果能否被截断也由它决定。SDK 不会自动
增加能力或扩大路径权限：读取溢出文件仍经过 `read_file` 自身的 boundary 检查。结果引用
相对于运行工作区，保存在所配置运行目录的 `results` 子目录。引用不是永久下载地址，
文件也可能已不存在。

使用绝对路径 `runtime_directory` 时，引用保留逻辑路径 `.runtime/...`，内置文件工具会
映射到外置存储。宿主读取真实文件时，使用
`runner.resolve_runtime_path(reference.path, workspace_path=...)`，不要直接拼接 workspace。
详见[外置运行存储](runner-and-configuration.md#将-sdk-私有运行文件放到工作区外)。

`tool.read_file` 保留从 1 开始的按行 `offset` 和 `limit`。
超长单行和精确继续读取可使用 `offset_chars`（从 0 开始的 Unicode 字符位置，
不包含文件开头的 UTF-8 BOM）和 `limit_chars`（默认 1024）。
字符模式不能与非默认按行参数混用。字符分页返回原样文本；运行时元信息携带继续位置，
模型展示进一步缩短时会按实际展示的连续前缀调整位置。读取结果文件不会递归生成新的结果文件。

`tool.grep` 和 `tool.list_files` 新增从 0 开始的条目 `offset` 和 `limit`，
单页上限仍分别为 200 条匹配、500 个条目。`list_files.value` 仍为本页条目列表。
分页会重新查询当前文件系统，不具备快照一致性；两次调用之间文件可能变化。

## 区分完整性和错误

执行状态与输出保留状态互相独立：工具可以执行成功，但保存的记录不完整或存在存储告警。

- `[EMPTY_RESULT]`：工具确实没有返回展示内容或产物。
- `[TRUNCATED]`：模型只看到了 SDK 已接收内容的摘录。
- `[SOURCE_TRUNCATED]`：源头受到限制，或返回的只是一个窗口。
- `[RECOVERY_UNAVAILABLE: no saved original]`：展示被截断，但没有写入溢出文件
  （写入失败、模型无法打开文件，或没有工具的验证器），不能假定省略内容可被重建。

shell 并发排空 stdout/stderr，以有界内存采集。未超过 shell 内联上限（100,000 字节且
2,000 行）的输出完整返回，不写文件。超过时，具备 `read_file` 才把完整输出流式写入一个
溢出文件，两路输出共用单次调用的采集上限，默认 64 MiB；超出采集上限后命令继续执行、
管道继续排空，已保存记录明确标为部分记录。没有 `read_file`，或输出中出现 NUL 时，只保留
各输出流的尾部，不留文件，并标 `[SOURCE_TRUNCATED]`。退出码、超时和取消状态保持真实。该上限不是整个工作区的磁盘配额。

展示文本保存发生 I/O 故障时，通过运行时事件回调发出 `tool_result_storage_warning`
（`Runner.stream` 中为 `capability.result.storage_warning`），
保持工具实际执行状态，并允许有界展示继续。降级情况下，已收到的文本可能超过正常阈值仍内联保留。
SDK 不会重跑工具。必需的二进制值或产物无法保存时，改为报告结果存储故障并阻止依赖执行；
DAG 返回失败结果；Tool 抛出公共异常 `dagent.RunExecutionError`，通过 `exc.result`
携带失败结果，流式接口则通过 `run.failed` 的 `event.data.result` 返回。
这些失败结果均返回 `checkpoint=None`；可序列化 trace 审计保留执行状态，
并通过 `retention.unavailable_fields` 标明未能保留的内存二进制值，该标记不是恢复引用。
详见[失败审计](results-streaming-review.md#审计-tool-执行失败)。

MCP 的保留保证仅覆盖 SDK 实际收到的内容，无法恢复远端已丢弃的数据，也不推断远端完整性。
SDK 不会自动下载远端资源。

## 压缩后查找结果

`ContextSummary.result_manifest` 指向不可变的逐行索引，包含较早的工具结果和调用。
运行时独立保留该入口，不依赖摘要模型记住路径。`result_archive_incomplete` 明确标记归档失败。
后续压缩继续保留之前的索引条目；之前的索引已打不开时，新索引不含其条目，并设置
`result_archive_incomplete`，压缩不会因此失败。压缩归档（压缩记录、源归档和该索引）是 SDK 跨 run
workspace 保留的那部分历史，其中保存被压缩的消息和原样的结果；带溢出文件的结果只归档
其路径，文件本身仍是尽力保存。

`CapabilityResult.retention` 和 `ToolResultMessage.retention` 包含类型化的源头完整性、
继续读取参数和存储告警，类型位于 `dagent.schemas.retention`。
规划观察条目位于 `UserMessage.result_observations`；它们是数据，不是伪造的工具回复。
`ContextUsage` 区分结果正文/元信息 tokens、源头截断和恢复不可用数量；
仅包含引用不算正文截断。

跨环境恢复 checkpoint 时，需要同时保留工作区；希望之后几轮仍能读取溢出文件时，每个会话
复用同一个 `workspace_path`。复制、保留期限、清理、脱敏和磁盘配额由宿主负责。
更积极的原文保存会增加磁盘占用，也可能保存敏感工具输出；关闭 Runner 不会自动删除这些文件。

离线示例：`uv run python -m examples.tool_result_recovery`。
示例的首次运行采用大于模型窗口的展示上限，验证自动缩短后仍可读取已保存的完整报告。

## 区分源窗口与模型展示

从 0.9.11 起，文件结果分别显示源范围和展示范围：

```text
[status=completed]
[TRUNCATED] model display shortened; received result has undisplayed text
[File window: chars=[0,20352); source_eof=true; unit=Unicode code points, zero-based, end-exclusive]
[Displayed: chars=[0,1900)]
[Continue with read_file: {"path": "report.txt", "offset_chars": 1900, "limit_chars": 1024}]
...原文第 0 到 1899 个字符...
```

这些数值仅示意，不是固定 token/字符换算。59,980 字节、20,352 字符、500 行文件
可以已由工具完整返回，但在 2048 token 预算下仅展示一部分，状态和游标也消耗预算。
`source_eof=true` 表示**返回窗口**到达 EOF，不代表模型看到了全部文件，也不代表
从中部开始的窗口覆盖了文件前缀。`source_eof=false` 与 `[SOURCE_TRUNCATED]` 表示
返回窗口后仍有源内容；`[TRUNCATED]` 独立表示模型展示缩短。仅源分页不算展示截断。

范围采用 Unicode 码点的 `[start,end)`，不是字节、行、字素簇或 token。初始 UTF-8
BOM 不计入，保留 LF/CRLF，CRLF 算两个码点。展示正文始终是返回窗口的连续前缀，
续读游标等于实际展示末尾，包括行窗口在行中间被裁剪的情况。EOF 且完整展示时没有
面向模型的续读提示；零正文时报告空展示区间，游标不前进。最低必要信息
无法装下时仍使用既有预算超限错误。

新增结果不再改变历史结果的展示范围和游标；整体上下文压力仍可触发旧完整交互的摘要。
`ResultRetention.window_start` / `window_length` 表示原始返回窗口，不是全局
阅读进度记录，也不撤销此前调用。应依据实际调用和已观察到的结果维护进度，同批
避免相同文件同区间请求；文件未变化时，不要仅因展示截断从头读取或换工具重读。
每次读取都重新访问文件系统，不是快照。

上述行为是工具说明和默认提示，不添加执行去重、自动补读、重试或恢复，全局预算
默认值不变。离线工具结果示例现在也打印这些面向模型的区间和游标。
