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
    workspace="agent-workspace",
    runtime_directory=".runtime",
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

## Prompt Discovery And Loading

Tool-loop agents receive a deterministic `Available Skills` system-prompt
index for their resolved skill scope. The index contains qualified names and,
within its prompt budget, complete descriptions. It does not preload
`SKILL.md` bodies. When the user explicitly requests a listed skill or the task
clearly matches its description, the agent is instructed to call `skill.view`
before acting and follow the returned instructions.

Each index line is a compact JSON array. A complete entry is
`[qualified_name, description]`; a one-item array is a name-only fallback.
This keeps JSON escaping while avoiding repeated object field names.

The description-rich portion of the index has an 8,000-character budget. Once
the next complete description would cross that boundary, subsequent entries
use names only, with a separate 2,000-character budget. Descriptions and names
are never partially truncated. If both budgets are exhausted, the prompt
reports the omitted count and the agent can call `skill.list` for the complete
catalog.

Entries are sorted by qualified name and contain no timestamps or task-based
ranking. Within one conversation, an unchanged skill scope and unchanged skill
metadata therefore produce the same system prompt, preserving provider prompt
cache eligibility. Top-level `ToolAgent` runs, AutoAgent's tool branch, and
registered or inline tool subagents receive the index. The dynamic DAG planner
does not receive this business-skill index; skill-bound subagents receive their
own configured index when invoked.

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
