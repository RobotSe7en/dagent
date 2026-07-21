# Dynamic DAG Generation Plan

This page records the staged technical direction for dynamic DAG generation.
Both phases are available in 0.7.3. Released-version behavior is documented in
the task guides. Public
APIs, capability ids, configuration semantics, and review contracts remain
intentional contracts.

## Status

- Phase one: implemented with an internal strict planner schema, full-plan
  initial generation, and full-plan replanning.
- Phase two: implemented as the optional `sdk_builder` planner frontend; the
  default remains `typed_spec`.
- Canonical representation: `DAGSpec`, stored in dynamic `RunState` and pending
  DAG reviews alongside the executable DAG projection.
- Legacy free-form PlanSpec DSL: removed without a fallback parser.

## Target Architecture

Static and dynamic DAGs should share one normalized execution representation:
`DAGSpec`. Their difference is the source of the plan, not its validation,
review, or execution semantics:

```text
Python Dag builder ──────────────────┐
                                     ├─ canonical DAGSpec
Typed dynamic planner output ────────┤        │
                                     │        ├─ validation
Restricted SDK builder (phase two) ──┘        ├─ review
                                              └─ execution
```

Core principles:

- `DAGSpec` is the only canonical IR used for persistence, fingerprints,
  review, and execution.
- The dynamic planner is no longer limited by the capability-only PlanSpec.
- The planner declares execution intent. `Runner` and the host continue to own
  providers, handlers, risk, boundaries, workspaces, runtime status, and
  invocation identity.
- Every planner frontend enters the same normalization, validation, review,
  and execution path. There must not be duplicate execution implementations.
- Conditions, maps, and loops use structured expressions and explicit bounds.
  Arbitrary executable condition code and unbounded control flow are rejected.

## Phase One: Typed Planner Spec to DAGSpec

Phase one establishes a typed, declarative dynamic planning protocol. Prefer
provider structured output instead of extending the current free-form PlanSpec
DSL.

### Planner Output Protocol

- Use an explicit discriminator for these responses instead of guessing from a
  parse attempt:
  - `propose_plan`
  - `no_change`
  - `final_answer`
- Keep the planner-facing spec as close to `DAGSpec` as practical while
  excluding host-owned fields.
- Cover these node types first:
  - capability or agent invocation
  - map fan-out
  - embedded subgraph
  - bounded loop
- Support structured `when` conditions on edges.
- Support graph-input, node-output/content/status/steps, item, artifact, and
  format value expressions.
- Support explicit DAG output and artifact producer/consumer declarations.

### Capability Context

The planner receives the real scope-filtered capability catalog, including:

- stable capability id, planner-visible name, kind, and description;
- complete input schemas, including types, required fields, enums, defaults,
  and nested properties;
- complete output schemas so the planner can select structured output paths
  and construct conditions;
- registered-agent argument contracts and local execution bounds.

The model cannot register capabilities, providers, MCP servers, or agents. It
also cannot choose risk, widen boundaries, or override host policy.

### Normalization and Execution Path

Process planner output in this order:

1. Parse and validate the planner-facing Pydantic contract.
2. Resolve stable planner capability ids against the scoped catalog.
3. Fill kind, risk, boundaries, default arguments, and other host-owned
   metadata from the catalog.
4. Produce canonical `DAGSpec`.
5. Call `validate_dag_spec(...)` to recursively check dependencies, value
   expressions, artifacts, subgraphs, and bounded control flow.
6. Review, fingerprint, and execute the normalized `DAGSpec`.

When validation fails, return the structured field path and concrete error to
the planner for repair. Unknown fields and unsupported control flow must fail
explicitly. They must not be silently discarded or mistaken for a final
answer.

### Initial Planning and Replanning

Stabilize complete initial-plan generation before expanding dynamic replanning:

1. Generate, validate, review, and execute a complete typed spec.
2. Replan with another complete typed spec while preserving unchanged
   invocation identity and requiring explicit reruns for changed completed
   nodes.
3. In a later milestone, introduce typed patches with `base_version` that atomically add,
   replace, or remove nodes, edges, and arguments.
4. Reject unintended changes to completed nodes by default. A required rerun
   must be explicit.
5. Invalidate results according to the actual changes and their downstream
   impact rather than invalidating every node after any edge change.

Until the replanning protocol is stable, layer-by-layer `dynamic_adjust` may be
disabled or restricted so initial planning, execution feedback, and graph
repair can be evaluated independently.

### Phase-One Acceptance Criteria

- Structured output reliably distinguishes plans, no-change responses, and
  final answers.
- The dynamic planner generates and executes representative conditional,
  parallel, map, loop, and subgraph cases.
- Capability references, output paths, and artifact dependencies all receive
  fail-closed validation.
- The planner cannot declare or widen host-owned risk, boundaries, or runtime
  configuration.
- The parser no longer silently ignores unknown planner lines or fields.
- The current PlanSpec DSL receives an explicit preservation, deprecation, or
  migration policy. Do not add a hidden compatibility path.

## Phase Two: Restricted SDK Builder to DAGSpec

Phase two adds the public Python DAG builder as an optional model-facing
authoring frontend, taking advantage of stronger code-generation behavior. It
does not change the canonical IR or introduce a second validator or executor.

Select it globally with `Runner(..., planner_frontend="sdk_builder")` or the
top-level `planner_frontend: sdk_builder` YAML setting. There is deliberately no
per-request API or WebUI selector. Initial plans and replans both return a full
Builder program. Validation failures consume the existing planner cycle budget;
there is no separate repair loop.

### DAG Generation Skill

Provide a dedicated DAG-generation skill containing:

- a compact, version-specific public SDK reference;
- capability and registered-agent usage rules;
- examples for conditional edges, structured output references, maps, loops,
  subgraphs, and artifacts;
- bounded-control-flow, boundary, and review rules;
- common validation failures and repair examples;
- instructions for the generation and validation entrypoint.

The planner must load the skill content explicitly. Installing the skill in a
`SkillStore` or including it in an agent scope does not by itself prove that the
planner read or followed it.

### Restricted Builder Contract

The model generates pure graph-construction code that creates `Dag`, `Node`,
`MapNode`, and `LoopNode` objects, adds nodes and edges, and declares output.
This code is never executed as unrestricted Python.

Allowed behavior includes at least:

- variable assignments;
- literals, lists, and dictionaries;
- approved DAG builder constructors and methods;
- graph-input, node-output, item, artifact, and comparison references;
- controlled subgraph construction.

Reject:

- imports and file, network, subprocess, or environment access;
- `eval`, `exec`, dunder access, and arbitrary function calls;
- executing real capabilities during graph construction;
- arbitrary side effects;
- unbounded loops and graph-construction logic that cannot be statically
  audited.

The implementation uses a restricted AST interpreter that converts approved
builder statements into real public Builder values without `exec` or `eval`.
Source is limited to 64 KiB, 10,000 AST nodes, and expression depth 64. It
accepts straight-line assignments, JSON-like values, approved constructors and
`Dag` methods, references, and comparisons. Imports, definitions, control flow,
comprehensions, arbitrary calls, dunder access, and host-owned arguments fail
closed before a `DAGSpec` is produced.

### Capability and Agent References

Generated code only refers to stable ids that already exist in the catalog:

```python
target="tool.search"
target="mcp.browser.open"
target="agent.analyst"
```

The model cannot define handlers, create providers, register MCP servers, or
construct agents that own runtime state. After the builder produces a `Dag`,
call `to_dag_spec()` immediately. Normalization, validation, review,
persistence, and execution use only canonical `DAGSpec` from that point on.

### Phase-Two Acceptance Criteria

- Reject every non-construction Python form before execution.
- Normalize identical builder input into stable, repeatable `DAGSpec` output.
- The SDK and typed-spec frontends share exactly the same capability
  resolution, validator, review, and executor.
- If the typed-spec frontend already meets the quality target, phase two stays
  optional rather than replacing phase one by assumption.

## Explicit Non-Goals

- Do not execute unrestricted model-generated Python.
- Do not make a skill responsible for runtime state, providers, capability
  registration, or security boundaries.
- Do not treat model-generated source code as the authoritative review or
  persistence object.
- Do not maintain separate DAG semantics, validation, or execution paths for
  the two frontends.
- Do not preserve the current DSL through silent conversion, legacy capability
  aliases, or an ambiguous fallback parser.

## Recorded Decisions and Remaining Work

- The planner-facing typed contract is internal. `DAGSpec` remains the shared
  public/canonical contract.
- Phase one uses full-spec replanning. Typed patch operations are deferred.
- Planner capability references use stable capability ids.
- Planner values use a recursive typed AST; the host converts it to native
  literals and `$expr` bindings.
- Existing runtime execution and cycle bounds remain authoritative; phase one
  adds no separate graph-size policy.
- The free-form PlanSpec DSL was removed directly. Persisted deterministic
  planner fixtures and custom providers must migrate to structured output.
- The packaged `generate-dag` skill is mandatory and version-locked. Its exact
  content and digest are frozen into V2 checkpoints and restored on resume.
- Builder source remains planner transcript data. Canonical `DAGSpec` is the
  authoritative review, persistence, fingerprint, and execution object.
- Checkpoint V1 remains readable and means `typed_spec`; newly produced
  checkpoints use V2 and record the selected frontend.
- No evaluation baseline or A/B gate is part of this implementation phase.
