# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project Context

dagent is a Dynamic DAG Agent framework. It supports bounded tool-agent runs,
dynamic DAG planning, and user-defined static DAG execution. Public agent objects
are declarative configuration; `Runner` owns runtime state, provider wiring,
capability registration, review continuations, and execution dispatch.

This project has not been formally released yet. Prefer clean current design over
backward compatibility. Do not add compatibility shims, legacy aliases, hidden
conversion layers, or duplicate code paths just to preserve old behavior or old
tests. Update or delete obsolete tests when the intended behavior has changed.

## Core Engineering Rules

- Keep code and logic simple. Choose direct implementations over abstractions
  unless the abstraction removes real duplication or clarifies ownership.
- Follow existing module boundaries and local style before introducing new
  patterns.
- Do not preserve historical behavior unless the user explicitly asks for it.
- Do not add compatibility aliases such as old capability ids or renamed SDK
  entrypoints.
- Do not add transition layers that convert old request shapes into new ones.
- Prefer typed data contracts and Pydantic models over ad hoc dictionaries when
  the value crosses SDK, API, or runtime boundaries.
- Keep public SDK surfaces intentional. If something is internal, import it from
  its owning module rather than re-exporting it at the package root.
- When changing behavior, update docs and examples in the same change.

## Architecture Boundaries

- `dagent.runner.Runner` is the public SDK entrypoint for executing agents and
  DAGs. It owns capability registration, skill store access, MCP registration,
  runtime state, and review resume flow.
- `ToolAgent`, `DagAgent`, and `Dag` are declarative public objects. They should
  not own runtime session state.
- `harness_runtime` owns execution behavior: routing, tool loops, DAG loops,
  validation, review state, event adapters, and DAG execution.
- `schemas` owns shared data contracts such as DAG specs, capability contracts,
  run traces, reviews, and runtime responses.
- `capabilities` owns capability providers, tools, MCP adapters, skills, shell,
  file, memory, and boundary enforcement.
- The API layer should use the public SDK where practical. If the API must reach
  internal modules, treat that as a design smell and consider whether a public
  SDK method belongs on `Runner` or another intentional surface.

## Capabilities And Skills

- Python function tools use `tool.<name>` capability ids.
- MCP stdio tools use `mcp.<server>.<tool>` capability ids.
- Skills are discovered through skill roots and managed installs. Use the
  `SkillStore.install`, `SkillStore.view`, and `SkillStore.delete` style API;
  do not keep separate temporary/import-only skill paths.
- Skill linked files under `references/`, `templates/`, `assets/`, and
  `scripts/` must be readable through checked relative paths. Reject path
  traversal and dot-segment escapes.
- Shell command execution uses a real shell with blacklist-style boundary checks.
  Keep command blocking focused on dangerous operations and boundary violations,
  not broad unsupported shell syntax.

## DAG Dataflow

- Static DAG node arguments can use structured `$expr` value references for graph
  input, upstream node output, node content/status/steps, artifacts, and format
  strings.
- A node that reads another node's output must explicitly depend on that node via
  `dag.add_edge(...)`.
- Pydantic graph inputs and Pydantic tool return values are the preferred simple
  path for typed parameter passing.
- Do not infer DAG edges implicitly from value references. Validation should fail
  closed for non-upstream reads, unknown artifacts, malformed expressions, and
  unsafe artifact boundaries.

## Public SDK Documentation

- Keep README focused on project introduction, core ideas, quick start,
  architecture, project layout, and links.
- Keep detailed Python SDK usage in `docs/python-sdk.md`.
- Runnable example code belongs in `examples/`.
- Keep the local FastAPI/WebUI backend in top-level `api/`. Do not put it
  inside the installable `dagent` SDK package unless it becomes a deliberate
  public server package.
- Keep built-in agent profiles as one Markdown file per profile under
  `dagent/resources/profiles/<name>.md`. User profile directories must be passed
  explicitly through `profile_root`; do not reintroduce cwd `profiles/` fallback,
  YAML profile manifests, layered prompt files, or profile memory files.
- `Runner(...)` must use explicit SDK inputs. Do not make it read `config.yaml`
  implicitly; configuration-file loading belongs in `Runner.from_config(...)`.
- README quick start should show capability registration, ToolAgent, DagAgent,
  and static Dag usage. Do not put provider connectivity checks there.
- Keep docs synchronized with the actual public exports from `dagent/__init__.py`.

## Testing And Verification

Use the narrowest meaningful checks for the change, then broaden when behavior or
public contracts changed.

Common checks:

```bash
uv run --extra dev pytest
npm --prefix web test
npm --prefix web run build
git diff --check
```

For Python-only changes, prefer targeted pytest files first, then run the full
suite before committing when the change touches shared runtime behavior.

For frontend changes, run the relevant web tests and build. If a local web app is
changed, verify it in a browser when the UI behavior matters.

## Git Workflow

- Never revert user changes unless explicitly asked.
- Inspect `git status` before staging.
- Stage only files that belong to the requested change.
- Keep commits focused and messages terse.
- Push only after the requested checks pass or after clearly reporting why a
  check could not be run.
