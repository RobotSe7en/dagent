# Skills

Skills are readable instructional assets discovered through skill roots and
managed installs. They are not executable capabilities by themselves. Agents
expose readable skills through the built-in `skill.list` and `skill.view`
capabilities.

## Skill Roots

Add skill roots at runner construction or runtime:

```python
runner = dagent.Runner(
    provider=provider,
    workspace=".",
    skill_roots=["team-skills"],
)

runner.add_skill_root("more-skills")
```

Agents can restrict which concrete skills are visible:

```python
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["tool.search"],
    skills=["writing/terse"],
)
```

Use `skills=None` to allow all configured skills, `skills=[]` to hide skill
tools, and `skills=[...]` to expose only named skills.

## Managed Installs

`Runner` exposes the `SkillStore` powering skill discovery and installation:

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

You can also construct a store directly:

```python
store = dagent.SkillStore(
    roots=["team-skills"],
    managed_root=".dagent/skills",
)
```

## Skill Files

Skills can link files under checked relative paths such as:

- `references/`
- `templates/`
- `assets/`
- `scripts/`

Read linked files through `view(..., file_path=...)`:

```python
view = runner.skill_store.view(
    "writing/terse",
    file_path="scripts/example.py",
)
print(view.content)
```

Path traversal and dot-segment escapes are rejected.

## Example

Run the offline runtime registration and skills example:

```bash
uv run python -m examples.runtime_registration_and_skills
```

See [Capabilities](capabilities.md) for the built-in skill accessor capability
ids, and [Runner and Configuration](runner-and-configuration.md) for skill root
configuration.
