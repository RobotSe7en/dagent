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
  SkillFileDetail,
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

export async function getSkillFile(name: string, filePath: string): Promise<SkillFileDetail> {
  const params = new URLSearchParams({ file_path: filePath });
  const res = await fetch(`${API_BASE}/skills/${skillPath(name)}?${params.toString()}`);
  if (!res.ok) throw new Error(await errorMessage(res));
  return await res.json();
}

export async function installSkill(payload: {
  file?: File;
  content?: string;
  name?: string;
  description?: string;
  category?: string;
}): Promise<SkillDetail> {
  const form = new FormData();
  if (payload.file) form.append('file', payload.file);
  if (payload.content) form.append('content', payload.content);
  if (payload.name) form.append('name', payload.name);
  if (payload.description) form.append('description', payload.description);
  if (payload.category) form.append('category', payload.category);
  const res = await fetch(`${API_BASE}/skills/install`, {
    method: 'POST',
    body: form,
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.skill;
}

export async function deleteSkill(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/skills/${skillPath(name)}`, {
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

interface ApiRunResult {
  kind: 'tool' | 'dynamic_dag' | 'static_dag';
  status: string;
  run_id: string | null;
  spec_id?: string | null;
  workspace_path?: string | null;
  output_text: string;
  dag: Dag | null;
  trace?: RunTrace | null;
  pending_review?: ReviewEventPayload | null;
  dag_run?: DagRun | null;
}

interface DonePayload {
  type: 'done';
  result: ApiRunResult;
}

interface StreamHandlers {
  onStatus?: (status: string) => void;
  onDag?: (dag: Dag) => void;
  onTrace?: (event: TraceLogEvent) => void;
  onCapability?: (event: CapabilityStreamEvent) => void;
  onToken?: (content: string) => void;
  onRetry?: (event: ValidationFeedbackEvent) => void;
  onValidating?: (event: { type: 'validating'; message: string }) => void;
  onDone?: (payload: DonePayload) => void;
  onError?: (message: string) => void;
}

export interface ChatCapabilityScopePayload {
  capabilityIds: string[] | null;
  skills: string[];
}

export async function streamTask(
  message: string,
  target: 'auto' | 'tool' | 'dag',
  reviewLevel: ReviewLevel,
  handlers: StreamHandlers,
  capabilityScope?: ChatCapabilityScopePayload,
): Promise<void> {
  const body: Record<string, unknown> = { message, target, review_level: reviewLevel };
  if (capabilityScope) {
    body.capability_ids = capabilityScope.capabilityIds;
    body.skills = capabilityScope.skills;
  }
  const response = await fetch(`${API_BASE}/messages/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage(response));
  }

  await readStream(response, handlers);
}

export async function resumeDagReview(
  reviewId: string,
  dag: Dag | null,
  reviewLevel: ReviewLevel,
  approved: boolean,
  handlers: StreamHandlers,
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
  handlers: StreamHandlers,
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
  await readStream(response, handlers);
}

async function readStream(response: Response, handlers: StreamHandlers) {
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
      const eventType = event.type === 'status' && typeof event.data?.type === 'string'
        ? event.data.type
        : event.type;
      const typedEvent = eventType === event.type ? event : { ...event, type: eventType };
      if (eventType === 'status') handlers.onStatus?.(event.message);
      if (eventType === 'dag') handlers.onDag?.(event.dag);
      if (eventType === 'trace') emitTraceSnapshot(event.trace, handlers.onTrace, seenTraceIds);
      if (eventType === 'capability_call' || eventType === 'capability_result' || eventType === 'capability_error') {
        handlers.onCapability?.(typedEvent);
      }
      if (eventType === 'token') handlers.onToken?.(event.content);
      if (eventType === 'retry' || eventType === 'validation_passed') handlers.onRetry?.(typedEvent);
      if (eventType === 'validating') handlers.onValidating?.(typedEvent);
      if (eventType === 'done') {
        const trace = event.result?.trace ?? event.result?.dag_run?.trace;
        emitTraceSnapshot(trace, handlers.onTrace, seenTraceIds);
        handlers.onDone?.(typedEvent);
      }
      if (eventType === 'error') handlers.onError?.(event.message);
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
  handlers: StreamHandlers,
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
