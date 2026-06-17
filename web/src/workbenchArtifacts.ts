import type { Artifact, Dag } from './types';

export type WorkbenchArtifactSource = 'dag' | 'run';

export interface WorkbenchArtifactItem {
  id: string;
  name: string;
  extension: string;
  meta: string;
  source: WorkbenchArtifactSource;
  path?: string;
  description?: string;
  content?: string;
  value?: unknown;
}

export interface WorkbenchArtifactInput {
  dag?: Dag | null;
  dagArtifacts?: Record<string, Artifact> | null;
  runArtifacts?: Record<string, unknown> | null;
}

export function buildWorkbenchArtifacts({
  dag,
  dagArtifacts,
  runArtifacts,
}: WorkbenchArtifactInput): WorkbenchArtifactItem[] {
  const dagItems = Object.values(dagArtifacts ?? {})
    .map(dagArtifactItem)
    .sort(compareArtifactItems);
  const runItems = Object.entries(runArtifacts ?? {})
    .map(([id, value]) => runArtifactItem(id, value))
    .sort(compareArtifactItems);

  if (!dag?.nodes.length) return [...dagItems, ...runItems];
  const outputIds = new Set(dag.nodes.flatMap((node) => node.outputs ?? []));
  const orderedRunItems = [
    ...runItems.filter((item) => outputIds.has(item.id.replace(/^run:/, ''))),
    ...runItems.filter((item) => !outputIds.has(item.id.replace(/^run:/, ''))),
  ];
  return [...dagItems, ...orderedRunItems];
}

export function artifactPreviewText(item: WorkbenchArtifactItem): string {
  if (item.content) return item.content;
  if (item.value !== undefined) return JSON.stringify(item.value, null, 2);
  return [item.description, item.path ? `Path: ${item.path}` : ''].filter(Boolean).join('\n\n');
}

function dagArtifactItem(artifact: Artifact): WorkbenchArtifactItem {
  const path = artifact.paths?.[0] ?? '';
  const name = displayName(artifact, path);
  return {
    id: `dag:${artifact.id}`,
    name,
    extension: fileExtension(name),
    meta: `${fileExtension(name).toLowerCase()}${path ? ` · ${path}` : ''}`,
    source: 'dag',
    path,
    description: artifact.description,
  };
}

function runArtifactItem(id: string, raw: unknown): WorkbenchArtifactItem {
  const artifact = normalizeRunArtifact(raw);
  const name = artifact.name || basename(artifact.path) || `${id}.json`;
  const value = artifact.content === undefined ? artifact.value : undefined;
  return {
    id: `run:${id}`,
    name,
    extension: value !== undefined ? '{ }' : fileExtension(name),
    meta: artifact.mediaType ? `${artifact.mediaType}${artifact.path ? ` · ${artifact.path}` : ''}` : artifact.path ?? 'runtime artifact',
    source: 'run',
    path: artifact.path,
    content: artifact.content,
    value,
  };
}

function normalizeRunArtifact(raw: unknown): {
  name?: string;
  path?: string;
  content?: string;
  value?: unknown;
  mediaType?: string;
} {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return { value: raw };
  }
  const item = raw as Record<string, unknown>;
  return {
    name: stringValue(item.name),
    path: stringValue(item.path),
    content: stringValue(item.content),
    value: item.value,
    mediaType: stringValue(item.media_type) ?? stringValue(item.mediaType),
  };
}

function displayName(artifact: Artifact, path: string): string {
  const displayNameValue = artifact.metadata?.display_name;
  if (typeof displayNameValue === 'string' && displayNameValue.trim()) return displayNameValue.trim();
  return basename(path) || artifact.description || artifact.id;
}

function basename(path: string | undefined): string {
  if (!path) return '';
  return path.replace(/\\/g, '/').split('/').filter(Boolean).pop() ?? '';
}

function fileExtension(name: string): string {
  const ext = name.includes('.') ? name.split('.').pop() ?? '' : '';
  return ext ? ext.toUpperCase() : 'FILE';
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

function compareArtifactItems(left: WorkbenchArtifactItem, right: WorkbenchArtifactItem): number {
  return left.name.localeCompare(right.name);
}
