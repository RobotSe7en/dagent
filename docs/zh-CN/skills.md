# Skills

Skills 是通过 skill roots 和 managed installs 发现的可读 instruction assets。它们本身
不是可执行 capabilities。Agents 通过内置的 `skill.list` 和 `skill.view` capabilities
暴露可读 skills。

## Skill Roots

可以在 runner 构造时或运行时添加 skill roots：

```python
runner = dagent.Runner(
    provider=provider,
    workspace="agent-workspace",
    runtime_directory=".runtime",
    skill_roots=["team-skills"],
)

runner.add_skill_root("more-skills")
```

Agents 可以限制哪些具体 skills 可见：

```python
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["tool.search", "skill.list", "skill.view"],
    skills=["writing/terse"],
)
```

使用 `skills=None` 允许所有已配置技能，`skills=[]` 不允许访问任何技能，
`skills=[...]` 只暴露指定技能。这个字段只限制技能目录和可读取内容，不决定工具选择。

`skill.list` 和 `skill.view` 与其他内置工具一样，由 capability 选择和启用状态控制。
`capabilities=None` 包含全部已启用的内置工具，即使技能目录为空；显式的
`capabilities=[...]` 只包含列出的工具，需要技能访问工具时应逐个加入。
`capabilities=[]` 不暴露任何工具。在 `skills=[]` 时，已选择的 `skill.list`
返回空目录，`skill.view` 拒绝访问任何技能。

## Prompt 发现与加载

Tool-loop agent 会根据解析后的 skill scope，在 system prompt 中收到确定性的
`Available Skills` 索引。索引包含 qualified name，并在预算允许时包含完整 description，
但不会预加载 `SKILL.md` 正文。当用户显式指定某个技能，或任务与 description 明确匹配
时，agent 会被要求先调用 `skill.view`，再按照返回的指令执行。

每条索引使用紧凑 JSON array：完整条目为 `[qualified_name, description]`，单元素 array
表示仅名称回退。这样既保留 JSON 转义，也避免为每个技能重复写 object 字段名。

包含 description 的索引区有 8,000 字符预算。如果下一条完整 description 会越过边界，
该条及后续条目只写名称，并使用独立的 2,000 字符预算。Description 和名称都不会被部分
截断。两段预算都用尽时，prompt 会报告省略数量，agent 可以调用 `skill.list` 获取完整目录。

条目按 qualified name 排序，不包含时间戳，也不根据当前任务动态排序。因此同一会话中，
skill scope 和 skill metadata 不变时，system prompt 也保持不变，有利于 provider prompt
cache 命中。顶层 `ToolAgent`、AutoAgent 的 tool 分支以及注册或 inline tool 子 agent 会
收到该索引；dynamic DAG planner 不接收业务技能索引，绑定技能的子 agent 会在被调用时
收到自己的配置范围。

## Managed Installs

`Runner` 暴露用于 skill discovery 和 installation 的 `SkillStore`：

```python
installed = runner.skill_store.install(
    "Keep every answer to one compact sentence.",
    name="terse",
    description="Compact response style.",
    category="writing",
)

print(installed.skill.qualified_name)
print(runner.skill_store.view("writing/terse").content)
```

也可以直接构造 store：

```python
store = dagent.SkillStore(
    roots=["team-skills"],
    managed_root=".dagent/skills",
)
```

ZIP 技能包使用同一个 API 安装：

```python
from pathlib import Path

installed = store.install(Path("terse.zip").read_bytes(), filename="terse.zip")
```

`SKILL.md` 可以位于压缩包根目录，也可以被连续的外层文件夹包裹，例如
`export/terse/SKILL.md`。每层外包目录只能包含下一层文件夹；如果出现额外同级文件
或多个技能目录，会明确拒绝，不会静默丢弃文件或猜选技能。安装时移除外包目录，
保留相对于技能 `SKILL.md` 的全部文件。包内元数据来自 frontmatter；名称、描述和
分类覆盖仅用于 Markdown 安装。解压前仍会校验危险路径、符号链接、重复路径，
以及包大小和文件数限制。

ZIP 上传限制为 10 MiB、256 个文件，单文件最大 1 MiB。压缩包中的每条路径最多包含
10 层目录，外包目录和技能内部目录一起计数，文件名不计入目录层数；例如
`export/terse/scripts/run.py` 的深度为 3。显式目录条目同样遵守深度限制。
移除外包目录和解压文件前检查深度，超过限制抛出 `SkillStoreError`，本地 API 返回
HTTP 400。对于过深的压缩包，请减少外包目录或调整内部目录结构后重新打包。

## Skill Files

Skills 可以链接 checked relative paths 下的文件，例如：

- `references/`
- `templates/`
- `assets/`
- `scripts/`

通过 `view(..., file_path=...)` 读取关联文件：

```python
view = runner.skill_store.view(
    "writing/terse",
    file_path="scripts/example.py",
)
print(view.content)
```

Path traversal 和 dot-segment escapes 会被拒绝。

## 示例

运行离线 runtime registration and skills 示例：

```bash
uv run python -m examples.runtime_registration_and_skills
```

内置 skill accessor capability ids 见 [Capabilities](capabilities.md)，skill root 配置见
[Runner 和配置](runner-and-configuration.md)。
