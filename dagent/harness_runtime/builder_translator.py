"""Translate a constrained Python DAG Builder program without executing Python."""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from typing import Any, Iterable

from dagent.dag_builder import (
    ArtifactRef,
    ArtifactValueRef,
    Case,
    CompareRef,
    ConditionNode,
    Dag,
    FormatRef,
    InputRef,
    ItemRef,
    LogicalRef,
    LoopNode,
    MapNode,
    Node,
    NodeOutputRef,
    all_of,
    any_of,
    item,
    not_,
)
from dagent.harness_runtime.dag_builder import DAGCreationError


MAX_BUILDER_SOURCE_BYTES = 64 * 1024
MAX_BUILDER_AST_NODES = 10_000
MAX_BUILDER_EXPRESSION_DEPTH = 64


class BuilderTranslationError(DAGCreationError):
    """Raised when model-authored Builder source is outside the safe subset."""


@dataclass(frozen=True)
class _DagentNamespace:
    """Marker used to expose only approved public Builder symbols."""


_DAGENT = _DagentNamespace()
_CONSTRUCTORS = {
    "Dag": Dag,
    "Node": Node,
    "MapNode": MapNode,
    "LoopNode": LoopNode,
    "ConditionNode": ConditionNode,
    "Case": Case,
}
_FUNCTIONS = {
    "all_of": all_of,
    "any_of": any_of,
    "not_": not_,
}
_DAG_METHODS = {"artifact", "add_node", "add_edge", "format"}
_NODE_ATTRIBUTES = {"output", "content", "status", "steps"}
_ARTIFACT_ATTRIBUTES = {"path", "paths", "absolute_path", "absolute_paths"}
_REF_TYPES = (InputRef, NodeOutputRef, ItemRef)
_VALUE_TYPES = (
    str,
    int,
    float,
    bool,
    type(None),
    list,
    tuple,
    dict,
    Dag,
    Node,
    ArtifactRef,
    ArtifactValueRef,
    InputRef,
    NodeOutputRef,
    ItemRef,
    FormatRef,
    CompareRef,
    LogicalRef,
    Case,
    ConditionNode,
)


def translate_builder_source(
    source: str,
    *,
    capability_ids: Iterable[str],
) -> Dag:
    """Parse canonical Builder source and return its root ``Dag`` value.

    The source is interpreted node by node against an explicit allowlist. It is
    never passed to ``exec``, ``eval``, or the Python import system.
    """
    if not isinstance(source, str) or not source.strip():
        raise BuilderTranslationError("Builder source must be a non-empty string.")
    if len(source.encode("utf-8")) > MAX_BUILDER_SOURCE_BYTES:
        raise BuilderTranslationError(
            f"Builder source exceeds the {MAX_BUILDER_SOURCE_BYTES}-byte limit."
        )
    try:
        module = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        location = _location(exc.lineno, exc.offset)
        raise BuilderTranslationError(
            f"Builder source has invalid Python syntax{location}: {exc.msg}."
        ) from exc
    node_count = sum(1 for _ in ast.walk(module))
    if node_count > MAX_BUILDER_AST_NODES:
        raise BuilderTranslationError(
            f"Builder source exceeds the {MAX_BUILDER_AST_NODES}-node AST limit."
        )

    translator = _BuilderTranslator(set(capability_ids))
    return translator.translate(module)


class _BuilderTranslator:
    def __init__(self, capability_ids: set[str]) -> None:
        self._capability_ids = capability_ids
        self._values: dict[str, Any] = {"dagent": _DAGENT}

    def translate(self, module: ast.Module) -> Dag:
        for statement in module.body:
            self._statement(statement)
        root = self._values.get("dag")
        if type(root) is not Dag:
            raise BuilderTranslationError(
                "Builder source must assign the root graph to variable 'dag'."
            )
        return root

    def _statement(self, statement: ast.stmt) -> None:
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1:
                self._fail(statement, "Chained assignments are not supported.")
            value = self._expression(statement.value, depth=1)
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                self._assign_name(target, value)
                return
            if isinstance(target, ast.Attribute):
                owner = self._expression(target.value, depth=1)
                if type(owner) is Dag and target.attr == "output":
                    owner.output = value
                    return
                self._fail(target, "Only a Dag.output attribute may be assigned.")
            self._fail(target, "Only simple variables and Dag.output may be assigned.")
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            if not self._is_dag_mutation(statement.value.func):
                self._fail(
                    statement,
                    "Expression statements must call Dag.add_node or Dag.add_edge.",
                )
            self._expression(statement.value, depth=1)
            return
        self._fail(
            statement,
            f"Statement '{type(statement).__name__}' is not supported by the Builder subset.",
        )

    def _assign_name(self, target: ast.Name, value: Any) -> None:
        if not target.id.isidentifier() or target.id.startswith("_"):
            self._fail(target, "Builder variable names must be public Python identifiers.")
        if target.id == "dagent" or target.id in self._values:
            self._fail(target, f"Builder variable '{target.id}' may only be assigned once.")
        if not isinstance(value, _VALUE_TYPES):
            self._fail(target, f"Unsupported Builder value '{type(value).__name__}'.")
        self._values[target.id] = value

    def _expression(self, expression: ast.expr, *, depth: int) -> Any:
        if depth > MAX_BUILDER_EXPRESSION_DEPTH:
            self._fail(
                expression,
                f"Builder expression exceeds the depth limit of {MAX_BUILDER_EXPRESSION_DEPTH}.",
            )
        next_depth = depth + 1
        if isinstance(expression, ast.Constant):
            if isinstance(expression.value, (str, int, float, bool, type(None))):
                if isinstance(expression.value, float) and not math.isfinite(expression.value):
                    self._fail(expression, "Non-finite numeric literals are not supported.")
                return expression.value
            self._fail(expression, "Only JSON-compatible literals are supported.")
        if isinstance(expression, ast.List):
            return [self._expression(item, depth=next_depth) for item in expression.elts]
        if isinstance(expression, ast.Tuple):
            return tuple(self._expression(item, depth=next_depth) for item in expression.elts)
        if isinstance(expression, ast.Dict):
            result: dict[str, Any] = {}
            for key_node, value_node in zip(expression.keys, expression.values, strict=True):
                if key_node is None:
                    self._fail(expression, "Dictionary unpacking is not supported.")
                key = self._expression(key_node, depth=next_depth)
                if not isinstance(key, str):
                    self._fail(key_node, "Builder dictionary keys must be strings.")
                if key in result:
                    self._fail(key_node, f"Duplicate dictionary key '{key}'.")
                result[key] = self._expression(value_node, depth=next_depth)
            return result
        if isinstance(expression, ast.Name):
            if expression.id not in self._values:
                self._fail(expression, f"Unknown Builder variable '{expression.id}'.")
            return self._values[expression.id]
        if isinstance(expression, ast.Attribute):
            return self._attribute(expression, depth=next_depth)
        if isinstance(expression, ast.Subscript):
            owner = self._expression(expression.value, depth=next_depth)
            index = self._subscript_index(expression.slice, depth=next_depth)
            if isinstance(owner, _REF_TYPES):
                return owner[index]
            if isinstance(owner, (list, tuple)) and isinstance(index, int):
                try:
                    return owner[index]
                except IndexError as exc:
                    self._fail(expression, "Literal sequence index is out of range.", cause=exc)
            if isinstance(owner, dict) and isinstance(index, str):
                try:
                    return owner[index]
                except KeyError as exc:
                    self._fail(expression, f"Unknown literal dictionary key '{index}'.", cause=exc)
            self._fail(expression, "Subscripts are only supported on references and literals.")
        if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, (ast.USub, ast.UAdd)):
            operand = self._expression(expression.operand, depth=next_depth)
            if isinstance(operand, bool) or not isinstance(operand, (int, float)):
                self._fail(expression, "Unary signs are only supported on numeric literals.")
            return -operand if isinstance(expression.op, ast.USub) else operand
        if isinstance(expression, ast.Compare):
            return self._comparison(expression, depth=next_depth)
        if isinstance(expression, ast.Call):
            return self._call(expression, depth=next_depth)
        self._fail(
            expression,
            f"Expression '{type(expression).__name__}' is not supported by the Builder subset.",
        )

    def _attribute(self, expression: ast.Attribute, *, depth: int) -> Any:
        if expression.attr.startswith("_"):
            self._fail(expression, "Private and dunder attributes are not supported.")
        owner = self._expression(expression.value, depth=depth)
        if owner is _DAGENT:
            if expression.attr in _CONSTRUCTORS:
                return _CONSTRUCTORS[expression.attr]
            if expression.attr in _FUNCTIONS:
                return _FUNCTIONS[expression.attr]
            if expression.attr == "item":
                return item
            self._fail(expression, f"dagent.{expression.attr} is not available to Builder source.")
        if type(owner) is Dag and expression.attr == "input":
            return owner.input
        if type(owner) in {Node, MapNode, LoopNode, ConditionNode} and expression.attr in _NODE_ATTRIBUTES:
            return getattr(owner, expression.attr)
        if isinstance(owner, _REF_TYPES):
            return getattr(owner, expression.attr)
        if type(owner) is ArtifactRef and expression.attr in _ARTIFACT_ATTRIBUTES:
            return getattr(owner, expression.attr)
        self._fail(
            expression,
            f"Attribute '{expression.attr}' is not supported on '{type(owner).__name__}'.",
        )

    def _comparison(self, expression: ast.Compare, *, depth: int) -> CompareRef:
        if len(expression.ops) != 1 or len(expression.comparators) != 1:
            self._fail(expression, "Chained comparisons are not supported.")
        operator = expression.ops[0]
        method_name = {
            ast.Eq: "__eq__",
            ast.NotEq: "__ne__",
            ast.Lt: "__lt__",
            ast.LtE: "__le__",
            ast.Gt: "__gt__",
            ast.GtE: "__ge__",
        }.get(type(operator))
        if method_name is None:
            self._fail(expression, "Only ==, !=, <, <=, >, and >= comparisons are supported.")
        left = self._expression(expression.left, depth=depth)
        right = self._expression(expression.comparators[0], depth=depth)
        if not isinstance(left, _REF_TYPES):
            self._fail(expression.left, "The left side of a condition must be a runtime reference.")
        result = getattr(left, method_name)(right)
        if not isinstance(result, CompareRef):
            self._fail(expression, "Condition did not produce a Builder comparison.")
        return result

    def _call(self, expression: ast.Call, *, depth: int) -> Any:
        if any(keyword.arg is None for keyword in expression.keywords):
            self._fail(expression, "Keyword unpacking is not supported.")
        positional = [self._expression(arg, depth=depth) for arg in expression.args]
        keywords = {
            str(keyword.arg): self._expression(keyword.value, depth=depth)
            for keyword in expression.keywords
        }
        if len(keywords) != len(expression.keywords):
            self._fail(expression, "Duplicate keyword arguments are not supported.")

        if isinstance(expression.func, ast.Attribute):
            owner = self._expression(expression.func.value, depth=depth)
            name = expression.func.attr
            if owner is _DAGENT and name in _CONSTRUCTORS:
                return self._construct(expression, name, positional, keywords)
            if owner is _DAGENT and name in _FUNCTIONS:
                return self._function_call(expression, name, positional, keywords)
            if type(owner) is Dag and name in _DAG_METHODS:
                return self._dag_call(expression, owner, name, positional, keywords)
        self._fail(expression, "Only approved dagent constructors and Dag methods may be called.")

    def _construct(
        self,
        expression: ast.Call,
        name: str,
        positional: list[Any],
        keywords: dict[str, Any],
    ) -> Any:
        allowed = {
            "Dag": {"name", "description"},
            "Node": {"target", "inputs", "artifact_inputs", "artifact_outputs", "title"},
            "MapNode": {
                "target",
                "over",
                "inputs",
                "max_items",
                "max_concurrency",
                "artifact_inputs",
                "artifact_outputs",
                "title",
            },
            "LoopNode": {"body", "until", "max_iterations", "input", "title"},
            "ConditionNode": {"cases", "default_branch", "title"},
            "Case": {"branch", "when"},
        }[name]
        self._check_call_shape(
            expression,
            positional,
            keywords,
            max_positional=2 if name == "Case" else 1,
            allowed=allowed,
        )
        if name in {"Node", "MapNode"}:
            target = keywords.get("target")
            if isinstance(target, str):
                if target.startswith("skill."):
                    self._fail(
                        expression,
                        "Skills cannot be DAG node targets; attach them to registered agents.",
                    )
                if target not in self._capability_ids:
                    available = ", ".join(sorted(self._capability_ids)) or "(none)"
                    self._fail(
                        expression,
                        f"Unknown capability '{target}'. Available capabilities: {available}.",
                    )
            elif name == "MapNode" or not isinstance(target, Dag):
                self._fail(expression, f"{name} target must be a catalog capability id string.")
        constructor = _CONSTRUCTORS[name]
        return self._invoke(expression, constructor, positional, keywords)

    def _function_call(
        self,
        expression: ast.Call,
        name: str,
        positional: list[Any],
        keywords: dict[str, Any],
    ) -> Any:
        if keywords:
            self._fail(expression, f"dagent.{name} accepts positional values only.")
        if name == "not_" and len(positional) != 1:
            self._fail(expression, "dagent.not_ requires exactly one value.")
        if name in {"all_of", "any_of"} and not positional:
            self._fail(expression, f"dagent.{name} requires at least one value.")
        return self._invoke(expression, _FUNCTIONS[name], positional, keywords)

    def _dag_call(
        self,
        expression: ast.Call,
        owner: Dag,
        name: str,
        positional: list[Any],
        keywords: dict[str, Any],
    ) -> Any:
        if name == "artifact":
            self._check_call_shape(
                expression,
                positional,
                keywords,
                max_positional=2,
                allowed={"description", "required"},
            )
        elif name == "add_node":
            self._check_call_shape(expression, positional, keywords, max_positional=1, allowed=set())
        elif name == "add_edge":
            self._check_call_shape(
                expression,
                positional,
                keywords,
                max_positional=2,
                allowed={"when", "branch", "reason"},
            )
        else:
            self._check_call_shape(
                expression,
                positional,
                keywords,
                max_positional=1,
                allowed=None,
            )
            if not positional or not isinstance(positional[0], str):
                self._fail(expression, "Dag.format requires a literal template string.")
        return self._invoke(expression, getattr(owner, name), positional, keywords)

    def _check_call_shape(
        self,
        expression: ast.Call,
        positional: list[Any],
        keywords: dict[str, Any],
        *,
        max_positional: int,
        allowed: set[str] | None,
    ) -> None:
        if len(positional) > max_positional:
            self._fail(expression, f"Call accepts at most {max_positional} positional argument(s).")
        if allowed is not None:
            unknown = sorted(set(keywords) - allowed)
            if unknown:
                self._fail(
                    expression,
                    f"Unsupported or host-owned keyword(s): {', '.join(unknown)}.",
                )
        if any(name.startswith("_") for name in keywords):
            self._fail(expression, "Private keyword arguments are not supported.")

    def _invoke(
        self,
        expression: ast.Call,
        callable_value: Any,
        positional: list[Any],
        keywords: dict[str, Any],
    ) -> Any:
        try:
            return callable_value(*positional, **keywords)
        except (TypeError, ValueError) as exc:
            self._fail(expression, f"Invalid Builder call: {exc}", cause=exc)

    def _subscript_index(self, expression: ast.expr, *, depth: int) -> str | int:
        value = self._expression(expression, depth=depth)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            self._fail(expression, "Reference paths require a string or integer literal.")
        return value

    def _is_dag_mutation(self, expression: ast.expr) -> bool:
        if not isinstance(expression, ast.Attribute) or expression.attr not in {"add_node", "add_edge"}:
            return False
        try:
            owner = self._expression(expression.value, depth=1)
        except BuilderTranslationError:
            return False
        return type(owner) is Dag

    def _fail(
        self,
        node: ast.AST,
        message: str,
        *,
        cause: Exception | None = None,
    ) -> Any:
        error = BuilderTranslationError(
            f"{message}{_location(getattr(node, 'lineno', None), getattr(node, 'col_offset', None))}"
        )
        if cause is not None:
            raise error from cause
        raise error


def _location(line: int | None, column: int | None) -> str:
    if line is None:
        return ""
    if column is None:
        return f" at line {line}"
    return f" at line {line}, column {column + 1}"
