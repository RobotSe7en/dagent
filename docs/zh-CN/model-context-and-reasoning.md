# 模型上下文与推理

结果展示预算、类型化保留元信息及文件/搜索分页，见[工具结果恢复](tool-result-recovery.md)。

dagent 对私有 vLLM 模型使用统一的 provider-neutral conversation model，并在每次请求时
序列化为 OpenAI Chat Completions 或 Responses。Runtime 不持久化 provider response ID，
也不依赖 server-side state。

## 同一 run 与多个 run

一个 **run** 从一条用户输入开始，其中可以有多次模型与工具交互：

```text
用户 -> 推理 + 工具调用 -> 工具结果 -> 推理 + 工具调用 -> ... -> 最终回答
```

之后的新用户输入属于新 run，即使它继续使用同一个 `ConversationState`。默认策略是：

```python
agent = dagent.ToolAgent(
    profile="conversation",
    context=dagent.ContextPolicy(reasoning_replay="active_run"),
)
```

可选模式：

- `none`：从不把已保存 reasoning 放回模型输入；
- `active_run`：回放当前 run 先前步骤产生的 reasoning，让模型在工具结果后继续原计划，
  无需重新推导；
- `all_runs`：还回放 continued conversation 中更早用户 run 的 reasoning。

reasoning 始终保存在 `AssistantMessage.reasoning` 中供审计。回放策略只改变下一次请求的
投影，不会删除审计数据。

## 两种协议中的同一个逻辑请求

假设当前 run 包含用户请求、assistant reasoning 与工具调用，以及工具结果。Chat
Completions 对已识别的 vLLM 发送：

```json
[
  {"role": "system", "content": "..."},
  {"role": "user", "content": "查找发布版本。"},
  {
    "role": "assistant",
    "content": "",
    "reasoning": "我应该检查仓库。",
    "tool_calls": [{
      "id": "call_1",
      "type": "function",
      "function": {"name": "read_file", "arguments": "{\"path\":\"CHANGELOG.md\"}"}
    }]
  },
  {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "..."}
]
```

`chat_reasoning_field="reasoning_content"` 只会改变 assistant 回放字段名；`"omit"` 会
移除它；`"auto"` 对已识别的官方 DeepSeek V4 端点与模型选择 `reasoning_content`，
对 vLLM 选择 `reasoning`，其他情况选择 `omit`。

等价的无状态 Responses input 是：

```json
[
  {"role": "user", "content": "查找发布版本。"},
  {
    "type": "reasoning",
    "id": "rs_<稳定的本地摘要>",
    "summary": [],
    "content": [{"type": "reasoning_text", "text": "我应该检查仓库。"}]
  },
  {"type": "function_call", "id": "fc_<稳定的本地摘要>", "call_id": "call_1", "name": "read_file", "arguments": "{\"path\":\"CHANGELOG.md\"}"},
  {"type": "function_call_output", "call_id": "call_1", "output": "..."}
]
```

请求还会发送 `instructions`、展平后的 Responses function tools、`store=False` 和选定的
structured-output format。wire shape 所需 ID 从本地 conversation item ID 确定性生成，
并不是 vLLM response ID。dagent 不发送 `previous_response_id` 或 encrypted content。

用户发送下一条消息后，`active_run` 仍会包含以前的 assistant content 与工具观察，但会
省略它们的 reasoning；`all_runs` 才会继续保留这些 reasoning items。

## Reasoning 控制

```python
provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="Qwen/Qwen3-Coder",
    protocol="auto",
    reasoning_effort="medium",
    reasoning_capture="field_and_tags",
    context_window_tokens=None,
    max_output_tokens=None,
)
```

`reasoning_effort` 接受 `none`、`minimal`、`low`、`medium`、`high`、`xhigh` 或
`max`。SDK 默认对 Chat 发送 `reasoning_effort`，对 Responses 发送
`reasoning.effort`。具体级别是否生效仍由 vLLM 中实际部署的模型决定。

SDK 不提供 token 数形式的推理预算，只配置 effort。

`reasoning_capture="field_and_tags"` 合并专用 reasoning response 字段与 `<think>`
内容；`reasoning_capture="field"` 只信任专用字段。两种情况下 thinking tag 都不会残留
在可见正文中。capture 只控制响应解析，不会改变请求。

### 自定义思考字段与强度名称

自 0.9.19 起，当端点需要其他请求字段或强度名称时，设置 `reasoning_mapping`。
两种协议各有一份可选映射：

```python
from dagent.config import ReasoningMapping

provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="private-model",
    reasoning_effort="high",
    reasoning_mapping={
        "chat_completions": ReasoningMapping(
            field="custom_reasoning.level",
            values={"high": "deep"},
        ),
        "responses": ReasoningMapping(
            field="reasoning.effort",
            values={"high": "xhigh"},
        ),
    },
)
```

示例字段和值须按实际端点契约配置。Chat 发送
`{"custom_reasoning": {"level": "deep"}}`，Responses 发送
`{"reasoning": {"effort": "xhigh"}}`。Python 中也接受普通字典；对应的配置文件为：

```yaml
provider:
  base_url: http://localhost:8000/v1
  model: private-model
  reasoning_effort: high
  reasoning_mapping:
    chat_completions:
      field: custom_reasoning.level
      values: {high: deep}
    responses:
      field: reasoning.effort
      values: {high: xhigh}
```

`field` 必填，使用非空的点分隔 JSON 对象路径，不支持数组，也不能指向 SDK 管理的
模型、输入、工具、输出限制或 Responses 状态等字段。`values` 默认为空字典，键使用
已有的强度档位，目标值只允许字符串。未列出的档位原样发送。最终强度未设置时不发送
映射参数；字符串 `"none"` 仍是一个会发送的值。

映射覆盖普通生成、流式调用、Chat 入口和上下文压缩。压缩仍独立选择强度，默认 `low`，
所以上例的压缩请求继续发送 `low`；需要转换时在 `values` 中增加 `low` 项。
压缩策略和 `/tokenize` 的请求内容保持不变。

对已配置映射的协议，SDK 信任显式字段和值，不要求 OpenAPI 声明内置 effort 控制能力；
也不自动生成原来的 effort 字段或 `include_reasoning`，需要的开关通过额外参数显式提供。
端点、工具、流式、结构化输出、输出限制和历史 reasoning 回传检查继续执行。
能力查询报告仍如实反映服务端声明。

`auto` 保留现有协议优先级。没有配置映射的协议继续使用内置行为，即使另一种协议配置了
映射也是如此。需要只使用某种协议的映射时，请显式指定协议。不配置 `reasoning_mapping`、
设为 `None` 或空字典，都保留现有行为。

额外参数保留已有优先级：`extra_request_args` 可替换映射的整个顶层对象；`extra_body`
覆盖自动生成的 body 值，并在最终 JSON 中覆盖 SDK 参数。显式设置
`extra_request_args["extra_body"]` 会整体替换自动生成及配置的 body，其中未重复提供的
映射字段也会被移除。OpenAI 客户端不接受的自定义字段请放在 `extra_body` 中。

映射调用的元数据保留原始 `requested_reasoning_effort`，并从最终序列化请求的映射字段读取
`effective_reasoning_effort`；字段缺失或不是字符串时记为 `None`。`fallback_reason`
包含配置的字段路径。这只记录发送内容，不能证明模型实际采用了该强度。映射被拒绝时
不会自动降档、删去参数或换协议重发。

使用 `uv run python -m examples.reasoning_mapping` 运行
[离线映射示例](../../examples/reasoning_mapping.py)。

## 能力探测与协议选择

构造 `Provider(...)` 不访问网络。需要时可显式查看：

```python
capabilities = await provider.inspect_capabilities()
print(capabilities.model_dump())
```

报告用 `supported`、`unsupported` 和 `unknown` 描述 Chat、Responses、reasoning、
effort、输出限制、tools、streaming、structured output 与 `/tokenize`。探测只读取一次
`/openapi.json` 和 `/version`，随后使用缓存。

自动模式仅在 Responses 支持当前请求所需的全部能力时才优先选择它，包括 tools、
streaming、structured output 与 reasoning 控制；否则在 Chat 满足请求时选择 Chat，两个
协议都明确不满足时则在 POST 前报错。探测不可用时发出 warning 并选择 Chat。显式设置
`protocol="chat_completions"` 或 `"responses"` 是严格选择：endpoint 失败直接返回给调用者，
不会把可能具有副作用的请求换协议重放。

Responses generation 只有终态为 `completed` 才会被接受，流还必须包含
`response.completed`。Chat Completions 必须提供有效的 `finish_reason`（`stop`、
`tool_calls`，或带显式 refusal 的 `content_filter`）。缺少终止信息、输出耗尽（`length`），
以及 Responses 的 failed、incomplete、cancelled 都抛出
`dagent.providers.ProviderResponseError`。直接调用 Provider 时可读取其 `reason`、
`status` 和部分 `response`。工具参数 JSON 无效时同样返回 typed error，原因为
`invalid_tool_call`；推理文本绝不会转换成可执行调用。

ToolAgent 中，这些错误返回 `RunResult.status="failed"`，提供类型化 `result.error`，
`output_text` 为空，工具结果、最后 assistant 响应、用量和 trace 都会保留。
已完整生成但仅有推理或完全为空的响应也失败，原因分别为 `reasoning_only_response`
和 `empty_response`。普通工具错误仍交给模型进入下一轮；新的合法调用继续执行。
非空最终正文或显式拒绝正常结束循环。此契约校验模型回合是否有效，不能证明正文已达成
用户任务。协议失败不会重跑任务或此前工具，配置任务 validator 时也一样。

每个已记录的 `AssistantMessage.model_call` 都会暴露实际选择的协议、请求用途、请求值与
生效的 effort/输出限制、实际 wire 字段以及自动选择原因。这些审计元数据会随 conversation
持久化，但绝不会投影回模型输入。

相同元数据记录服务端实际返回的 `finish_reason` 和 `response_status`；缺失值为
`None`。`ModelTokenUsage` 缺失字段也为 `None`，服务端明确返回的零仍为零。
`ModelCallMetadata.attempts` 保存公开的 `ModelCallAttempt`：从 1 开始的次数、耗时秒数、
异常类型、HTTP 状态和重试前等待秒数。无法取得 HTTP 状态或不再重试时相应字段为
`None`。Runner 保持既有瞬态请求重试策略，流已输出 token 后不再重试。
OpenAI client 的自动重试被关闭，确保每次尝试均可观察。直接 Provider 调用只尝试一次；
传输或 HTTP 失败抛出 `dagent.providers.ProviderRequestError`，保留 `metadata`、
`cause` 和可能存在的部分 `response`。

## 提示缓存用量

自 0.10.0 起，缓存统计显式启用，来自服务端，独立于上下文 token 估算。
通过 `Provider(...)` 或 `Runner.from_config(...)` 使用的 provider 配置设置 `capture_cache_usage=True`；
默认值为 `False`。

```python
provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="local-model",
    capture_cache_usage=True,
    stream_include_usage=True,  # 仅对支持 Chat 流式 usage 的端点启用。
)
```

该选项不会发送新请求字段、开启服务端缓存、改变协议选择或自动启用流式 usage。
Chat 读取 `usage.prompt_tokens_details.cached_tokens`，Responses 读取
`usage.input_tokens_details.cached_tokens`，包括流式终止响应。
不映射 `prompt_cache_hit_tokens` 等私有字段。

`ModelTokenUsage.cached_input_tokens` 是现有 `input_tokens` 的一部分，不额外增加
token 消耗。关闭采集、字段缺失或统计非法时为 `None`；明确的 `0` 表示服务端报告
零命中。缺失统计不能当作零。开启后，负数、非整数（包括布尔值和数字字符串）、超过
已知输入数的缓存计数会被省略，并记录不含请求或响应正文的 warning；其它合法 usage
和生成结果保留。输入 token 数未知时，合法缓存数仍可单独保留。

逐响应统计通过 `AssistantMessage.usage` 读取，包括 `result.new_items` 中的 planner
审计消息。压缩保留独立的 `ContextSummary.usage` 和
`model_call.request_purpose="compaction"`。现有模型调用 trace 输出、最终结果和
checkpoint 序列化会保留计数。`ContextUsage` 描述请求估算和上下文投影，
`RunResult.usage` 统计操作次数，两者都不是缓存命中数。统计属于审计数据，不会被
回放到模型输入。

```python
for item in result.new_items:
    if isinstance(item, dagent.AssistantMessage) and item.usage is not None:
        usage = item.usage
        ratio = (
            usage.cached_input_tokens / usage.input_tokens
            if usage.cached_input_tokens is not None
            and usage.input_tokens is not None and usage.input_tokens > 0
            else None
        )
        print(usage.cached_input_tokens, ratio)  # None 表示未知或无法定义比例。
```

SDK 不提供 run 级缓存汇总或序列化命中率字段。宿主可根据已知计数计算比例，需要显式
排除未知调用并说明统计覆盖范围。流式 usage 快照替换前一快照，不累加。返回响应的
usage 不代表已覆盖全部失败重试的消耗。

缓存数未知时，序列化 usage 省略该新字段，保持已发布的默认记录结构。旧记录无需
迁移即可读取；包含显式启用字段的新记录需要支持该字段的读取方，旧版严格读取方可能
拒绝。可运行离线[缓存统计示例](../../examples/cache_usage.py)。

前缀复用仍取决于实际模型输入和服务端缓存状态。固定 workspace 可保持路径稳定，
但 `active_run` reasoning 省略、上下文压力、摘要或 DAG 规划历史重建仍可能改变前缀。
开启统计不改变这些策略。

## Token 计数与压缩

本节描述默认的 `compaction_strategy="legacy"`。下节显式启用的 ToolAgent 策略
使用最终请求计数和独立摘要预算。已有应用保留本节行为。

当 endpoint 已声明能力时，`token_counting="auto"` 会用 vLLM `/tokenize` 计算投影后的
messages 与 tools。此时 `ContextUsage.estimator` 为 `"vllm"`，
`server_max_model_len` 记录发现的上限。设置 `token_counting="vllm"` 会在无法精确计数时
报错；设置 `"heuristic"` 则始终使用本地确定性估算。

`/tokenize` 必须返回非负整数 `count` 或整数 token ID 列表。非空 messages/tools
不能接受零计数。无效结构和无效 `max_model_len` 在 `auto` 下发出 warning，改用明确
标注的 heuristic 计数；显式 `vllm` 则抛出
`dagent.providers.ProviderTokenCountError`。缺失字段不会被解释成精确的零。

`ContextUsage.context_window_source` 区分 `configured`、`server`、`model` 和
`fallback`，旧记录未观察到来源时为 `None`。兜底 131,072 只是本地预算，不是已经验证的
服务端能力。`server_max_model_len` 在有效探测返回上限之前保持 `None`；后续探测失败
或缺少上限字段时保留此前验证的值，计数器则可转为 heuristic。Provider 也公开
`context_window_source` 和 `server_max_model_len`。

`context_window_tokens=None` 时使用探测到的 `max_model_len`；探测失败会 warning 并
fallback 到 131,072（128K）。显式值覆盖自动值，但大于 server limit 时会在 generation 前被拒绝。
`max_output_tokens=None` 不发送输出限制；显式值映射到 Chat 已探测到的
`max_completion_tokens`/`max_tokens`，或 Responses 的 `max_output_tokens`。

对于 DeepSeek 官网 API（`https://api.deepseek.com`，可带 `/v1` 或 `/beta`），SDK
识别 `deepseek-v4-flash`、`deepseek-v4-pro`、`deepseek-v4-flash-vision-exp` 的 1M
上下文窗口。依据 2026-09-07 核对的[官方模型目录示例](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)，
采用 1,048,576-token 上限。这是官网端点与模型 ID 的匹配，不是实时长度查询：
DeepSeek `/models` 不提供上下文长度。未知模型回退到 128K；第三方端点不会套用官网上限。
显式配置优先，但不能超过已知模型上限。构造 Provider 仍不访问网络。
DeepSeek 的 `auto` 计数使用启发式估算，不调用 `/tokenize`；显式选择 `vllm` 会报错。
`ContextUsage.model_context_window_tokens` 记录已知模型上限，与
`server_max_model_len` 和 `estimator` 分开，避免把模型识别误认为精确 token 计数。
设置 `token_counting="heuristic"` 时仍会识别官网模型窗口。

总窗口为 `W`、输出上限为 `O` 时，输入预算是 `W - O`；未配置输出上限时为 `W - 1`。
vLLM 精确计数不增加安全系数，安全系数只应用于 heuristic/custom counter。

压缩依据 token 压力，而不是最低对话轮数。到达 trigger 后，dagent 按以下顺序缩减：

1. 汇总旧历史，并以最近 16% 原始历史作为保留目标；
2. 仅从 active request 投影移除最旧的已回放 reasoning；
3. 汇总过大 active run 中已经完成的中间步骤。

工具结果和 planner observations 独立使用固定的单条展示上限，新增结果或剩余窗口变少
都不再重新分配额度。独立的工具结果总预算已弃用且不再生效，只按整体上下文压力汇总旧完整交互。
这使保留结果的文本保持稳定，有利于缓存前缀复用。受保护的结果或其它必要输入仍放不下时，
在生成前抛出 `ContextWindowExceeded`。请按模型窗口和并行工具批次大小配置单条上限。
原文保存及续读方式见[工具结果恢复](tool-result-recovery.md)。

当前 run 的起始用户输入、未闭合的 assistant/tool-result chain 和最新原子步骤会保留，
tool-call/result pair 不会拆开。16% 保留目标是软目标：如果固定输入仍会造成硬超限，dagent
会先继续汇总最旧的跨 run 历史。如果缩减后必要输入仍超过有效窗口，会在 generation 前
抛出 `ContextWindowExceeded`。

默认在输入容量的 80% 触发压缩，这是软阈值：完成全部安全缩减后，只要尚未超过硬输入
预算，仍可发起请求。摘要默认最多生成 8,192 tokens，并独立使用
`compaction_reasoning_effort="low"`，不继承普通 Provider effort：

```python
context = dagent.ContextPolicy(
    compaction_trigger_ratio=0.8,
    compaction_retain_ratio=0.16,
    summary_max_tokens=8192,
    compaction_reasoning_effort="low",
)
```

摘要 reasoning 会被丢弃。如果摘要调用或该 effort 不受支持，dagent 会记录原因并使用
有界的确定性 fallback，不会因此中断 agent run。

`ContextUsage` 会报告回放模式、回放与省略的 reasoning 数量及 token 估算、active-run
压缩、精确/启发式 estimator、有效窗口和显式配置上限。

`context.compaction.finished` 仅报告实际的历史摘要压缩，包括确定性 fallback 摘要。
单纯保存工具结果或缩短展示不会发送此事件；请查看 `ContextUsage` 中的工具结果计数。
SDK 创建的子 agent 和辅助角色会继承 Provider 最后验证的服务端窗口，后续精确计数不可用时
也会保留该上限。`auto` 仍允许启发式计数；窗口完全未知时仍警告并使用 128K 兜底。
显式 `vllm` 计数继续采用严格模式。

## 有界的 ToolAgent 上下文策略

自 0.10.0 起，SDK 中的长 ToolAgent 任务可显式选择 `"chunked"` 或 `"cache_aware"`：

```python
provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="qwen-3.8-flash",  # 使用实际部署的模型名。
    context_window_tokens=131072,  # 已确认支持 256K 的部署可设为 262144。
    max_output_tokens=8192,
    token_counting="auto",
    capture_cache_usage=True,
    stream_include_usage=True,
    timeout_seconds=600,  # 示例：慢速本地模型的单次请求超时。
)
agent = dagent.ToolAgent(
    profile="conversation",
    context=dagent.ContextPolicy(
        compaction_strategy="cache_aware",
        allow_reference_fallback=True,
        compaction_timeout_seconds=600,  # 可选：一次压缩过程的总时限。
    ),
)
```

仍按普通方式在 `Runner` 注册 agent 所需工具。`chunked` 使用独立摘要请求。
`cache_aware` 优先在符合条件的完整历史前缀后追加摘要指令，并保留该请求原有的
system 和 tools；放不下或不兼容时改用独立分块。不会仅为复用缓存而把受保护的最新
交互也塞进摘要请求。固定的维护指令从首次 `cache_aware` 生成起就存在。
实际前缀复用仍受服务端模板、路由和缓存状态影响，不能保证命中。
这两种策略只作用于 SDK ToolAgent；DAG 和默认 `legacy` 保持已发布行为。
自定义 provider 需要实现内部 prepared-call 契约才能使用新策略。

### 会丢弃较早推理的聊天模板

有些聊天模板只渲染最后一条 user 消息之后的 assistant 推理，例如 Qwen3、QwQ 的模板。
在默认 `reasoning_replay="active_run"` 下（且服务端把回放的推理传给模板时），
generation prompt 包含本次 Run 的推理；`cache_aware` 摘要请求却在末尾追加一条 user
指令，模板随即丢弃这些推理，两个 prompt 从本次 Run 的第一个 assistant 轮次开始分叉。
用官方 Qwen3-8B 模板渲染一个五步 Run，前缀摘要请求与之前的 generation prompt 只共享
13%。使用这类模板时可以：

- 在 `cache_aware` 下配合 `reasoning_replay="none"`，两个请求渲染相同的历史，但模型
  在本次 Run 中不再看到自己之前的推理；或
- 改用 `chunked`，其独立摘要请求不依赖前缀。

开启 `capture_cache_usage=True` 后（摘要请求是流式的，Chat 端点还需
`stream_include_usage=True`），`cache_aware` 会检查每次前缀摘要由 provider 报告的缓存
命中；缓存的输入 token 不到一半时，本次 Run 剩余部分改用独立分块摘要，新的 Run 会再次
尝试前缀。缓存用量未知时不会改变路径。压缩记录的 `calls[].kind` 显示每次摘要走的路径。

### 同一请求计数与三条预算线

Provider 按额外参数优先级构造最终请求后将其冻结，计数与发送共用同一份不可变
payload；普通生成、摘要与流式请求均如此。新策略拒绝将已投影的模型、历史、
instructions 或 tools 替换为不同值的额外参数，也拒绝服务端静默截断输入和有状态
Responses 链。摘要请求还会拒绝与任务摘要不兼容的固定额外输出 schema；普通生成
仍保留受支持的额外输出格式覆盖。

严格预算也拒绝显式 `truncation=null`；请省略该字段或使用 `"disabled"`。render 在
返回计数前拒绝自动输出预留时，可通过受支持的 tokenizer 单独发现窗口，再重新冻结
自动输出预算并 render 最终请求。仅窗口探测不是生成输入计数，显式输出上限不会
被自动缩小。

设有效总窗口为 `W`，强制生效的输出预留为 `O`。新策略取有效的配置值、已发现服务端
上限与已知模型上限的最小值。服务端窗口更小时缩小预算，并在生成前重新冻结自动输出
上限，不会扩大服务端容量。Reasoning 可能消耗 `O`，不会另得
一份额度。硬输入预算 `H = W - O`，默认软触发线为 `floor(0.8 * H)`。
压缩尽量留出接近 `floor(0.65 * H)` 的空间；必要输入无法再缩减时，仍可接受高于
目标但不超过硬上限的请求。软触发时，`compaction_retain_ratio` 按完整组保留最近历史，
默认目标 16%，至少保留最新一组。软压力下还会等到选中的较早历史值得总结（按其在已计数
请求中的占比折算，约为 `H` 的 10%）再压缩，避免必要输入持续高于触发线时每一步都付费
摘要并打断前缀。硬压力下立即压缩，并可继续压缩这部分近期历史，直到受保护的最新组。
发送 generation 前重新校验整个请求，缓存命中不会增大 `H`。

| 已确认的总窗口 | 输出预留 | 硬输入预算 | 默认软触发线 |
| --- | --- | --- | --- |
| 131,072（128K） | 8,192 | 122,880 | 98,304 |
| 262,144（256K） | 8,192 | 253,952 | 203,161 |

与 `legacy` 不同，新策略未配置普通输出上限时，会在实际请求里设置
`min(8192, W // 8)`。最终额外参数中的显式输出上限仍按原优先级生效，并计入预算。
`summary_max_tokens` 限制可见摘要正文；每次摘要调用独立预留最多
`min(2 * requested_summary_tokens, W // 4)` 的总输出 token，包括 reasoning，
不会被普通生成较小的上限再次压低。这不保证一定获得所请求长度的可见正文。
摘要目标还会按最终 generation 必须保留的输入所剩空间缩小。

计数优先使用探测到的 vLLM `/v1/chat/completions/render` 或
`/v1/responses/render`，提交完整最终 payload，并依据返回的 prompt token IDs 计数。
其次使用兼容的 `/tokenize` 请求；工具选择渲染或模板选项不一致时，不会声称精确。
`auto` 允许本地估算并加 `token_safety_margin`（默认 15%）；显式 `vllm` 在没有
受支持的服务端计数路径时失败。`ContextUsage.count_precision` 区分 `rendered`、
`tokenized_estimate` 和 `estimated`，独立于 `estimator`。
`request_fingerprint` 标识被冻结的请求体，不是服务端缓存键。

本地估算不会取代可用的服务端计数：它的高估可能超过安全余量，中文文本就是常见情况。
因此 generation 候选和压缩请求（包括 `cache_aware` 前缀请求）都会先计数，再决定
是否压缩或拆分。探测到的服务端窗口只会缩小有效窗口。

Render 能力取决于版本与部署方式。[已核对的 vLLM 源码](https://github.com/vllm-project/vllm/blob/428cd0cb4a42750f5339d08bb67d534cf8051429/vllm/entrypoints/scale_out/render/api_router.py)
使用完整 generation 请求 schema；普通服务需要 `--enable-scale-out` 才开放可用的
render handler。dagent 会探测，不假设部署一定支持。`/tokenize` 不能通用替代包含
所有 generation 选项的渲染。已知超预算请求会在生成前缩减或拒绝；未知计数、服务端
配置不一致或部署变化仍可能造成服务端上下文错误。SDK 不把任意 HTTP 400 归类为
上下文超限，也不依据错误文本猜测后重放 generation。

render 会校验输入长度，超窗口的请求体会被拒绝而不是计数。此时抛出
`dagent.providers.openai_compatible.ProviderRenderRejected`，它是
`ProviderTokenCountError` 的子类，现有异常处理不受影响。若本地估算已判定该候选超预算，
此拒绝按超限处理：SDK 压缩 generation 输入或拆分压缩请求，不会发送被拒绝的请求；
本地估算认为能放下的候选被拒绝时，仍作为诊断错误失败。tokenizer 对被拒请求的计数
超过输入预算时（包括带工具而不精确的情况），以 `tokenized_estimate` 报告。

### 完整来源覆盖与稳定结果展示

摘要连续覆盖选中的来源；过大的来源按明确范围分块，不先丢弃中间部分。
结构化摘要保存目标、约束、决策、已完成步骤、有关工具观察、未完成工作和来源引用，
帮助模型记住已做事项；不会据此去重或禁止工具调用。收到的原始内容、摘要覆盖范围和
各次模型调用 usage 都会归档。

每个摘要请求都会写明大小上限：可见摘要预算的 80%，以 token 及约合的英文字符数、
中文字数表示，便于模型合并或缩短较早条目。渲染后仍超出预算的摘要会在同一次压缩中
带着实测大小重试一次（计入本次调用预算），第二次仍超出则照旧失败。条目必须引用
提供的来源 ID：从上一份摘要内部抄来的 ID 会映射为该摘要自身的来源 ID，其他未知 ID
会被去掉；某条目去掉后不剩任何有效 ID 时，整份摘要仍会被拒绝。

一次压缩最多进行 8 次逻辑模型调用、12 次模型请求尝试（含重试）；这些限制不约束
普通生成次数或整个任务持续时间。摘要在内部通过流收集，不作为 agent 答案输出。
`compaction_timeout_seconds` 默认为 `None`，不额外施加总时限，仍受 provider
单次请求超时约束。显式设置后限制整次压缩（含重试）；慢速自托管模型还需单独调大
provider timeout。取消会继续传播。未变化且已失败的来源不会每轮重复总结，
不能收敛的替换也不会形成无限摘要循环。
只有摘要服务确实响应过时，来源才会记为失败：摘要无效或超长、压缩预算或时限耗尽，
或终止性的 re-preparation 信号。请求重试后仍然存在的传输失败（连接错误、超时、
HTTP 408/409/425/429 或 5xx）即使发生在部分分块已完成之后，也不代表来源本身有问题：
本次压缩停止调用摘要服务（失败的前缀请求不会再改走分块），对话中不记录任何失败，本次已
付费的分块也不复用。本次 Run 剩余时间里软压力不再重试该来源，避免服务中断时每一步都
增加重试等待；硬压力下（否则会以 `ContextWindowExceeded` 结束 Run）会重试，新的 Run 或
续跑也会重试。硬压力且 `allow_reference_fallback=True` 时，摘要失败仍适用下文的引用续接。
`0.10.0` 至 `0.10.2a0` 保存的对话可能已把这类来源标为失败；如需重试，可从
`conversation.model_copy(update={"compaction_state": None})` 继续，或开启
`allow_reference_fallback=True`。
最终计数仍超过硬预算时，同一压缩过程最多再收紧一次当前根摘要，不重新发送历史原文。

新收到的工具结果在第一次交给 generation 展示前共享本批可用预算。
新策略中 `max_tool_result_tokens` 限制每条结果的常规正文额度，必要的身份、状态和
恢复元数据另计。持久化 receipt 冻结实际展示文本、已显示范围和续读 cursor。
后续步骤、review 续跑与新 run 复用该文本，不会逐轮重新分配。
`max_total_tool_result_tokens` 仍已弃用且忽略。其它缩减后仍硬超限时，最新的已冻结
批次可明确地单向转为仅引用展示一次，记录
`prefix_break_reason="emergency_result_reference"`。全部 call、参数、状态及对应
result 消息仍保留。

人工审批反馈独立于外置工具正文保留，作为必要展示元数据计入预算。紧急缩短仍保留
反馈，它不属于可截断的正文额度，也不改变正文字符位置。

只有通过 agent 现有且受边界检查的恢复工具真实可读的原文才能缩短，SDK 不会自动
添加工具。不可补读的正文保持完整，包括内置文件读取器不能恢复的含 NUL 文本；
预算不足时会明确失败。补读只能恢复 SDK 收到的内容，不能找回上游
已经截断的部分。当前用户指令和最新完整 call/result 关系受保护，因此 system、工具
schema、用户输入、调用参数或最小结果元数据本身过大时，仍可能抛出
`ContextWindowExceeded`。

旧的冻结展示如果隐藏了正文，也必须仍能恢复；读取工具或来源不可用时，准备阶段明确
失败并保留原 receipt。已经完整展示正文的记录仍可使用。

`allow_reference_fallback=False` 在完整语义摘要失败时保留原始来源；请求仍放得下就
可以继续。设为 `True` 后，还允许在必须缩减才能容纳时以较低保真的引用继续：只替换
可恢复来源，未总结范围明确记录，最终硬校验仍执行。
`ContextSummary.method="reference_fallback"` 与归档 record 的 `partial` 或
`reference_only` 状态将其与完整摘要区分。两种配置都不承诺语义无损或超限错误归零。

旧的部分摘要只在输入匹配时复用。其他压缩更新总摘要后，已启用的引用降级保留当前
来源及明确的未总结范围，不对同一来源重复发起模型请求。

运行期归档索引跨上下文准备复用已验证的引用关系。每轮仍检查文件身份，未变化的
旧正文不再重复读取、计算哈希或解析；文件状态变化时重新校验，缺失或损坏明确失败。
新运行期或工作区重新校验历史。正文读取减少，但文件状态遍历仍随历史量增长；索引不缓存全部
历史正文。

在 Run 边界，同一个 `Runner` 的各次 Run 共用一个对话资源存储（Run 结束后保存保留的结果，
下次 Run 开始前恢复），并记录已校验文件的身份。续用对话时再次保存或恢复，未变化的保留结果
不会重新读取；变化的文件重新校验，缺失或损坏仍明确失败。恢复到新工作区仍需复制（因而读取）
每个保留结果一次。新的 `Runner` 或进程会重新校验历史。

### 失败快照与缓存验证

新策略准备上下文失败时，原异常类型保留，其 `result` 带有失败结果及最新已执行
工具批次。已解析的执行配置还会生成 checkpoint，可从 result 或
`Runner.run_checkpoint(run_id)` 取得。如果后续发现的服务端窗口使显式输出上限失效，
快照保留最后有效的生成配置；初始配置本身非法时没有已解析 checkpoint。
摘要成功后准备或计数失败，快照也保留已付费记录。新 run 只在输入匹配时复用完整
成果，提交精简历史前仍必须通过最终预算检查；原异常类型和取消语义保持不变。
解决大小或能力问题后，使用
`Runner.run(agent, input="Continue from the saved progress.",
conversation=error.result.conversation)` 开始**新 run**。这是通过会话续聊，不是
同 run 恢复，也不会自动重放已完成工具。请保留来源 workspace 和持久化资源。
恢复文件缺失或损坏会明确失败，单有引用不能代替实际来源数据。

验证 vLLM 缓存时，需在服务端启用前缀缓存及 `--enable-prompt-tokens-details`；
后者决定是否返回标准 `cached_tokens`，见[已核对的实现](https://github.com/vllm-project/vllm/blob/428cd0cb4a42750f5339d08bb67d534cf8051429/vllm/entrypoints/openai/chat_completion/serving.py)。
SDK 配置 `capture_cache_usage=True`；支持 Chat 流式 usage 时再启用
`stream_include_usage=True`。把两次足够长且相同的请求固定到同一 pod，对比冷、热
请求的服务端缓存计数。统计缺失表示未知。SDK 不修改服务端开关，不清生产缓存，也
不保证命中。还应统计未缓存输入、摘要输入/输出和延迟，单独提高命中比例不代表总成本降低。

新增 policy 和审计字段取默认值时不进入持久化记录，保持旧默认 checkpoint 的
fingerprint。启用新策略、冻结展示或摘要 manifest 的记录，需要支持这些字段的读取方。

运行[离线上下文示例](../../examples/context_management.py)：
`uv run python -m examples.context_management`。实际部署可使用
[vLLM 长时验证脚本](../../examples/vllm_context_soak.py)，默认验收目标为一小时、
200 次工具执行和 5 次压缩：

```bash
uv run python -m examples.vllm_context_soak --live \
  --base-url http://localhost:8000/v1 --model MODEL --context-window 128 \
  --workspace /tmp/dagent-vllm-soak --report /tmp/dagent-vllm-soak.json
```

已确认的 256K 部署使用 `--context-window 256`。脚本使用
`token_counting="vllm"`，要求服务端计数可用；`/tokenize` 无法匹配实际生成选项时，
需要开放 render handler。由部署方配置工具模板、APC、usage details 和所需的 API key
环境变量。没有 `--live` 不发送请求；统计缺失或缩短的 smoke 参数不能算完整验收通过。
报告记录实际预算、usage 和执行进度，conversation/checkpoint 文件保存合成任务的可恢复
状态。脚本不会重置共享缓存。

## Custom provider 兼容

已有的自定义 provider 只要实现 `chat(...)` 和可选的 `stream_chat(...)`，仍可通过明确的
内部 adapter 使用。它们会收到普通 Chat messages/tools shape；但 SDK 无法推断其接受的
reasoning input 字段，因此会省略 provider-specific reasoning 回放。需要双协议能力时，
请使用面向私有 vLLM 的内置 `Provider`。

## DeepSeek Chat 工具推理回传

从 0.9.11 起，`auto` 识别 HTTPS `api.deepseek.com`（默认或 443 端口，根路径、
`/v1` 或 `/beta`）上的 `deepseek-v4-flash`、`deepseek-v4-pro`、
`deepseek-v4-flash-vision-exp`。显式字段配置始终优先；未知模型及第三方端点
不会仅凭模型名获得该协议映射。

[官方思考指南](https://api-docs.deepseek.com/guides/thinking_mode/)要求携带 `tools`
的后续请求完整回传 `reasoning_content`，包括没有调用工具的 assistant 轮次；
不携带工具时服务端可能忽略推理。协议核对日期：2026-09-07；另见
[模型列表](https://api-docs.deepseek.com/)、
[/v1 示例](https://api-docs.deepseek.com/quick_start/agent_integrations/workbuddy/)、
[beta 工具协议](https://api-docs.deepseek.com/guides/tool_calls/)。

本项仅修复字段映射。`active_run` 仍排除其他 Run 的推理，预算压力仍可能在保留
消息时单独省略较早推理，因此这些请求可能不满足官方完整回传要求。`none` 和
显式 `omit` 仍生效，即使服务端可能拒绝这样的工具请求。不能由此断言推理省略
导致了重复读取，也不能从请求携带推理推断服务端实际使用了推理。

小额验收使用合成数据，最多三次生成请求：
`DAGENT_RUN_DEEPSEEK_TESTS=1 uv run python -m examples.deepseek_replay`。
通过 `API_KEY` 提供测试凭据；示例只输出计数和验证结果，不输出推理、完整请求
或凭据。验收使用 `reasoning_effort="high"`；空推理响应无法验证推理回传。
每次生成最多 1024 输出 token，禁用重试；失败即停，输出截断也视为
验收失败，不自动增加预算重试。

## 观察请求实际携带的推理

`ContextUsage.replayed_reasoning_items` / `replayed_reasoning_tokens` 保留既有
语义：内部上下文投影中保留的推理。`omitted_reasoning_*` 统计投影省略；这些计数
都不能证明最终 Chat 字段已序列化，也不重新定义历史统计。

从 0.9.11 起，`AssistantMessage.model_call.request_reasoning`（压缩调用的
model-call 元数据中也可用）在 `extra_request_args`、`extra_body` 全部覆盖后，
从最终 HTTP 请求体统计推理文本：

| 字段 | 含义 |
| --- | --- |
| `resolved_field` | Provider 映射结果：`reasoning`、`reasoning_content` 或 `omit`。 |
| `serialized_fields` | 实际非空文本字段；没有时为 `("omit",)`。透传覆盖若携带两种 Chat 字段，则同时列出。 |
| `serialized_items` | 非空 assistant 推理字段数，或含文本的 Responses reasoning 输入项数。 |
| `serialized_characters` | JSON 解码后的 Unicode 码点数之和；不含转义和包装，不是 token 数。 |
| `omission_reasons` | 上下文或序列化省略原因，可同时出现多项。 |

原因枚举为 `policy_none`、`outside_active_run`、`context_budget`、
`explicit_omit`、`auto_unsupported`、`request_override`、`no_reasoning_available`。
上下文原因描述交给 Provider 的投影；透传覆盖之后仍可能注入不同内容。Responses
统计 reasoning 项的 `content` 和 `summary` 文本，每项计一条；不统计
`reasoning.effort`、加密数据或本次响应生成的推理。空推理字符串计零。

```python
for item in result.conversation.items:
    if isinstance(item, dagent.AssistantMessage) and item.model_call:
        observation = item.model_call.request_reasoning
        if observation is not None:
            print(observation.model_dump(mode="json"))
```

`RequestReasoning` 位于 `dagent.schemas.context`，不在包根重新导出。旧记录及
未实现请求观测的 Provider 缺少摘要时表示**未知**，不是实际发送零条。摘要随正常
响应元数据返回，包括流式完成结果；不另建失败请求或传输尝试日志。不额外记录
推理正文、完整请求和凭据，只证明请求携带，不证明服务端使用。

## Provider 包装与观测

Provider 包装可使用受支持的 `dagent.providers.ModelRequest`、
`PreparedModelCall` 和 `ProviderReprepareRequired`。包装识别明确的请求拒绝并更新
准备策略后，可从 `count_prepared`、`complete_prepared` 或 `stream_prepared`
抛出 `ProviderReprepareRequired(cause, metadata=call.metadata)`，前提是尚未输出响应事件。

对于显式启用的 `cache_aware` 和 `chunked` ToolAgent 策略，SDK 会以同一份已投影
`ModelRequest` 和 stream 模式调用原 request preparer，对替换请求重新计数，并按
**最终请求的实际输出上限**和有效上下文窗口检查预算，包括计数时发现的窗口上限。
最终发送通过检查的同一个 `PreparedModelCall`，包装不能在发送时静默替换它。
替换请求超预算时，继续按现有上下文策略压缩或在发送前失败。保留已完成工具与已付费
摘要，不重放工具或整个 Run。

同一机制覆盖生成阶段请求重准备后的再次计数、受保护候选的最终强制计数，以及压缩
请求的首次计数和替换计数。每条计数链最多替换四次（最多五次计数），fingerprint
不变或重复时抛出触发该次替换的 `ProviderReprepareRequired`。发送阶段保留原有
最多四次替换及 fingerprint 检查。摘要生成尝试继续共享一次压缩的十二次请求额度，
准备与计数也受压缩截止时间约束。普通 `ProviderTokenCountError` 和其它错误仍按
原有规则处理，不会触发重新准备。
压缩中途因计数重准备信号终止时，已完成分块会保存为部分压缩记录，并关联到失败会话，
供后续显式引用恢复；启用降级也不会吞掉原类型化信号。替换请求超预算时，失败结果包含
该请求的 `ContextUsage`，流式结果同样保留。

拒绝识别、参数降级规则（例如 `high` → `medium`）、拒绝缓存的作用域与有效期均由
上层包装负责，SDK 不内置模型专属降级或 HTTP 400 自动重试。通过
`Runner(provider=wrapper, ...)` 接入，让包装按当前策略委托原 Provider 准备请求，
仅在策略已更新后抛出信号。直接调用 Provider 不会执行这套 SDK 预算循环。
可运行离线[计数阶段重准备示例](../../examples/prepared_count_reprepare.py)：

```bash
uv run python -m examples.prepared_count_reprepare
```

示例识别一种模拟 render 拒绝，在包装内缓存决策，下次准备选择 `medium`，不修改原
Provider 配置；无法识别的错误原样抛出。已有应用和包装无需修改，除非主动使用该信号。

`ModelRequest(omit_reasoning_effort=True)` 显式省略思考强度，不继承 Provider 默认值，
并移除额外参数中的当前映射强度字段和标准强度字段。其它嵌套参数和显式思考开关保留，
不会修改 Provider 配置。省略为显式行为，本身不增加重试、不改变映射或协议选择。

`Runner(context_observability=True)` 为 ToolAgent 开启模型调用观测及压缩进度。
`model.call.observed` 包含唯一请求 ID、用途、可用 usage、元数据、状态与耗时，
不包含提示或响应正文；失败请求的 usage 可能未知。
`context.compaction.progress` 上报内部摘要的真实进展，每秒最多一次；
`context.compaction.ended` 在失败或取消时也结束当前压缩过程。
`context.compaction.finished` 仍仅表示实际发生上下文替换。
默认不发送新增观测事件，SDK 旧默认、legacy 压缩默认、4/6/9 状态版本和默认
checkpoint fingerprint 保持不变。启用观测时 legacy 摘要使用内部流式调用，
fallback 策略不变。
带作用域的 emitter 不会改变上述观测事件结构；父能力、DAG 和节点归属信息继续通过
response、capability 事件提供。

流式上下文失败现在也传递原异常已经附带的类型化失败 result，保留与非流式调用一致的
失败证据；不增加同 Run 或跨服务重启恢复。
