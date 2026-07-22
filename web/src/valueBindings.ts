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

export type FormatValueBinding = ValueBinding & {
  $expr: Extract<ValueExpr, { type: 'format' }>;
};

export interface TemplateCompilation {
  template: string;
  placeholders: string[];
  hasEscapedLiteral: boolean;
}

export interface TemplateVariableInsertion {
  source: string;
  placeholder: string;
  cursor: number;
  binding: FormatValueBinding;
}

const templateVariablePattern = /^[\p{L}_][\p{L}\p{N}_]*$/u;

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

export function compileTemplateSyntax(source: string): TemplateCompilation {
  const placeholders: string[] = [];
  let hasEscapedLiteral = false;
  let template = '';
  let index = 0;
  while (index < source.length) {
    if (source[index] === '\\' && source.slice(index + 1, index + 3) === '{{') {
      hasEscapedLiteral = true;
      const close = source.indexOf('}}', index + 3);
      const end = close === -1 ? source.length : close + 2;
      template += escapeFormatLiteral(source.slice(index + 1, end));
      index = end;
      continue;
    }
    if (source.slice(index, index + 2) === '{{') {
      const close = source.indexOf('}}', index + 2);
      if (close !== -1) {
        const name = source.slice(index + 2, close).trim();
        if (templateVariablePattern.test(name)) {
          template += `{${name}}`;
          if (!placeholders.includes(name)) placeholders.push(name);
          index = close + 2;
          continue;
        }
        template += escapeFormatLiteral(source.slice(index, close + 2));
        index = close + 2;
        continue;
      }
    }
    if (source[index] === '{') {
      template += '{{';
    } else if (source[index] === '}') {
      template += '}}';
    } else {
      template += source[index];
    }
    index += 1;
  }
  return { template, placeholders, hasEscapedLiteral };
}

export function makeFormatBinding(
  source: string,
  previousValues: Record<string, unknown> = {},
): FormatValueBinding {
  const compilation = compileTemplateSyntax(source);
  const values: Record<string, unknown> = {};
  for (const name of compilation.placeholders) {
    if (Object.prototype.hasOwnProperty.call(previousValues, name)) {
      values[name] = previousValues[name];
    }
  }
  return {
    $expr: {
      type: 'format',
      template: compilation.template,
      values,
    },
  };
}

export function insertTemplateVariable(
  source: string,
  selectionStart: number,
  selectionEnd: number,
  item: VariableCatalogItem,
  previousValues: Record<string, unknown> = {},
): TemplateVariableInsertion {
  const compilation = compileTemplateSyntax(source);
  const existingName = compilation.placeholders.find((name) => (
    isValueBinding(previousValues[name])
    && sameValueBinding(previousValues[name], item.binding)
  ));
  const usedNames = new Set([...compilation.placeholders, ...Object.keys(previousValues)]);
  const placeholder = existingName ?? uniqueTemplatePlaceholderName(item, usedNames);
  const token = `{{ ${placeholder} }}`;
  const first = Math.max(0, Math.min(source.length, selectionStart));
  const second = Math.max(0, Math.min(source.length, selectionEnd));
  const start = Math.min(first, second);
  const end = Math.max(first, second);
  const nextSource = `${source.slice(0, start)}${token}${source.slice(end)}`;
  const nextValues = { ...previousValues, [placeholder]: item.binding };
  return {
    source: nextSource,
    placeholder,
    cursor: start + token.length,
    binding: makeFormatBinding(nextSource, nextValues),
  };
}

export function isFormatBinding(value: unknown): value is FormatValueBinding {
  return isValueBinding(value) && value.$expr.type === 'format';
}

export function formatBindingDisplayTemplate(value: FormatValueBinding): string | null {
  return parseRuntimeFormat(value.$expr.template).display;
}

export function formatBindingPlaceholders(value: FormatValueBinding): string[] {
  return parseRuntimeFormat(value.$expr.template).placeholders;
}

export function collectUnboundFormatPlaceholders(value: unknown): string[] {
  const unbound: string[] = [];
  visitValues(value, (item) => {
    if (!isFormatBinding(item)) return;
    const values = item.$expr.values ?? {};
    for (const name of formatBindingPlaceholders(item)) {
      if (!Object.prototype.hasOwnProperty.call(values, name) && !unbound.includes(name)) {
        unbound.push(name);
      }
    }
  });
  return unbound;
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
  return Object.keys(outputSchemaProperties(capability.output_schema ?? {})).sort().map((key) => ({
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

function uniqueTemplatePlaceholderName(item: VariableCatalogItem, usedNames: Set<string>): string {
  const base = templatePlaceholderBase(item);
  if (!usedNames.has(base)) return base;
  let suffix = 2;
  while (usedNames.has(`${base}_${suffix}`)) suffix += 1;
  return `${base}_${suffix}`;
}

function templatePlaceholderBase(item: VariableCatalogItem): string {
  const expr = item.binding.$expr;
  let parts: ValuePathItem[];
  if (expr.type === 'graph_input') {
    parts = expr.path?.length ? expr.path : ['input'];
  } else if (expr.type === 'node_output') {
    parts = [
      expr.node_id,
      expr.field === undefined || expr.field === 'value' ? 'output' : expr.field,
      ...(expr.path ?? []),
    ];
  } else if (expr.type === 'artifact') {
    parts = [expr.artifact_id, expr.field ?? 'path'];
  } else {
    parts = ['variable'];
  }
  const normalized = normalizeTemplateAlias(parts.map(String).join('_')) || 'variable';
  return templateVariablePattern.test(normalized) ? normalized : `value_${normalized}`;
}

function sameValueBinding(left: ValueBinding, right: ValueBinding): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function normalizeTemplateAlias(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}_]+/gu, '_')
    .replace(/^_+|_+$/g, '');
}

function escapeFormatLiteral(value: string): string {
  return value.replace(/\{/g, '{{').replace(/\}/g, '}}');
}

function parseRuntimeFormat(template: string): { display: string | null; placeholders: string[] } {
  const segments: Array<{ kind: 'literal' | 'variable'; value: string }> = [];
  const placeholders: string[] = [];
  const appendLiteral = (value: string) => {
    const last = segments[segments.length - 1];
    if (last?.kind === 'literal') {
      last.value += value;
    } else {
      segments.push({ kind: 'literal', value });
    }
  };
  let index = 0;
  while (index < template.length) {
    if (template.slice(index, index + 2) === '{{') {
      appendLiteral('{');
      index += 2;
      continue;
    }
    if (template.slice(index, index + 2) === '}}') {
      appendLiteral('}');
      index += 2;
      continue;
    }
    if (template[index] === '{') {
      const close = template.indexOf('}', index + 1);
      if (close === -1) return { display: null, placeholders };
      const name = template.slice(index + 1, close);
      if (!templateVariablePattern.test(name)) return { display: null, placeholders };
      segments.push({ kind: 'variable', value: name });
      if (!placeholders.includes(name)) placeholders.push(name);
      index = close + 1;
      continue;
    }
    if (template[index] === '}') return { display: null, placeholders };
    appendLiteral(template[index]);
    index += 1;
  }
  const display = segments.map((segment) => (
    segment.kind === 'variable'
      ? `{{ ${segment.value} }}`
      : escapeLiteralTemplateTokens(segment.value)
  )).join('');
  if (compileTemplateSyntax(display).template !== template) {
    return { display: null, placeholders };
  }
  return { display, placeholders };
}

function escapeLiteralTemplateTokens(value: string): string {
  return value.replace(/(\{\{\s*[\p{L}_][\p{L}\p{N}_]*\s*\}\})/gu, '\\$1');
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

function outputSchemaProperties(schema: Record<string, unknown>): Record<string, unknown> {
  if (schema.type !== undefined && schema.type !== 'object') return {};
  return schemaProperties(schema);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
