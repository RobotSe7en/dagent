# 模型上下文与推理

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
移除它；`"auto"` 对 vLLM 选择 `reasoning`，对未知 server 选择 `omit`。

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
    reasoning={
        "effort": "medium",
        "budget_tokens": 2048,
        "capture": "field_and_tags",
    },
)
```

`effort` 接受 `none`、`minimal`、`low`、`medium`、`high`、`xhigh` 或 `max`。SDK 对
Chat 发送 `reasoning_effort`，对 Responses 发送 `reasoning.effort`。具体级别是否生效
仍由 vLLM 中实际部署的模型决定。

`budget_tokens` 必须是正整数，映射为 vLLM 的 `thinking_token_budget`。只有所选协议的
已探测 schema 包含该字段时 SDK 才会发送。能力不支持或未知时会产生
`ProviderCapabilityWarning`，并在不带该字段的情况下继续请求。`auto` 模式下，如果只有
Chat 暴露 budget 能力，就选择 Chat。

`capture="field_and_tags"` 合并专用 reasoning response 字段与 `<think>` 内容；
`capture="field"` 只信任专用字段。两种情况下 thinking tag 都不会残留在可见正文中。

## 能力探测与协议选择

构造 `Provider(...)` 不访问网络。需要时可显式查看：

```python
capabilities = await provider.inspect_capabilities()
print(capabilities.model_dump())
```

报告用 `supported`、`unsupported` 和 `unknown` 描述 Chat、Responses、reasoning、
effort、budget、tools、streaming、structured output 与 `/tokenize`。探测只读取一次
`/openapi.json` 和 `/version`，随后使用缓存。

自动模式优先选择 Responses；只有已请求的 budget 明确仅能通过 Chat 使用时才选择 Chat。
探测不可用时发出 warning 并选择 Chat。显式设置
`protocol="chat_completions"` 或 `"responses"` 是严格选择：endpoint 失败直接返回给调用者，
不会把可能具有副作用的请求换协议重放。

每个已记录的 `AssistantMessage.model_call` 都会暴露实际选择的协议、请求值与生效的
effort/budget、被忽略的参数以及自动选择原因。这些审计元数据会随 conversation 持久化，
但绝不会投影回模型输入。

## Token 计数与压缩

当 endpoint 已声明能力时，`token_counting="auto"` 会用 vLLM `/tokenize` 计算投影后的
messages 与 tools。此时 `ContextUsage.estimator` 为 `"vllm"`，
`server_max_model_len` 记录发现的上限。设置 `token_counting="vllm"` 会在无法精确计数时
报错；设置 `"heuristic"` 则始终使用本地确定性估算。

压缩依据 token 压力，而不是最低对话轮数。到达 trigger 后，dagent 按以下顺序缩减：

1. 汇总旧 run 中的完整 items；
2. 仅从 active request 投影移除最旧的已回放 reasoning；
3. 汇总过大 active run 中已经完成的中间步骤。

当前 run 的起始用户输入、未闭合的 assistant/tool-result chain 和最新原子步骤会保留，
tool-call/result pair 不会拆开。如果缩减后必要输入仍超过有效窗口，会在 generation 前抛出
`ContextWindowExceeded`。

`ContextUsage` 会报告回放模式、回放与省略的 reasoning 数量及 token 估算、active-run
压缩、精确/启发式 estimator、有效窗口和显式配置上限。

## Custom provider 兼容

已有的自定义 provider 只要实现 `chat(...)` 和可选的 `stream_chat(...)`，仍可通过明确的
内部 adapter 使用。它们会收到普通 Chat messages/tools shape；但 SDK 无法推断其接受的
reasoning input 字段，因此会省略 provider-specific reasoning 回放。需要双协议能力时，
请使用面向私有 vLLM 的内置 `Provider`。
