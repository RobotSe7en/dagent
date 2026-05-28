import type {
  AgentProfile,
  CapabilityDefinition,
  CapabilityKind,
  CapabilityResult,
  DagRun,
  DagSpec,
  ProfileWarning,
  Dag,
  ReviewLevel,
  ReviewEventPayload,
  CapabilityStreamEvent,
  TraceLogEvent,
  ValidationFeedbackEvent,
  RunTrace,
  RunTraceNode,
  RunTraceStatus,
  SkillDetail,
  SkillSummary,
  MCPServer,
  MCPServerConfig,
} from './types';
import { uploadFormFilename, type UploadFormFilenameOptions } from './dagArtifacts';

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api';

export async function resetSession(): Promise<void> {
  await fetch(`${API_BASE}/session/reset`, { method: 'POST' });
}

export async function getValidationStatus(): Promise<boolean> {
  const res = await fetch(`${API_BASE}/settings/validation`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return Boolean(data.enabled);
}

export async function setValidationEnabled(enabled: boolean): Promise<boolean> {
  const res = await fetch(`${API_BASE}/settings/validation`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return Boolean(data.enabled);
}

export async function listCapabilities(kind?: CapabilityKind): Promise<CapabilityDefinition[]> {
  const suffix = kind ? `?kind=${encodeURIComponent(kind)}` : '';
  const res = await fetch(`${API_BASE}/capabilities${suffix}`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.capabilities ?? [];
}

export async function createCapability(definition: CapabilityDefinition): Promise<CapabilityDefinition> {
  const res = await fetch(`${API_BASE}/capabilities`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(definition),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.capability;
}

export async function setCapabilityEnabled(capabilityId: string, enabled: boolean): Promise<CapabilityDefinition> {
  const res = await fetch(`${API_BASE}/capabilities/${encodeURIComponent(capabilityId)}/${enabled ? 'enable' : 'disable'}`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.capability;
}

export async function deleteCapability(capabilityId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/capabilities/${encodeURIComponent(capabilityId)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error(await errorMessage(res));
}

export async function testCapability(
  capabilityId: string,
  argumentsValue: Record<string, unknown>,
): Promise<CapabilityResult> {
  const res = await fetch(`${API_BASE}/capabilities/${encodeURIComponent(capabilityId)}/test`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ arguments: argumentsValue }),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.result;
}

export async function listDagSpecs(): Promise<DagSpec[]> {
  const res = await fetch(`${API_BASE}/dag-specs`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.dag_specs ?? [];
}

export async function saveDagSpec(spec: DagSpec): Promise<DagSpec> {
  const res = await fetch(`${API_BASE}/dag-specs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(spec),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.dag_spec;
}

export async function uploadDagSpecArtifact(
  specId: string,
  artifactId: string,
  files: File[],
  options: UploadFormFilenameOptions = {},
): Promise<{ artifact_id: string; files: string[] }> {
  const body = new FormData();
  for (const file of files) {
    body.append('files', file, uploadFormFilename(file, options));
  }
  const res = await fetch(
    `${API_BASE}/dag-specs/${encodeURIComponent(specId)}/artifacts/${encodeURIComponent(artifactId)}/upload`,
    {
      method: 'POST',
      body,
    },
  );
  if (!res.ok) throw new Error(await errorMessage(res));
  return await res.json();
}

export async function listProfiles(): Promise<{ profiles: AgentProfile[]; warnings: ProfileWarning[] }> {
  const res = await fetch(`${API_BASE}/profiles`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return {
    profiles: data.profiles ?? [],
    warnings: data.warnings ?? [],
  };
}

export async function listSkills(): Promise<SkillSummary[]> {
  const res = await fetch(`${API_BASE}/skills`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.skills ?? [];
}

export async function getSkill(name: string): Promise<SkillDetail> {
  const res = await fetch(`${API_BASE}/skills/${skillPath(name)}`);
  if (!res.ok) throw new Error(await errorMessage(res));
  return await res.json();
}

export async function importSkill(payload: {
  content: string;
  name?: string;
  description?: string;
  category?: string;
}): Promise<SkillDetail> {
  const res = await fetch(`${API_BASE}/skills/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.skill;
}

export async function deleteImportedSkill(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/skills/imported/${skillPath(name)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error(await errorMessage(res));
}

export async function listMcpServers(): Promise<MCPServer[]> {
  const res = await fetch(`${API_BASE}/mcp/servers`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.servers ?? [];
}

export async function createMcpServer(payload: { name: string } & MCPServerConfig): Promise<MCPServer> {
  const res = await fetch(`${API_BASE}/mcp/servers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.server;
}

export async function updateMcpServer(name: string, payload: { name: string } & MCPServerConfig): Promise<MCPServer> {
  const res = await fetch(`${API_BASE}/mcp/servers/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.server;
}

export async function deleteMcpServer(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/mcp/servers/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error(await errorMessage(res));
}

export async function reloadMcpServers(): Promise<MCPServer[]> {
  const res = await fetch(`${API_BASE}/mcp/reload`, { method: 'POST' });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.servers ?? [];
}

interface DonePayload {
  status?: string;
  task_id: string | null;
  dag: Dag | null;
  pending_review?: ReviewEventPayload | null;
  final_answer: string;
  trace?: RunTrace | null;
}

interface DagRunDonePayload {
  status?: string;
  dag_run: DagRun;
}

export interface ChatCapabilityScopePayload {
  capabilityIds: string[] | null;
  skillNames: string[];
}

export async function streamTask(
  message: string,
  mode: 'auto' | 'tool' | 'dag',
  reviewLevel: ReviewLevel,
  handlers: {
    onStatus?: (status: string) => void;
    onDag?: (dag: Dag) => void;
    onTrace?: (event: TraceLogEvent) => void;
    onCapability?: (event: CapabilityStreamEvent) => void;
    onToken?: (content: string) => void;
    onRetry?: (event: ValidationFeedbackEvent) => void;
    onValidating?: (event: { type: 'validating'; message: string }) => void;
    onDone?: (payload: DonePayload) => void;
    onError?: (message: string) => void;
  },
  capabilityScope?: ChatCapabilityScopePayload,
): Promise<void> {
  const body: Record<string, unknown> = { message, mode, review_level: reviewLevel };
  if (capabilityScope) {
    body.capability_ids = capabilityScope.capabilityIds;
    body.skill_names = capabilityScope.skillNames;
  }
  const response = await fetch(`${API_BASE}/messages/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage(response));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const seenTraceIds = new Set<string>();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';
    for (const frame of frames) {
      const line = frame.split('\n').find((item) => item.startsWith('data: '));
      if (!line) continue;
      const event = JSON.parse(line.slice(6));
      if (event.type === 'status') handlers.onStatus?.(event.message);
      if (event.type === 'dag') handlers.onDag?.(event.dag);
      if (event.type === 'trace') emitTraceSnapshot(event.trace, handlers.onTrace, seenTraceIds);
      if (event.type === 'capability_call' || event.type === 'capability_result' || event.type === 'capability_error') {
        handlers.onCapability?.(event);
      }
      if (event.type === 'token') handlers.onToken?.(event.content);
      if (event.type === 'retry' || event.type === 'validation_passed') handlers.onRetry?.(event);
      if (event.type === 'validating') handlers.onValidating?.(event);
      if (event.type === 'done') handlers.onDone?.(event);
      if (event.type === 'error') handlers.onError?.(event.message);
    }
  }
}

export async function resumeDagReview(
  reviewId: string,
  dag: Dag | null,
  reviewLevel: ReviewLevel,
  approved: boolean,
  handlers: {
    onStatus?: (status: string) => void;
    onDag?: (dag: Dag) => void;
    onTrace?: (event: TraceLogEvent) => void;
    onCapability?: (event: CapabilityStreamEvent) => void;
    onToken?: (content: string) => void;
    onRetry?: (event: ValidationFeedbackEvent) => void;
    onValidating?: (event: { type: 'validating'; message: string }) => void;
    onDone?: (payload: DonePayload) => void;
    onError?: (message: string) => void;
  },
): Promise<void> {
  const response = await fetch(`${API_BASE}/messages/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      review_id: reviewId,
      dag: approved ? dag : null,
      approved,
      review_level: reviewLevel,
    }),
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage(response));
  }
  await readStream(response, handlers);
}

export async function runDagSpecStream(
  specId: string,
  handlers: {
    onStatus?: (status: string) => void;
    onTrace?: (event: TraceLogEvent) => void;
    onCapability?: (event: CapabilityStreamEvent) => void;
    onToken?: (content: string) => void;
    onDone?: (payload: DagRunDonePayload) => void;
    onError?: (message: string) => void;
  },
  options: {
    workspaceRoot?: string;
  } = {},
): Promise<void> {
  const body = options.workspaceRoot?.trim()
    ? JSON.stringify({ workspace_root: options.workspaceRoot.trim() })
    : undefined;
  const response = await fetch(`${API_BASE}/dag-specs/${encodeURIComponent(specId)}/run/stream`, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body,
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage(response));
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const seenTraceIds = new Set<string>();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';
    for (const frame of frames) {
      const line = frame.split('\n').find((item) => item.startsWith('data: '));
      if (!line) continue;
      const event = JSON.parse(line.slice(6));
      if (event.type === 'status') handlers.onStatus?.(event.message);
      if (event.type === 'trace') emitTraceSnapshot(event.trace, handlers.onTrace, seenTraceIds);
      if (event.type === 'capability_call' || event.type === 'capability_result' || event.type === 'capability_error') {
        handlers.onCapability?.(event);
      }
      if (event.type === 'token') handlers.onToken?.(event.content);
      if (event.type === 'done') {
        if (event.dag_run?.trace) emitTraceSnapshot(event.dag_run.trace, handlers.onTrace, seenTraceIds);
        handlers.onDone?.(event);
      }
      if (event.type === 'error') handlers.onError?.(event.message);
    }
  }
}

async function readStream(
  response: Response,
  handlers: {
    onStatus?: (status: string) => void;
    onDag?: (dag: Dag) => void;
    onTrace?: (event: TraceLogEvent) => void;
    onCapability?: (event: CapabilityStreamEvent) => void;
    onToken?: (content: string) => void;
    onRetry?: (event: ValidationFeedbackEvent) => void;
    onValidating?: (event: { type: 'validating'; message: string }) => void;
    onDone?: (payload: DonePayload) => void;
    onError?: (message: string) => void;
  },
) {
  const reader = response.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buffer = '';
  const seenTraceIds = new Set<string>();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';
    for (const frame of frames) {
      const line = frame.split('\n').find((item) => item.startsWith('data: '));
      if (!line) continue;
      const event = JSON.parse(line.slice(6));
      if (event.type === 'status') handlers.onStatus?.(event.message);
      if (event.type === 'dag') handlers.onDag?.(event.dag);
      if (event.type === 'trace') emitTraceSnapshot(event.trace, handlers.onTrace, seenTraceIds);
      if (event.type === 'capability_call' || event.type === 'capability_result' || event.type === 'capability_error') {
        handlers.onCapability?.(event);
      }
      if (event.type === 'token') handlers.onToken?.(event.content);
      if (event.type === 'retry' || event.type === 'validation_passed') handlers.onRetry?.(event);
      if (event.type === 'validating') handlers.onValidating?.(event);
      if (event.type === 'done') handlers.onDone?.(event);
      if (event.type === 'error') handlers.onError?.(event.message);
    }
  }
}

function emitTraceSnapshot(
  trace: RunTrace | undefined,
  onTrace: ((event: TraceLogEvent) => void) | undefined,
  seenTraceIds: Set<string>,
) {
  if (!trace || !onTrace) return;
  for (const event of mapRunTrace(trace)) {
    if (seenTraceIds.has(event.id)) continue;
    seenTraceIds.add(event.id);
    onTrace(event);
  }
}

export function mapRunTrace(trace: RunTrace): TraceLogEvent[] {
  const events: TraceLogEvent[] = [];
  const dagId = typeof trace.root.ref.dag_id === 'string' ? trace.root.ref.dag_id : undefined;

  const visit = (node: RunTraceNode, currentNodeId?: string) => {
    const nodeId = node.kind === 'dag_node' ? node.ref.node_id : currentNodeId;
    if (node.kind !== 'run') {
      events.push(TraceLogEventFromNode(node, trace.run_id, dagId, nodeId));
    }
    node.children.forEach((child) => visit(child, nodeId));
  };

  visit(trace.root);
  return events;
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const payload = await response.json();
    return payload.detail ?? response.statusText;
  } catch {
    return response.statusText;
  }
}

function skillPath(name: string): string {
  return name.split('/').map((part) => encodeURIComponent(part)).join('/');
}

function TraceLogEventFromNode(
  node: RunTraceNode,
  runId: string,
  dagId: string | undefined,
  nodeId: string | undefined,
): TraceLogEvent {
  const eventType = `${node.kind}_${node.status}`;
  const payload = tracePayload(node);
  return {
    event_id: node.id,
    event_type: eventType,
    dag_id: dagId,
    node_id: nodeId ?? null,
    payload,
    created_at: node.ended_at ?? node.started_at ?? undefined,
    id: `${node.id}:${node.status}`,
    type: traceType(node.kind),
    label: node.label || node.ref.capability_id || node.ref.node_id || node.kind,
    detail: traceDetail(node, runId),
    status: traceStatus(node.status),
    timestamp: new Date(node.ended_at ?? node.started_at ?? Date.now()).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }),
  };
}

function tracePayload(node: RunTraceNode): Record<string, unknown> {
  return {
    ...node.ref,
    input: node.input,
    output: node.output,
    error: node.error?.message,
    result: node.capability_execution?.result,
  };
}

function traceDetail(node: RunTraceNode, runId: string): string {
  if (node.error?.message) return node.error.message;
  const result = node.capability_execution?.result;
  if (result?.error) return result.error;
  if (typeof node.output === 'string' && node.output) return clip(node.output);
  if (typeof result?.content === 'string' && result.content) return clip(result.content);
  return node.ref.capability_id ?? node.ref.node_id ?? runId;
}

function traceType(kind: RunTraceNode['kind']): TraceLogEvent['type'] {
  if (kind === 'dag_node') return 'node';
  if (kind === 'capability_call') return 'capability';
  if (kind === 'model_call') return 'model';
  return 'dag';
}

function traceStatus(status: RunTraceStatus): TraceLogEvent['status'] {
  if (status === 'failed' || status === 'cancelled') return 'failed';
  if (status === 'completed') return 'completed';
  if (status === 'planned' || status === 'skipped') return 'queued';
  return 'running';
}

function clip(value: string): string {
  return value.length > 180 ? `${value.slice(0, 177)}...` : value;
}

export async function resumeCapabilityReview(
  reviewId: string,
  approved: boolean,
  handlers: {
    onStatus?: (status: string) => void;
    onToken?: (content: string) => void;
    onRetry?: (event: ValidationFeedbackEvent) => void;
    onValidating?: (event: { type: 'validating'; message: string }) => void;
    onDone?: (payload: DonePayload) => void;
    onError?: (message: string) => void;
  },
): Promise<void> {
  const response = await fetch(`${API_BASE}/messages/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ review_id: reviewId, approved }),
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage((response as unknown) as Response));
  }
  await readStream(response, {
    ...handlers,
    onDag: undefined,
    onTrace: undefined,
    onCapability: undefined,
  });
}
