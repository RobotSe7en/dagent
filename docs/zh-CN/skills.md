# Skills

Skills 是通过 skill roots 和 managed installs 发现的可读 instruction assets。它们本身
不是可执行 capabilities。Agents 通过内置的 `skill.list` 和 `skill.view` capabilities
暴露可读 skills。

## Skill Roots

可以在 runner 构造时或运行时添加 skill roots：

```python
runner = dagent.Runner(
    provider=provider,
    workspace=".dagent",
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
