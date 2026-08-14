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
    capabilities=["tool.search"],
    skills=["writing/terse"],
)
```

使用 `skills=None` 允许所有已配置 skills，`skills=[]` 隐藏 skill tools，
`skills=[...]` 只暴露指定 skills。

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
