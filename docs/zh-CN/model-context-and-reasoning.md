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
`max`。SDK 对 Chat 发送 `reasoning_effort`，对 Responses 发送
`reasoning.effort`。具体级别是否生效仍由 vLLM 中实际部署的模型决定。

SDK 不提供 token 数形式的推理预算，只配置 effort。

`reasoning_capture="field_and_tags"` 合并专用 reasoning response 字段与 `<think>`
内容；`reasoning_capture="field"` 只信任专用字段。两种情况下 thinking tag 都不会残留
在可见正文中。capture 只控制响应解析，不会改变请求。

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

## Token 计数与压缩

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
