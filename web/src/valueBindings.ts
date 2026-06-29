import type {
  Artifact,
  CapabilityDefinition,
  Dag,
  DagEdge,
  DagNode,
  ValueBinding,
  ValueExpr,
  ValuePathItem,
} from './types';

export type NodeOutputField = 'value' | 'content' | 'status' | 'steps';
export type ArtifactField = 'path' | 'paths' | 'absolute_path' | 'absolute_paths';

export interface NodeOutputReference {
  nodeId: string;
  field: NodeOutputField;
  path: ValuePathItem[];
}

export interface BindingLabelContext {
  dag?: Dag;
  artifacts?: Record<string, Artifact>;
}

export interface VariableCatalog {
  graphInputs: VariableCatalogItem[];
  nodeOutputs: VariableCatalogItem[];
  artifacts: VariableCatalogItem[];
}

export interface VariableCatalogItem {
  id: string;
  label: string;
  binding: ValueBinding;
}

export function makeGraphInputBinding(path: ValuePathItem[] = []): ValueBinding {
  return {
    $expr: {
      type: 'graph_input',
      path,
    },
  };
}

export function makeNodeOutputBinding(
  nodeId: string,
  field: NodeOutputField = 'value',
  path: ValuePathItem[] = [],
): ValueBinding {
  return {
    $expr: {
      type: 'node_output',
      node_id: nodeId,
      field,
      path,
    },
  };
}

export function makeArtifactBinding(artifactId: string, field: ArtifactField = 'path'): ValueBinding {
  return {
    $expr: {
      type: 'artifact',
      artifact_id: artifactId,
      field,
    },
  };
}

export function isValueBinding(value: unknown): value is ValueBinding {
  if (!isRecord(value) || !isRecord(value.$expr)) return false;
  if (Object.keys(value).length !== 1) return false;
  return isValueExpr(value.$expr);
}

export function bindingLabel(binding: ValueBinding, _context: BindingLabelContext = {}): string {
  const expr = binding.$expr;
  if (expr.type === 'graph_input') {
    return pathLabel('DAG input', expr.path);
  }
  if (expr.type === 'node_output') {
    const field = expr.field ?? 'value';
    const base = field === 'value' ? `${expr.node_id}.output` : `${expr.node_id}.${field}`;
    return pathLabel(base, expr.path);
  }
  if (expr.type === 'artifact') {
    return `artifact.${expr.artifact_id}.${expr.field ?? 'path'}`;
  }
  if (expr.type === 'format') {
    return `format(${expr.template})`;
  }
  return 'value binding';
}

export function collectNodeOutputRefs(value: unknown): NodeOutputReference[] {
  const refs: NodeOutputReference[] = [];
  visitValues(value, (item) => {
    if (!isValueBinding(item)) return;
    const expr = item.$expr;
    if (expr.type !== 'node_output') return;
    refs.push({
      nodeId: expr.node_id,
      field: expr.field ?? 'value',
      path: expr.path ?? [],
    });
  });
  return refs;
}

export function rewriteNodeOutputRefs<T>(value: T, oldNodeId: string, nextNodeId: string): T {
  if (isValueBinding(value)) {
    const expr = value.$expr;
    if (expr.type === 'node_output' && expr.node_id === oldNodeId) {
      return {
        $expr: {
          ...expr,
          node_id: nextNodeId,
        },
      } as T;
    }
    if (expr.type === 'format') {
      return {
        $expr: {
          ...expr,
          values: rewriteNodeOutputRefs(expr.values ?? {}, oldNodeId, nextNodeId),
        },
      } as T;
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => rewriteNodeOutputRefs(item, oldNodeId, nextNodeId)) as T;
  }
  if (isRecord(value)) {
    const next: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      next[key] = rewriteNodeOutputRefs(item, oldNodeId, nextNodeId);
    }
    return next as T;
  }
  return value;
}

export function removeNodeOutputRefs<T>(value: T, nodeId: string): T {
  const next = removeNodeOutputRefsInner(value, nodeId);
  return (next === undefined ? {} : next) as T;
}

export function buildVariableCatalog(
  dag: Dag,
  targetNodeId: string,
  inputSchema: Record<string, unknown> = {},
  artifacts: Record<string, Artifact> = {},
  capabilities: Pick<CapabilityDefinition, 'id' | 'kind' | 'output_schema'>[] = [],
): VariableCatalog {
  const edges = dag.edges ?? [];
  const capabilityById = new Map(capabilities.map((capability) => [capability.id, capability]));
  return {
    graphInputs: graphInputCatalogItems(inputSchema),
    nodeOutputs: dag.nodes
      .filter((node) => node.id !== targetNodeId && !wouldCreateCycle(edges, node.id, targetNodeId))
      .flatMap((node) => nodeOutputCatalogItems(node, capabilityById)),
    artifacts: Object.values(artifacts)
      .sort((left, right) => left.id.localeCompare(right.id))
      .flatMap((artifact) => artifactCatalogItems(artifact)),
  };
}

export function hasPathToNode(edges: DagEdge[], source: string, target: string): boolean {
  return upstreamNodeIds(edges, target).has(source);
}

export function wouldCreateCycle(edges: DagEdge[], source: string, target: string): boolean {
  return source === target || hasPathToNode(edges, target, source);
}

function removeNodeOutputRefsInner(value: unknown, nodeId: string): unknown {
  if (isValueBinding(value)) {
    const expr = value.$expr;
    if (expr.type === 'node_output' && expr.node_id === nodeId) return undefined;
    if (expr.type === 'format') {
      return {
        $expr: {
          ...expr,
          values: removeNodeOutputRefsInner(expr.values ?? {}, nodeId),
        },
      };
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => removeNodeOutputRefsInner(item, nodeId))
      .filter((item) => item !== undefined);
  }
  if (isRecord(value)) {
    const next: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      const cleaned = removeNodeOutputRefsInner(item, nodeId);
      if (cleaned !== undefined) next[key] = cleaned;
    }
    return next;
  }
  return value;
}

function graphInputCatalogItems(inputSchema: Record<string, unknown>): VariableCatalogItem[] {
  const properties = schemaProperties(inputSchema);
  if (!Object.keys(properties).length) {
    return [{
      id: 'graph_input',
      label: 'DAG input',
      binding: makeGraphInputBinding(),
    }];
  }
  return Object.keys(properties).sort().map((key) => ({
    id: `graph_input.${key}`,
    label: `DAG input.${key}`,
    binding: makeGraphInputBinding([key]),
  }));
}

function nodeOutputCatalogItems(
  node: DagNode,
  capabilityById: Map<string, Pick<CapabilityDefinition, 'id' | 'kind' | 'output_schema'>>,
): VariableCatalogItem[] {
  const items: VariableCatalogItem[] = [
    {
      id: `node.${node.id}.value`,
      label: `${node.id}.output`,
      binding: makeNodeOutputBinding(node.id),
    },
    {
      id: `node.${node.id}.content`,
      label: `${node.id}.content`,
      binding: makeNodeOutputBinding(node.id, 'content'),
    },
    {
      id: `node.${node.id}.status`,
      label: `${node.id}.status`,
      binding: makeNodeOutputBinding(node.id, 'status'),
    },
    {
      id: `node.${node.id}.steps`,
      label: `${node.id}.steps`,
      binding: makeNodeOutputBinding(node.id, 'steps'),
    },
  ];
  items.push(...nodeOutputSchemaCatalogItems(node, capabilityById));
  return items;
}

function nodeOutputSchemaCatalogItems(
  node: DagNode,
  capabilityById: Map<string, Pick<CapabilityDefinition, 'id' | 'kind' | 'output_schema'>>,
): VariableCatalogItem[] {
  const invocation = node.payload.type === 'capability' ? node.payload.invocation : null;
  if (!invocation) return [];
  const capability = capabilityById.get(invocation.capability_id);
  if (!capability || !['tool', 'mcp'].includes(capability.kind)) return [];
  return Object.keys(schemaProperties(capability.output_schema ?? {})).sort().map((key) => ({
    id: `node.${node.id}.value.${key}`,
    label: `${node.id}.output.${key}`,
    binding: makeNodeOutputBinding(node.id, 'value', [key]),
  }));
}

function artifactCatalogItems(artifact: Artifact): VariableCatalogItem[] {
  return (['path', 'paths', 'absolute_path', 'absolute_paths'] as ArtifactField[]).map((field) => ({
    id: `artifact.${artifact.id}.${field}`,
    label: `artifact.${artifact.id}.${field}`,
    binding: makeArtifactBinding(artifact.id, field),
  }));
}

function upstreamNodeIds(edges: DagEdge[], targetNodeId: string): Set<string> {
  const incoming = new Map<string, string[]>();
  for (const edge of edges) {
    const sources = incoming.get(edge.target) ?? [];
    sources.push(edge.source);
    incoming.set(edge.target, sources);
  }

  const upstream = new Set<string>();
  const pending = [...(incoming.get(targetNodeId) ?? [])];
  while (pending.length) {
    const nodeId = pending.pop();
    if (!nodeId || upstream.has(nodeId)) continue;
    upstream.add(nodeId);
    pending.push(...(incoming.get(nodeId) ?? []));
  }
  return upstream;
}

function visitValues(value: unknown, visitor: (value: unknown) => void): void {
  visitor(value);
  if (isValueBinding(value)) {
    const expr = value.$expr;
    if (expr.type === 'format') {
      visitValues(expr.values ?? {}, visitor);
    }
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) visitValues(item, visitor);
    return;
  }
  if (isRecord(value)) {
    for (const item of Object.values(value)) visitValues(item, visitor);
  }
}

function isValueExpr(value: unknown): value is ValueExpr {
  if (!isRecord(value) || typeof value.type !== 'string') return false;
  if (value.type === 'graph_input') return optionalPath(value.path);
  if (value.type === 'node_output') {
    const field = value.field ?? 'value';
    return typeof value.node_id === 'string'
      && ['value', 'content', 'status', 'steps'].includes(String(field))
      && optionalPath(value.path);
  }
  if (value.type === 'artifact') {
    const field = value.field ?? 'path';
    return typeof value.artifact_id === 'string'
      && ['path', 'paths', 'absolute_path', 'absolute_paths'].includes(String(field));
  }
  if (value.type === 'format') {
    return typeof value.template === 'string'
      && (value.values === undefined || isRecord(value.values));
  }
  return false;
}

function optionalPath(value: unknown): value is ValuePathItem[] | undefined {
  return value === undefined
    || (Array.isArray(value) && value.every((item) => typeof item === 'string' || typeof item === 'number'));
}

function pathLabel(base: string, path: ValuePathItem[] = []): string {
  if (!path.length) return base;
  return `${base}.${path.map(String).join('.')}`;
}

function schemaProperties(schema: Record<string, unknown>): Record<string, unknown> {
  const properties = schema.properties;
  if (!isRecord(properties)) return {};
  return properties;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
