# 恢复较大的工具结果

自 0.9.9 起，工具结果采用“最低信息优先”的展示预算，覆盖 ToolAgent
回复和结构化 DAG 规划观察。

## 分别配置展示与存储

```python
import dagent

context = dagent.ContextPolicy(
    max_tool_result_tokens=2048,
    max_total_tool_result_tokens=16384,
)
storage = dagent.ResultStoragePolicy(
    max_inline_bytes=256 * 1024,
    max_shell_output_bytes=64 * 1024 * 1024,
)
```

将 `context` 传给 agent，将 `result_storage_policy=storage` 传给 `Runner`。
展示预算包含状态、摘录、截断标记和恢复引用。每条结果先获得简短的确定性摘录，
剩余预算再从新到旧分配；生成摘录不会额外调用模型。
最低信息放不下时压缩已完成的原子交互；受保护的最新交互仍放不下时，
生成前抛出 `ContextWindowExceeded`，不会悄悄将结果替换为空字符串。

内联阈值独立生效：小于阈值的结果也可能在模型展示截断前保存。
展示摘录不会替换工具的结构化值，静态 DAG 值表达式仍读取恢复后的完整数据。

## 读取原文

需要模型恢复文本时，显式启用 `tool.read_file`。SDK 不会自动增加能力或扩大路径权限。
结果引用相对于运行工作区，保存在所配置运行目录的 `results` 子目录。
直接读取能力不可用、路径被拒绝或文件缺失时，模型会得到提示；引用不是永久下载地址。

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
- `[RECOVERY_UNAVAILABLE]`：缺少原文记录或所需读取权限，不能假定省略内容可被重建。

shell 并发排空 stdout/stderr，以有界内存采集，两者共用单次调用的采集上限，默认 64 MiB。
超限后命令继续执行、管道继续排空，并保留有界尾部；已保存记录明确标为部分记录。
退出码、超时和取消状态保持真实。该上限不是整个工作区的磁盘配额。

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
后续压缩继续保留之前的索引条目。

`CapabilityResult.retention` 和 `ToolResultMessage.retention` 包含类型化的源头完整性、
继续读取参数和存储告警，类型位于 `dagent.schemas.retention`。
规划观察条目位于 `UserMessage.result_observations`；它们是数据，不是伪造的工具回复。
`ContextUsage` 区分结果正文/元信息 tokens、源头截断和恢复不可用数量；
仅包含引用不算正文截断。

跨环境恢复 checkpoint 时，需要同时保留工作区。复制、保留期限、清理、脱敏和磁盘配额由宿主负责。
更积极的原文保存会增加磁盘占用，也可能保存敏感工具输出；关闭 Runner 不会自动删除这些文件。

离线示例：`uv run python -m examples.tool_result_recovery`。

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

总工具结果预算可能使后续请求中的历史结果缩短，展示范围和游标随请求重算。
`ResultRetention.window_start` / `window_length` 仍表示原始返回窗口，不是全局
阅读进度记录，也不撤销此前调用。应依据实际调用和已观察到的结果维护进度，同批
避免相同文件同区间请求；文件未变化时，不要仅因展示截断从头读取或换工具重读。
每次读取都重新访问文件系统，不是快照。

上述行为是工具说明和默认提示，不添加执行去重、自动补读、重试或恢复，全局预算
默认值不变。离线工具结果示例现在也打印这些面向模型的区间和游标。
