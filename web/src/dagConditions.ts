import type {
  Artifact,
  CapabilityDefinition,
  CompareOperator,
  Dag,
  DagEdge,
  ValueBinding,
} from './types';
import {
  bindingLabel,
  buildVariableCatalog,
  collectNodeOutputRefs,
  isValueBinding,
  rewriteNodeOutputRefs,
  type VariableCatalog,
  upstreamNodeIds,
} from './valueBindings';

export type CompareBinding = ValueBinding & {
  $expr: {
    type: 'compare';
    op: CompareOperator;
    left: unknown;
    right: unknown;
  };
};

export interface FlowEdgeLike {
  source: string;
  target: string;
  sourceHandle?: string | null;
  data?: unknown;
}

export type ConditionLiteralType = 'string' | 'number' | 'json';

export type ConditionLiteralParseResult =
  | { valid: true; value: unknown }
  | { valid: false };

export const compareOperatorOptions: Array<{ value: CompareOperator; label: string }> = [
  { value: 'eq', label: '==' },
  { value: 'ne', label: '!=' },
  { value: 'gt', label: '>' },
  { value: 'ge', label: '>=' },
  { value: 'lt', label: '<' },
  { value: 'le', label: '<=' },
];

export function isCompareBinding(value: unknown): value is CompareBinding {
  return isValueBinding(value) && value.$expr.type === 'compare';
}

export function makeCompareBinding(
  left: unknown,
  op: CompareOperator = 'eq',
  right: unknown = true,
): CompareBinding {
  return {
    $expr: {
      type: 'compare',
      op,
      left,
      right,
    },
  };
}

export function conditionLabel(when: DagEdge['when']): string {
  if (!when) return '';
  if (!isCompareBinding(when)) {
    return `IF ${conditionOperandLabel(when)}`;
  }
  const operator = compareOperatorOptions.find((item) => item.value === when.$expr.op)?.label ?? when.$expr.op;
  return `IF ${conditionOperandLabel(when.$expr.left)} ${operator} ${conditionOperandLabel(when.$expr.right)}`;
}

export function conditionOperandLabel(value: unknown): string {
  if (isValueBinding(value)) return bindingLabel(value);
  if (typeof value === 'string') {
    const encoded = JSON.stringify(value);
    return encoded.length > 48 ? `${encoded.slice(0, 45)}…` : encoded;
  }
  if (value === null) return 'null';
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value === undefined) return 'undefined';
  const encoded = JSON.stringify(value);
  if (!encoded) return String(value);
  return encoded.length > 48 ? `${encoded.slice(0, 45)}…` : encoded;
}

export function isConditionVariableBinding(value: unknown): value is ValueBinding {
  if (!isValueBinding(value)) return false;
  return ['graph_input', 'node_output', 'artifact'].includes(value.$expr.type);
}

export function conditionVariableKey(binding: ValueBinding): string {
  const expr = binding.$expr;
  if (expr.type === 'graph_input') {
    return JSON.stringify({ type: expr.type, path: expr.path ?? [] });
  }
  if (expr.type === 'node_output') {
    return JSON.stringify({
      type: expr.type,
      node_id: expr.node_id,
      field: expr.field ?? 'value',
      path: expr.path ?? [],
    });
  }
  if (expr.type === 'artifact') {
    return JSON.stringify({
      type: expr.type,
      artifact_id: expr.artifact_id,
      field: expr.field ?? 'path',
    });
  }
  return JSON.stringify(binding);
}

export function parseConditionLiteralDraft(
  rawValue: string,
  type: ConditionLiteralType,
): ConditionLiteralParseResult {
  if (type === 'string') return { valid: true, value: rawValue };
  if (type === 'number') {
    if (!rawValue.trim()) return { valid: false };
    const parsed = Number(rawValue);
    return Number.isFinite(parsed)
      ? { valid: true, value: parsed }
      : { valid: false };
  }
  try {
    return { valid: true, value: JSON.parse(rawValue) };
  } catch {
    return { valid: false };
  }
}

export function hasUnconditionalSiblingEdge(edges: DagEdge[], edgeIndex: number): boolean {
  const selected = edges[edgeIndex];
  if (!selected?.when) return false;
  return edges.some((candidate, candidateIndex) => (
    candidateIndex !== edgeIndex
    && candidate.target === selected.target
    && !candidate.when
    && !candidate.branch
  ));
}

export function dagEdgesFromFlowEdges(
  flowEdges: FlowEdgeLike[],
  currentEdges: DagEdge[],
): DagEdge[] {
  return flowEdges.map((flowEdge) => {
    const fromData = dagEdgeFromData(flowEdge.data);
    const branch = fromData?.branch ?? (flowEdge.sourceHandle && flowEdge.sourceHandle !== 'out'
      ? flowEdge.sourceHandle
      : null);
    const current = currentEdges.find((edge) => (
      edge.source === flowEdge.source
      && edge.target === flowEdge.target
      && (edge.branch ?? null) === branch
    ));
    const original = fromData ?? current;
    return {
      source: flowEdge.source,
      target: flowEdge.target,
      reason: original?.reason ?? 'User dependency.',
      when: original?.when ?? null,
      ...((original?.branch ?? branch) ? { branch: original?.branch ?? branch } : {}),
    };
  });
}

export function rewriteDagEdgeNodeReferences(
  edges: DagEdge[],
  previousNodeId: string,
  nextNodeId: string,
): DagEdge[] {
  return edges.map((edge) => ({
    ...edge,
    source: edge.source === previousNodeId ? nextNodeId : edge.source,
    target: edge.target === previousNodeId ? nextNodeId : edge.target,
    when: edge.when
      ? rewriteNodeOutputRefs(edge.when, previousNodeId, nextNodeId)
      : edge.when,
  }));
}

export function removeDagEdgesForNode(edges: DagEdge[], nodeId: string): DagEdge[] {
  return edges.filter((edge) => (
    edge.source !== nodeId
    && edge.target !== nodeId
    && !collectNodeOutputRefs(edge.when).some((reference) => reference.nodeId === nodeId)
  ));
}

export function replaceIncomingEdgeSources(
  edges: DagEdge[],
  target: string,
  sources: string[],
): DagEdge[] {
  const incoming = new Map(
    edges
      .filter((edge) => edge.target === target)
      .map((edge) => [edge.source, edge]),
  );
  const uniqueSources = [...new Set(sources)].filter((source) => source && source !== target);
  return [
    ...edges.filter((edge) => edge.target !== target),
    ...uniqueSources.map((source) => incoming.get(source) ?? {
      source,
      target,
      reason: 'User dependency.',
      when: null,
    }),
  ];
}

export function buildConditionVariableCatalog(
  dag: Dag,
  edge: DagEdge,
  inputSchema: Record<string, unknown> = {},
  artifacts: Record<string, Artifact> = {},
  capabilities: Pick<CapabilityDefinition, 'id' | 'kind' | 'output_schema'>[] = [],
): VariableCatalog {
  const catalog = buildVariableCatalog(dag, edge.target, inputSchema, artifacts, capabilities);
  const allowedNodeIds = upstreamNodeIds(dag.edges, edge.target);
  return {
    graphInputs: catalog.graphInputs,
    nodeOutputs: catalog.nodeOutputs.filter((item) => {
      const expr = item.binding.$expr;
      return expr.type === 'node_output' && allowedNodeIds.has(expr.node_id);
    }),
    artifacts: catalog.artifacts,
  };
}

function dagEdgeFromData(value: unknown): DagEdge | null {
  if (!value || typeof value !== 'object') return null;
  const dagEdge = (value as { dagEdge?: unknown }).dagEdge;
  if (!dagEdge || typeof dagEdge !== 'object') return null;
  const candidate = dagEdge as Partial<DagEdge>;
  if (typeof candidate.source !== 'string' || typeof candidate.target !== 'string') return null;
  return {
    source: candidate.source,
    target: candidate.target,
    reason: typeof candidate.reason === 'string' ? candidate.reason : '',
    when: candidate.when ?? null,
    ...(candidate.branch ? { branch: candidate.branch } : {}),
  };
}
