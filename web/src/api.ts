import type {
  AgentPreset,
  AgentPresetInput,
  AgentProfile,
  CapabilityDefinition,
  CapabilityKind,
  CapabilityResult,
  DagRun,
  UserDag,
  ProfileWarning,
  Dag,
  ReviewLevel,
  ReviewEventPayload,
  CapabilityStreamEvent,
  DagValidationResult,
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
  ModelProvider,
  ModelProviderInput,
  PythonToolConfig,
  PythonToolEntry,
  RunArtifactPreview,
  RunArtifactsResponse,
} from './types';
import { chatScopeRequestFields, type ChatCapabilityScopePayload } from './agentScope';
import { uploadFormFilename, type UploadFormFilenameOptions } from './dagArtifacts';
import {
  responseDeltaPayload,
  runStartedPayload,
  type ResponseDeltaStreamEvent,
  type RunStartedStreamEvent,
} from './streamProtocol';

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

export async function listDags(): Promise<UserDag[]> {
  const res = await fetch(`${API_BASE}/dags`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.dags ?? [];
}

export async function saveDag(spec: UserDag): Promise<UserDag> {
  const res = await fetch(`${API_BASE}/dags`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(spec),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.dag;
}

export async function validateDag(spec: UserDag): Promise<DagValidationResult> {
  const res = await fetch(`${API_BASE}/dags/validate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(spec),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return {
    valid: Boolean(data.valid),
    issues: Array.isArray(data.issues) ? data.issues : [],
  };
}

export async function uploadDagArtifact(
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
    `${API_BASE}/dags/${encodeURIComponent(specId)}/artifacts/${encodeURIComponent(artifactId)}/upload`,
    {
      method: 'POST',
      body,
    },
  );
  if (!res.ok) throw new Error(await errorMessage(res));
  return await res.json();
}

export async function listRunArtifacts(runId: string): Promise<RunArtifactsResponse> {
  const res = await fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/artifacts`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return {
    run_id: data.run_id ?? runId,
    workspace_path: data.workspace_path ?? null,
    artifacts: data.artifacts ?? {},
    files: data.files ?? [],
    files_truncated: Boolean(data.files_truncated),
    file_limit: data.file_limit,
    visit_limit: data.visit_limit,
  };
}

export async function previewRunArtifact(runId: string, path: string): Promise<RunArtifactPreview> {
  const params = new URLSearchParams({ path });
  const res = await fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/artifacts/preview?${params.toString()}`);
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

export async function createProfile(payload: { name: string; content: string }): Promise<AgentProfile> {
  const res = await fetch(`${API_BASE}/profiles`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.profile;
}

export async function updateProfile(name: string, content: string): Promise<AgentProfile> {
  const res = await fetch(`${API_BASE}/profiles/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.profile;
}

export async function deleteProfile(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/profiles/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error(await errorMessage(res));
}

export async function listAgents(): Promise<{ agents: AgentPreset[]; errors: Record<string, string> }> {
  const res = await fetch(`${API_BASE}/agents`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return {
    agents: data.agents ?? [],
    errors: data.errors ?? {},
  };
}

export async function createAgent(payload: AgentPresetInput): Promise<AgentPreset> {
  const res = await fetch(`${API_BASE}/agents`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.agent;
}

export async function updateAgent(name: string, payload: Omit<AgentPresetInput, 'name'>): Promise<AgentPreset> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.agent;
}

export async function deleteAgent(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error(await errorMessage(res));
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

export async function listPythonTools(): Promise<PythonToolEntry[]> {
  const res = await fetch(`${API_BASE}/python-tools`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.tools ?? [];
}

export async function createPythonTool(payload: PythonToolConfig): Promise<PythonToolEntry> {
  const res = await fetch(`${API_BASE}/python-tools`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.tool;
}

export async function updatePythonTool(id: string, payload: PythonToolConfig): Promise<PythonToolEntry> {
  const res = await fetch(`${API_BASE}/python-tools/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.tool;
}

export async function deletePythonTool(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/python-tools/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error(await errorMessage(res));
}

export async function reloadPythonTools(): Promise<PythonToolEntry[]> {
  const res = await fetch(`${API_BASE}/python-tools/reload`, { method: 'POST' });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.tools ?? [];
}

export async function validatePythonTool(payload: PythonToolConfig): Promise<PythonToolEntry> {
  const res = await fetch(`${API_BASE}/python-tools/validate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.tool;
}

export async function uploadPythonTool(
  file: File,
  payload: Pick<PythonToolConfig, 'id' | 'names' | 'enabled'>,
): Promise<PythonToolEntry> {
  const form = new FormData();
  form.append('file', file);
  form.append('id', payload.id);
  form.append('names', payload.names.join(','));
  form.append('enabled', String(payload.enabled));
  const res = await fetch(`${API_BASE}/python-tools/upload`, {
    method: 'POST',
    body: form,
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.tool;
}

export async function listModels(): Promise<{ models: ModelProvider[]; active_model_id: string }> {
  const res = await fetch(`${API_BASE}/models`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return {
    models: data.models ?? [],
    active_model_id: data.active_model_id ?? 'config',
  };
}

export async function createModelProvider(payload: ModelProviderInput): Promise<{ model: ModelProvider; active_model_id: string }> {
  const res = await fetch(`${API_BASE}/models`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  return await res.json();
}

export async function updateModelProvider(
  modelId: string,
  payload: ModelProviderInput,
): Promise<{ model: ModelProvider; active_model_id: string }> {
  const res = await fetch(`${API_BASE}/models/${encodeURIComponent(modelId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  return await res.json();
}

export async function deleteModelProvider(modelId: string): Promise<{ status: string; active_model_id: string }> {
  const res = await fetch(`${API_BASE}/models/${encodeURIComponent(modelId)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  return await res.json();
}

export async function activateModelProvider(modelId: string): Promise<{ model: ModelProvider; active_model_id: string }> {
  const res = await fetch(`${API_BASE}/models/${encodeURIComponent(modelId)}/activate`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  return await res.json();
}

export interface ApiRunState {
  run_id?: string | null;
  kind: 'tool' | 'dynamic_dag' | 'static_dag';
  status: string;
  internal_messages: Array<Record<string, unknown>>;
  dynamic_adjust?: boolean;
  dag?: Dag | null;
  trace?: RunTrace | null;
  pending_review?: ReviewEventPayload | null;
  spec_id?: string | null;
  workspace_path?: string | null;
}

interface ApiRunResult {
  output_text: string;
  state?: ApiRunState | null;
}

interface FinishedPayload {
  type: 'run.finished';
  result: ApiRunResult;
}

interface StreamEnvelope {
  type: string;
  data?: Record<string, unknown>;
  sequence?: number;
  run_id?: string | null;
}

interface StreamHandlers {
  onStarted?: (event: RunStartedStreamEvent) => void;
  onDag?: (dag: Dag) => void;
  onTrace?: (event: TraceLogEvent) => void;
  onCapability?: (event: CapabilityStreamEvent) => void;
  onReasoning?: (event: ResponseDeltaStreamEvent) => void;
  onContent?: (event: ResponseDeltaStreamEvent) => void;
  onRetry?: (event: ValidationFeedbackEvent) => void;
  onValidating?: (event: { type: 'validation.started'; message: string }) => void;
  onReview?: (review: ReviewEventPayload) => void;
  onDone?: (payload: FinishedPayload) => void;
  onError?: (message: string) => void;
}

interface StreamRequestOptions {
  signal?: AbortSignal;
}

export interface ChatStreamMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

export async function streamMessagesTask(
  messages: ChatStreamMessage[],
  target: 'auto' | 'tool' | 'dag',
  reviewLevel: ReviewLevel,
  handlers: StreamHandlers,
  capabilityScope?: ChatCapabilityScopePayload,
  dynamicAdjust?: boolean,
  options: StreamRequestOptions = {},
): Promise<void> {
  const body: Record<string, unknown> = {
    messages,
    target,
    review_level: reviewLevel,
  };
  if (capabilityScope) {
    Object.assign(body, chatScopeRequestFields(capabilityScope));
  }
  if (typeof dynamicAdjust === 'boolean') body.dynamic_adjust = dynamicAdjust;
  const response = await fetch(`${API_BASE}/messages/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: options.signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage(response));
  }

  await readStream(response, handlers);
}

export async function streamTask(
  message: string,
  target: 'auto' | 'tool' | 'dag',
  reviewLevel: ReviewLevel,
  handlers: StreamHandlers,
  capabilityScope?: ChatCapabilityScopePayload,
  state?: ApiRunState | null,
  dynamicAdjust?: boolean,
  options: StreamRequestOptions = {},
): Promise<void> {
  const body: Record<string, unknown> = {
    messages: [{ role: 'user', content: message }],
    target,
    review_level: reviewLevel,
  };
  if (capabilityScope) {
    Object.assign(body, chatScopeRequestFields(capabilityScope));
  }
  if (state) body.state = state;
  if (typeof dynamicAdjust === 'boolean') body.dynamic_adjust = dynamicAdjust;
  const response = await fetch(`${API_BASE}/messages/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: options.signal,
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
  state?: ApiRunState | null,
  feedback?: string,
  options: StreamRequestOptions = {},
): Promise<void> {
  const normalizedFeedback = feedback?.trim();
  const response = await fetch(`${API_BASE}/messages/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      review_id: reviewId,
      dag: approved ? dag : null,
      approved,
      review_level: reviewLevel,
      state,
      ...(normalizedFeedback ? { feedback: normalizedFeedback } : {}),
    }),
    signal: options.signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage(response));
  }
  await readStream(response, handlers);
}

export async function runDagStream(
  specId: string,
  handlers: StreamHandlers,
  options: {
    workspaceRoot?: string;
    input?: unknown;
  } = {},
): Promise<void> {
  const payload: Record<string, unknown> = {};
  if (options.workspaceRoot?.trim()) payload.workspace_root = options.workspaceRoot.trim();
  if (Object.prototype.hasOwnProperty.call(options, 'input')) payload.graph_input = options.input;
  const body = Object.keys(payload).length ? JSON.stringify(payload) : undefined;
  const response = await fetch(`${API_BASE}/dags/${encodeURIComponent(specId)}/run/stream`, {
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
      const event = JSON.parse(line.slice(6)) as StreamEnvelope;
      const data = isRecord(event.data) ? event.data : {};
      if (event.type === 'run.started') handlers.onStarted?.(runStartedPayload(data));
      if (event.type === 'dag.updated' && data.dag) handlers.onDag?.(data.dag as Dag);
      if (event.type === 'trace.updated') emitTraceSnapshot(data.trace as RunTrace | undefined, handlers.onTrace, seenTraceIds);
      if (event.type === 'capability.call.started') {
        handlers.onCapability?.({
          type: 'capability.call.started',
          invocation_id: String(data.invocation_id ?? ''),
          capability_id: String(data.capability_id ?? ''),
          arguments: isRecord(data.arguments) ? data.arguments : {},
          ...capabilityContext(data),
        });
      }
      if (event.type === 'capability.call.completed' || event.type === 'capability.call.failed') {
        handlers.onCapability?.({
          type: event.type,
          invocation_id: String(data.invocation_id ?? ''),
          capability_id: String(data.capability_id ?? ''),
          content: String(data.content ?? ''),
          ...capabilityContext(data),
        });
      }
      if (event.type === 'response.reasoning.delta') handlers.onReasoning?.(responseDeltaPayload(data));
      if (event.type === 'response.content.delta') handlers.onContent?.(responseDeltaPayload(data));
      if (event.type === 'validation.retry') {
        handlers.onRetry?.({
          type: 'validation.retry',
          summary: String(data.summary ?? ''),
          issues: Array.isArray(data.issues) ? data.issues as ValidationFeedbackEvent['issues'] : [],
          reason: String(data.reason ?? ''),
        });
      }
      if (event.type === 'validation.passed') {
        handlers.onRetry?.({
          type: 'validation.passed',
          passed: true,
          summary: String(data.summary ?? ''),
          issues: Array.isArray(data.issues) ? data.issues as ValidationFeedbackEvent['issues'] : [],
        });
      }
      if (event.type === 'validation.started') {
        handlers.onValidating?.({ type: 'validation.started', message: String(data.message ?? '') });
      }
      if (event.type === 'review.required') {
        handlers.onReview?.(reviewPayload(data));
      }
      if (event.type === 'run.finished' && data.result) {
        const result = data.result as ApiRunResult;
        handlers.onDone?.({ type: 'run.finished', result });
      }
      if (event.type === 'run.failed') handlers.onError?.(String(data.message ?? 'Run failed.'));
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function reviewPayload(data: Record<string, unknown>): ReviewEventPayload {
  const payload: ReviewEventPayload = {
    review_id: String(data.review_id ?? ''),
    kind: String(data.kind ?? 'initial_dag') as ReviewEventPayload['kind'],
    message: String(data.message ?? ''),
  };
  if (isRecord(data.proposed_dag)) {
    payload.proposed_dag = data.proposed_dag as unknown as Dag;
  }
  if (isRecord(data.capability_call)) {
    payload.capability_call = {
      invocation_id: String(data.capability_call.invocation_id ?? ''),
      capability_id: String(data.capability_call.capability_id ?? ''),
      arguments: isRecord(data.capability_call.arguments) ? data.capability_call.arguments : {},
    };
  }
  if (isRecord(data.payload)) {
    payload.payload = data.payload;
  }
  return payload;
}

function capabilityContext(data: Record<string, unknown>) {
  return {
    run_id: nullableString(data.run_id),
    dag_id: nullableString(data.dag_id),
    node_id: nullableString(data.node_id),
    parent_capability_id: nullableString(data.parent_capability_id),
  };
}

function nullableString(value: unknown): string | null {
  return value === null || value === undefined ? null : String(value);
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
  const execution = node.capability_execution;
  return {
    ...node.ref,
    input: execution?.invocation.arguments ?? node.input,
    output: node.output ?? execution?.result?.content,
    error: node.error?.message,
    result: execution?.result,
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
  if (status === 'awaiting_review') return 'awaiting_review';
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
  state?: ApiRunState | null,
  feedback?: string,
  options: StreamRequestOptions = {},
): Promise<void> {
  const normalizedFeedback = feedback?.trim();
  const response = await fetch(`${API_BASE}/messages/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      review_id: reviewId,
      approved,
      state,
      ...(normalizedFeedback ? { feedback: normalizedFeedback } : {}),
    }),
    signal: options.signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage((response as unknown) as Response));
  }
  await readStream(response, handlers);
}
