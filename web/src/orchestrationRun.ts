import type { Artifact, CapabilityStreamEvent, RiskLevel, UserDag, UserDagNode } from './types';

export interface RunArtifactSummary {
  id: string;
  label: string;
  path: string;
  kind: 'file' | 'artifact';
}

export interface RunRiskSummary {
  id: string;
  capabilityId: string;
  risk: RiskLevel;
}

export interface RunDialogIssue {
  message: string;
  nodeId?: string;
}

export interface RunDialogSummary {
  nodeCount: number;
  edgeCount: number;
  inputArtifacts: RunArtifactSummary[];
  outputArtifacts: RunArtifactSummary[];
  riskyNodes: RunRiskSummary[];
  issues: RunDialogIssue[];
  canRun: boolean;
}

export type RunTranscriptItem =
  | { type: 'text'; content: string }
  | { type: 'capability'; event: CapabilityStreamEvent; result?: CapabilityStreamEvent };

export function buildRunDialogSummary(spec: UserDag): RunDialogSummary {
  const artifacts = spec.artifacts ?? {};
  const inputIds = new Set<string>();
  const outputIds = new Set<string>();
  const issues: RunDialogIssue[] = [];
  const riskyNodes: RunRiskSummary[] = [];

  for (const node of spec.nodes ?? []) {
    const target = node.target?.trim() ?? '';
    const risk = riskFromTarget(target);
    if (!target) {
      issues.push({ nodeId: node.id, message: `Node '${node.id}' is missing a target.` });
    }
    if (target && risk !== 'low') {
      riskyNodes.push({
        id: node.id,
        capabilityId: target,
        risk,
      });
    }
    collectArtifactReferences(node, 'artifact_inputs', 'input', artifacts, inputIds, issues);
    collectArtifactReferences(node, 'artifact_outputs', 'output', artifacts, outputIds, issues);
  }

  if (!(spec.nodes ?? []).length) {
    issues.push({ message: 'Add at least one node before running.' });
  }

  return {
    nodeCount: spec.nodes?.length ?? 0,
    edgeCount: spec.edges?.length ?? 0,
    inputArtifacts: summarizeArtifacts(inputIds, artifacts),
    outputArtifacts: summarizeArtifacts(outputIds, artifacts),
    riskyNodes,
    issues,
    canRun: issues.length === 0,
  };
}

export function appendRunTranscriptToken(
  timeline: RunTranscriptItem[],
  content: string,
): RunTranscriptItem[] {
  if (!content) return timeline;
  const next = [...timeline];
  const last = next[next.length - 1];
  if (last?.type === 'text') {
    next[next.length - 1] = { ...last, content: `${last.content}${content}` };
    return next;
  }
  return [...next, { type: 'text', content }];
}

export function appendRunTranscriptCapability(
  timeline: RunTranscriptItem[],
  event: CapabilityStreamEvent,
): RunTranscriptItem[] {
  const next = [...timeline];
  if (event.type === 'capability.call.completed' || event.type === 'capability.call.failed') {
    const index = next.findIndex(
      (item) => item.type === 'capability'
        && item.event.invocation_id === event.invocation_id
        && item.event.type === 'capability.call.started',
    );
    if (index !== -1) {
      const item = next[index] as { type: 'capability'; event: CapabilityStreamEvent; result?: CapabilityStreamEvent };
      next[index] = { ...item, result: event };
      return next;
    }
  }
  return [...next, { type: 'capability', event }];
}

function collectArtifactReferences(
  node: UserDagNode,
  field: 'artifact_inputs' | 'artifact_outputs',
  label: 'input' | 'output',
  artifacts: Record<string, Artifact>,
  ids: Set<string>,
  issues: RunDialogIssue[],
) {
  for (const artifactId of node[field] ?? []) {
    if (artifacts[artifactId]) {
      ids.add(artifactId);
    } else {
      issues.push({
        nodeId: node.id,
        message: `Node '${node.id}' references unknown ${label} artifact '${artifactId}'.`,
      });
    }
  }
}

function riskFromTarget(target: string): RiskLevel {
  return target.startsWith('agent.') ? 'medium' : 'low';
}

function summarizeArtifacts(ids: Set<string>, artifacts: Record<string, Artifact>): RunArtifactSummary[] {
  return [...ids]
    .map((id) => artifactSummary(artifacts[id]))
    .filter((artifact): artifact is RunArtifactSummary => Boolean(artifact))
    .sort((left, right) => left.path.localeCompare(right.path));
}

function artifactSummary(artifact?: Artifact): RunArtifactSummary | null {
  if (!artifact) return null;
  const uploadedFile = isUploadedFileArtifactForRun(artifact);
  const displayName = artifact.metadata?.display_name;
  return {
    id: artifact.id,
    label: uploadedFile && typeof displayName === 'string' && displayName.trim()
      ? displayName
      : artifact.id,
    path: artifact.paths?.[0] || artifact.id,
    kind: uploadedFile ? 'file' : 'artifact',
  };
}

function isUploadedFileArtifactForRun(artifact: Artifact): boolean {
  return artifact.metadata?.source === 'upload'
    && artifact.metadata?.kind === 'file'
    && artifact.metadata?.hidden === true;
}
