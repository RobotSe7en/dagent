# 让模型读取图片

本功能属于尚未发布的 SDK 更新。普通安装已包含基础依赖 Pillow；需要选择支持视觉输入的
模型与端点。

## 显式上传视觉图片

继续使用现有上传入口，逐张声明需要让模型查看的图片：

```python
from pathlib import Path
import dagent

runner = dagent.Runner(provider=provider, workspace="agent-workspace")
agent = dagent.ToolAgent(profile="conversation", capabilities=[])
result = await runner.run(
    agent,
    input="比较这两张截图。",
    input_uploads=[
        dagent.ArtifactUpload("before.png", Path("before.png").read_bytes(), model_input="image"),
        dagent.ArtifactUpload("after.png", Path("after.png").read_bytes(), model_input="image"),
    ],
)
```

`model_input` 是仅限关键字的可选参数，默认 `None`。未标记的文件保持既有上传行为：
模型只收到文件元数据，即使文件本身是图片。已标记图片位于请求文字之后，顺序与上传列表一致。
支持多图、图片和普通文件混合上传；`Runner.stream` 接受相同参数。纯图片消息使用
`input=""`；`input=None` 仍表示既有的持久化续跑，不能携带新上传。

支持 PNG、JPEG、WebP 和非动画 GIF。SDK 校验真实文件内容；显式声明的 `media_type`
必须匹配。损坏文件、动画和不支持的格式会明确失败。首版保持原图字节，不自动缩放、
转换格式或覆盖端点的 `detail` 默认值。

显式视觉请求中，覆盖 `messages` 或 `input` 仍需保留已选图片及顺序，冲突会在生成前报错。
其他参数覆盖优先级保持不变。

## 让 Agent 按需读取工作区图片

图片读取需要显式注册 capability：

```python
runner.add_image_tools()
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["tool.read_image"],
)
result = await runner.run(agent, input="读取 uploads/screenshot.png 并描述它。")
```

`tool.read_image(path)` 遵循现有文件边界和审核规则，是可并行的只读工具，并支持内置沙箱
传输。使用新 Runner 恢复包含该工具的 checkpoint 时，需要重新注册。注册前不会改变
默认工具目录。

Python 函数可以直接返回类型化图片：

```python
@dagent.tool
def screenshot() -> dagent.ImageData:
    return dagent.ImageData.from_bytes(capture_png(), media_type="image/png")
```

自定义 capability handler 可以返回 `CapabilityResult.completed(...,
images=[ImageData.from_bytes(...)])`；已有校验快照也可以通过 `ImageReference` 返回。
普通 bytes 和其他 Pydantic 工具返回值保留原有含义。

## 接收 MCP 图片

按 MCP server 显式启用视觉结果：

```python
runner.add_mcp_server("browser", {
    "command": "your-browser-mcp",
    "args": [],
    "image_results_as_input": True,
})
```

该选项默认 `False`，既有图片 artifact 仍会保存，但不自动进入视觉上下文。启用后，图片块
经过校验，快照与 artifact 共用引用。音频及其他 artifact 行为保持不变。图片继续关联原始
工具调用：Responses 使用原生图片工具输出；Chat Completions 在该组工具回复结束后附加
带调用来源的图片数据。

## 在静态 DAG 中使用图片

`artifact_uploads` 保持文件语义。将 artifact 显式声明为 Agent 节点输入，并为该 Agent 配置
`tool.read_image`，节点即可查看边界允许的文件。`model_input="image"` 仅用于
`input_uploads`，在 `artifact_uploads` 中使用会报错。依赖和边仍需显式声明。
参见[静态 DAG 示例](../../examples/static_dag_images.py)。

## 配置限制与上下文预算

```python
runner = dagent.Runner(
    provider=provider,
    image_input_policy=dagent.ImageInputPolicy(
        max_image_bytes=20 * 1024 * 1024,
        max_image_pixels=40_000_000,
        max_images_per_request=16,
        max_total_image_bytes=100 * 1024 * 1024,
        estimated_tokens_per_image=8192,
    ),
)
```

以上为默认值。SDK 优先使用明确覆盖图片 token 的计数器；没有可信计数时，普通请求使用
可配置的每图估算，加上文字估算和安全余量，不把 Base64 长度作为文字 token 计算。
每图估算属于规划值，不代表实际消耗或可靠上界。`ContextUsage.image_count` 和
`estimated_image_tokens` 提供图片预算；无图时保留既有统计格式。

严格预算（包括 `chunked`、`cache_aware` 上下文策略）要求计数器同时声明 `exact=True`
和 `includes_image_tokens=True`。普通 tokenize/render 返回值如果未证明覆盖图片，不能
被视为精确视觉计数，这类请求在生成前失败。自定义计数器通过
`dagent.providers.model_io.ModelTokenCount` 声明覆盖范围；实际计费仍取自 Provider 的
`ModelTokenUsage`。

三种压缩策略均将被选中的图片内容发送给摘要模型。视觉摘要成功后保留快照引用；失败时
不会用仅含元数据的兜底摘要替换图片，包括已为 `chunked`、`cache_aware` 启用引用兜底的
情况。如果必须依赖这种兜底才能继续，准备上下文会明确报错并保留原始消息。
后续纯文本历史使用兜底时，先前已总结的图片仍可恢复。

`ContextSummary.image_manifest` 指向归档 `ImageReference` 记录的 JSONL 索引。
模型请求中仅保留一个长度受控的索引提示，不再逐张展开快照路径，避免恢复元数据随图片
历史无限增长。Agent 需要通过 `read_file` 读取索引，再用 `read_image` 查看选中的快照；
两种读取都必须符合文件边界。迁移会话时应同时保留索引资源和图片快照。此前没有索引的
视觉摘要会在准备上下文时自动生成索引。

配置结果验证后，验证器会通过 v5 会话接收动态 DAG 节点返回的图片。
证据快照缺失或损坏时会抛出 `ImageInputError`，不会跳过验证。
参见离线[图片验证示例](../../examples/image_validation.py)。

## 持久化与恢复模型实际看到的内容

上传到工作区的文件仍可修改。每次显式视觉输入保存独立的、按内容寻址的原始字节快照。
修改或删除原路径不会改变历史视觉输入；再次调用 `read_image` 才查看新版本。

`ImageReference` 包含带必需 SHA-256 的 `ContentReference`，以及媒体类型、字节数和图片
尺寸。Runner 记录的消息、归一化工具结果和 checkpoint 保存引用，不包含 Base64。将会话迁移到其他
工作区时，需要恢复快照资源。快照缺失或被修改会明确失败。无效视觉输入可捕获 `dagent.ImageInputError`，工具图片校验
失败则移除被拒绝的图片工件，并记录为工具失败结果。执行开始后的图片上下文错误
通过现有的 `result` 属性携带失败运行审计，恢复 DAG 审核时也会保留已完成的工作。

视觉会话使用 `ConversationState.schema_version=5`，纯文本会话仍为 v4。消费会话变更的
宿主重建状态时，需要使用 `batch.conversation_schema_version`。图片快照沿用现有资源
交付和 ACK 规则，长期存储及资源保留策略由宿主负责。

离线接线示例：[上传](../../examples/image_uploads.py)、
[读取工作区文件](../../examples/image_reading.py)、[MCP](../../examples/image_mcp.py)、
[静态 DAG](../../examples/static_dag_images.py)。示例使用确定性的 MockProvider 回复，
实际识图时替换为视觉模型。
