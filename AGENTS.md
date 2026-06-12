# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project Context

dagent is a Dynamic DAG Agent framework. It supports bounded tool-agent runs,
dynamic DAG planning, and user-defined static DAG execution. Public agent objects
are declarative configuration; `Runner` owns runtime state, provider wiring,
capability registration, review continuations, and execution dispatch.

This project has been released. Treat the public SDK and documented behavior as
intentional user-facing contracts. Prefer clean current design, but do not break
public APIs, capability ids, documented request shapes, config semantics, or
example workflows casually. Public breaking changes must be deliberate,
documented, covered by tests, and accompanied by migration guidance.

Do not add hidden compatibility shims, legacy aliases, conversion layers, or
duplicate code paths as a reflex. If compatibility is necessary for a released
surface, make the compatibility policy explicit in the design, tests, and docs.
Update or delete obsolete tests only when the intended released behavior has
changed deliberately.

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

Write documentation like a mature open-source project: task-oriented, accurate,
version-aware, easy to navigate, and synchronized with runnable examples. Prefer
small focused docs over a single sprawling guide, and keep every page clear about
the public surface it teaches.

- Keep the root README focused on project introduction, core ideas, a short
  quick start, architecture, project layout, and documentation links. The
  architecture section belongs in the root README because it is central to the
  project identity.
- Use `docs/README.md` as the documentation landing page and reading map.
- Use `docs/quick-start.md` for the first complete user path from install to a
  working run.
- Use `docs/python-sdk.md` as the public SDK overview and export/reference map,
  synchronized with `dagent/__init__.py`.
- Split feature documentation by user task and runtime boundary, for example:
  `docs/runner-and-configuration.md`, `docs/capabilities.md`,
  `docs/agents.md`, `docs/static-dag.md`, `docs/skills.md`, and
  `docs/results-streaming-review.md`.
- Runnable example code belongs in `examples/`; `examples/README.md` should map
  each example to the docs page and feature it demonstrates.
- Keep the local FastAPI/WebUI backend in top-level `api/`. Do not put it
  inside the installable `dagent` SDK package unless it becomes a deliberate
  public server package.
- Keep built-in agent profiles as one Markdown file per profile under
  `dagent/resources/profiles/<name>.md`. User profile directories must be passed
  explicitly through `profile_root`; do not reintroduce cwd `profiles/` fallback,
  YAML profile manifests, layered prompt files, or profile memory files.
- `Runner(...)` must use explicit SDK inputs. Do not make it read `config.yaml`
  implicitly; configuration-file loading belongs in `Runner.from_config(...)`.
- Root README quick start should stay short. Detailed setup, provider options,
  agent choices, static DAGs, streaming, review, and skills belong in the
  focused docs pages.
- Do not put provider connectivity checks in the README quick start.
- When changing public behavior, update the relevant docs, examples, and
  migration notes in the same change.
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
