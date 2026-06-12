# dagent Documentation

This directory contains the user-facing documentation for dagent. The root
[`README.md`](../README.md) introduces the project and keeps the architecture
overview close to the project identity. The pages here focus on installation,
SDK usage, feature guides, and released behavior.

## Start Here

- New to dagent: read [Quick Start](quick-start.md).
- Setting up an environment: read [Installation](installation.md).
- Trying to understand the model first: read [Core Concepts](concepts.md).
- Looking up public SDK names: read [Python SDK Reference Map](python-sdk.md).

## Feature Guides

- [Runner and Configuration](runner-and-configuration.md): providers,
  `Runner(...)`, `Runner.from_config(...)`, validation, MCP registration, and
  runtime capability management.
- [Capabilities](capabilities.md): Python tools, MCP capability ids, structured
  results, policies, and boundaries.
- [Agents](agents.md): when to use `ToolAgent`, `AutoAgent`, or `DagAgent`.
- [Static DAGs](static-dag.md): typed graph input, node output references,
  artifacts, explicit edges, control flow, subgraphs, and loops.
- [Skills](skills.md): skill roots, managed installs, `SkillStore`, linked
  files, and agent-level skill visibility.
- [Results, Streaming, and Review](results-streaming-review.md): `RunResult`,
  `RunState`, streaming events, review checkpoints, resume, and persistence.

## Operations

- [Troubleshooting](troubleshooting.md): common setup, provider, MCP,
  capability, DAG validation, and review issues.
- [Migration Notes](migration.md): released-surface changes and upgrade notes.
- [Examples](../examples/README.md): runnable scripts mapped to the docs page
  they demonstrate.

## Documentation Principles

- Public SDK behavior described here is treated as user-facing contract.
- Examples should run from the repository root unless a page says otherwise.
- Feature pages should link back to the SDK map instead of duplicating long
  reference tables.
- When public behavior changes, update the relevant docs and examples in the
  same change.
