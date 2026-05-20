import type { Artifact, DagSpec } from './types';

export interface ArtifactDraft {
  id: string;
  paths: string[];
  description?: string;
  required?: boolean;
  metadata?: Record<string, unknown>;
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

export function removeArtifactBinding(spec: DagSpec, artifactId: string): DagSpec {
  const artifacts = { ...(spec.artifacts ?? {}) };
  delete artifacts[artifactId];
  return {
    ...spec,
    artifacts,
    nodes: spec.nodes.map((node) => ({
      ...node,
      ...(node.inputs ? { inputs: node.inputs.filter((id) => id !== artifactId) } : {}),
      ...(node.outputs ? { outputs: node.outputs.filter((id) => id !== artifactId) } : {}),
    })),
  };
}

export function artifactPlaceholder(artifactId: string): string {
  return `{{artifact.${artifactId}.path}}`;
}
