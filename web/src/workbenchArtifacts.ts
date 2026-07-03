import type { Artifact, Dag, RunArtifactFile, RunArtifactPreviewKind } from './types';

export type WorkbenchArtifactSource = 'dag' | 'run';

export interface WorkbenchArtifactItem {
  id: string;
  name: string;
  extension: string;
  meta: string;
  source: WorkbenchArtifactSource;
  path?: string;
  description?: string;
  artifactId?: string | null;
  runId?: string;
  previewKind?: RunArtifactPreviewKind;
  previewable?: boolean;
  previewUrl?: string | null;
  downloadUrl?: string | null;
  onlyOfficeConfigUrl?: string | null;
  size?: number | null;
  version?: string | null;
  status?: string;
  error?: string | null;
}

export interface WorkbenchArtifactInput {
  dag?: Dag | null;
  dagArtifacts?: Record<string, Artifact> | null;
  runFiles?: RunArtifactFile[] | null;
  runId?: string | null;
}

export interface WorkbenchArtifactTreeFolder {
  kind: 'folder';
  id: string;
  name: string;
  path: string;
  fileCount: number;
  children: WorkbenchArtifactTreeNode[];
}

export interface WorkbenchArtifactTreeFile {
  kind: 'file';
  id: string;
  name: string;
  path: string;
  item: WorkbenchArtifactItem;
}

export type WorkbenchArtifactTreeNode = WorkbenchArtifactTreeFolder | WorkbenchArtifactTreeFile;

export function buildWorkbenchArtifacts({
  dag,
  dagArtifacts,
  runFiles,
  runId,
}: WorkbenchArtifactInput): WorkbenchArtifactItem[] {
  const dagItems = Object.values(dagArtifacts ?? {})
    .map(dagArtifactItem)
    .sort(compareArtifactItems);
  const runItems = (runFiles ?? [])
    .map((file) => runFileArtifactItem(file, runId ?? undefined))
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
  return [item.description, item.path ? `Path: ${item.path}` : ''].filter(Boolean).join('\n\n');
}

export function buildWorkbenchArtifactTree(items: WorkbenchArtifactItem[]): WorkbenchArtifactTreeNode[] {
  const roots: WorkbenchArtifactTreeNode[] = [];
  const folders = new Map<string, WorkbenchArtifactTreeFolder>();

  items.forEach((item) => {
    const pathParts = artifactPathParts(item.path || item.name);
    const fileName = pathParts.pop() || item.name;
    let children = roots;
    const folderParts: string[] = [];

    pathParts.forEach((part) => {
      folderParts.push(part);
      const folderPath = folderParts.join('/');
      const id = artifactFolderId(folderPath);
      let folder = folders.get(id);
      if (!folder) {
        folder = {
          kind: 'folder',
          id,
          name: part,
          path: folderPath,
          fileCount: 0,
          children: [],
        };
        folders.set(id, folder);
        children.push(folder);
      }
      folder.fileCount += 1;
      children = folder.children;
    });

    children.push({
      kind: 'file',
      id: item.id,
      name: fileName,
      path: item.path || item.name,
      item,
    });
  });

  sortArtifactTree(roots);
  return roots;
}

export function artifactFolderIdsForPath(path: string | undefined): string[] {
  const parts = artifactPathParts(path);
  parts.pop();
  return parts.map((_, index) => artifactFolderId(parts.slice(0, index + 1).join('/')));
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

function runFileArtifactItem(file: RunArtifactFile, runId: string | undefined): WorkbenchArtifactItem {
  const name = file.name || basename(file.path) || file.id;
  const meta = [
    file.media_type,
    typeof file.size === 'number' ? formatBytes(file.size) : '',
  ].filter(Boolean).join(' · ');
  return {
    id: file.id,
    name,
    extension: fileExtension(name),
    meta: meta || 'runtime artifact',
    source: 'run',
    path: file.path,
    artifactId: file.artifact_id ?? null,
    runId,
    previewKind: file.preview_kind ?? undefined,
    previewable: file.previewable,
    previewUrl: file.preview_url ?? null,
    downloadUrl: file.download_url ?? null,
    onlyOfficeConfigUrl: file.onlyoffice_config_url ?? null,
    size: file.size ?? null,
    version: file.version ?? null,
    status: file.status,
    error: file.error ?? null,
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

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function compareArtifactItems(left: WorkbenchArtifactItem, right: WorkbenchArtifactItem): number {
  return left.name.localeCompare(right.name);
}

function artifactPathParts(path: string | undefined): string[] {
  return (path || '')
    .replace(/\\/g, '/')
    .split('/')
    .map((part) => part.trim())
    .filter(Boolean);
}

function artifactFolderId(path: string): string {
  return `folder:${path}`;
}

function sortArtifactTree(nodes: WorkbenchArtifactTreeNode[]): void {
  nodes.sort((left, right) => {
    if (left.kind !== right.kind) return left.kind === 'folder' ? -1 : 1;
    return left.name.localeCompare(right.name);
  });
  nodes.forEach((node) => {
    if (node.kind === 'folder') sortArtifactTree(node.children);
  });
}
