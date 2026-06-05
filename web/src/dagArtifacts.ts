import type { Artifact, UserDag, ValueBinding } from './types';

export interface UploadSourceFile {
  name: string;
  relativePath?: string;
  webkitRelativePath?: string;
}

export interface ArtifactDraft {
  id: string;
  paths: string[];
  description?: string;
  required?: boolean;
  metadata?: Record<string, unknown>;
}

export interface UploadedFileArtifact {
  source: UploadSourceFile;
  artifact: Artifact;
}

export interface CreateUploadedFileArtifactsOptions {
  artifacts: Record<string, Artifact>;
  uploadRoot: string;
}

export interface UploadFormFilenameOptions {
  preserveRelativePath?: boolean;
}

export function normalizeArtifact(artifact: ArtifactDraft): Artifact {
  return {
    id: artifact.id.trim(),
    paths: artifact.paths.map((path) => path.trim()).filter(Boolean),
    description: artifact.description ?? '',
    required: artifact.required ?? true,
    metadata: artifact.metadata ?? {},
  };
}

export function upsertArtifact(
  artifacts: Record<string, Artifact>,
  artifact: ArtifactDraft,
  previousId?: string,
): Record<string, Artifact> {
  const normalized = normalizeArtifact(artifact);
  if (!normalized.id) return artifacts;
  const next = { ...artifacts };
  if (previousId && previousId !== normalized.id) {
    delete next[previousId];
  }
  next[normalized.id] = normalized;
  return next;
}

export function removeArtifactBinding(spec: UserDag, artifactId: string): UserDag {
  const artifacts = { ...(spec.artifacts ?? {}) };
  delete artifacts[artifactId];
  return {
    ...spec,
    artifacts,
    nodes: spec.nodes.map((node) => ({
      ...node,
      inputs: removeArtifactValueRefs(node.inputs ?? {}, artifactId) as Record<string, unknown>,
      artifact_inputs: (node.artifact_inputs ?? []).filter((id) => id !== artifactId),
      artifact_outputs: (node.artifact_outputs ?? []).filter((id) => id !== artifactId),
    })),
  };
}

export function artifactPathExpr(artifactId: string): ValueBinding {
  return {
    $expr: {
      type: 'artifact',
      artifact_id: artifactId,
      field: 'path',
    },
  };
}

export function isUploadedFileArtifact(artifact: Artifact): boolean {
  return artifact.metadata?.source === 'upload'
    && artifact.metadata?.kind === 'file'
    && artifact.metadata?.hidden === true;
}

export function createUploadedFileArtifacts(
  files: UploadSourceFile[],
  options: CreateUploadedFileArtifactsOptions,
): { artifacts: Record<string, Artifact>; uploads: UploadedFileArtifact[] } {
  const next = { ...options.artifacts };
  const usedIds = new Set(Object.keys(next));
  const uploadRoot = normalizeUploadPath(options.uploadRoot) || 'inputs/uploads';
  const uploads = files.map((source) => {
    const relativePath = sourceUploadPath(source);
    const artifactPath = joinArtifactPath(uploadRoot, relativePath);
    const id = uniqueArtifactId(`upload_${slugForId(artifactPath)}`, usedIds);
    const artifact: Artifact = {
      id,
      paths: [artifactPath],
      description: source.name,
      required: true,
      metadata: {
        source: 'upload',
        kind: 'file',
        hidden: true,
        display_name: source.name,
        relative_path: relativePath,
      },
    };
    next[id] = artifact;
    return { source, artifact };
  });
  return { artifacts: next, uploads };
}

export function uploadFormFilename(
  file: UploadSourceFile,
  options: UploadFormFilenameOptions = {},
): string {
  if (options.preserveRelativePath === false) {
    return sanitizePathSegment(file.name || 'upload');
  }
  return sourceUploadPath(file);
}

function sourceUploadPath(file: UploadSourceFile): string {
  return normalizeUploadPath(file.relativePath || file.webkitRelativePath || file.name || 'upload') || 'upload';
}

function normalizeUploadPath(path: string): string {
  return path
    .replace(/\\/g, '/')
    .split('/')
    .map((part) => part.trim())
    .filter((part) => part && part !== '.' && part !== '..')
    .map(sanitizePathSegment)
    .filter(Boolean)
    .join('/');
}

function sanitizePathSegment(segment: string): string {
  return segment.replace(/[<>:"|?*]/g, '_').trim();
}

function joinArtifactPath(root: string, relativePath: string): string {
  return [root, relativePath].filter(Boolean).join('/');
}

function removeArtifactValueRefs(value: unknown, artifactId: string): unknown {
  if (!value || typeof value !== 'object') return value;
  if (Array.isArray(value)) {
    return value
      .map((item) => removeArtifactValueRefs(item, artifactId))
      .filter((item) => item !== undefined);
  }

  const record = value as Record<string, unknown>;
  const expr = record.$expr;
  if (expr && typeof expr === 'object' && !Array.isArray(expr)) {
    const typedExpr = expr as Record<string, unknown>;
    if (typedExpr.type === 'artifact' && typedExpr.artifact_id === artifactId) {
      return undefined;
    }
    if (typedExpr.type === 'format') {
      return {
        $expr: {
          ...typedExpr,
          values: removeArtifactValueRefs(typedExpr.values ?? {}, artifactId),
        },
      };
    }
    return record;
  }

  return Object.fromEntries(
    Object.entries(record)
      .map(([key, item]) => [key, removeArtifactValueRefs(item, artifactId)] as const)
      .filter(([, item]) => item !== undefined),
  );
}

function slugForId(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '') || 'file';
}

function uniqueArtifactId(baseId: string, usedIds: Set<string>): string {
  let candidate = baseId;
  let index = 2;
  while (usedIds.has(candidate)) {
    candidate = `${baseId}_${index}`;
    index += 1;
  }
  usedIds.add(candidate);
  return candidate;
}
