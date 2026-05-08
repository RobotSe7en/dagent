import type { Dag, ReviewLevel, ToolReview, ToolStreamEvent, TraceEvent } from './types';

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api';

export async function resetSession(): Promise<void> {
  await fetch(`${API_BASE}/session/reset`, { method: 'POST' });
}

interface DonePayload {
  status?: string;
  task_id: string | null;
  dag: Dag | null;
  pending_review?: { kind: string; message: string } | null;
  pending_tool_review?: ToolReview | null;
  message_markdown: string;
}

interface BackendTrace {
  event_id: string;
  event_type: string;
  dag_id: string;
  node_id?: string | null;
  payload?: Record<string, unknown>;
  created_at: string;
}

export async function streamTask(
  message: string,
  mode: 'auto' | 'direct' | 'dag',
  reviewLevel: ReviewLevel,
  handlers: {
    onStatus?: (status: string) => void;
    onDag?: (dag: Dag) => void;
    onTrace?: (event: TraceEvent) => void;
    onTool?: (event: ToolStreamEvent) => void;
    onToolReview?: (review: ToolReview) => void;
    onToken?: (content: string) => void;
    onDone?: (payload: DonePayload) => void;
    onError?: (message: string) => void;
  },
): Promise<void> {
  const response = await fetch(`${API_BASE}/messages/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, mode, review_level: reviewLevel }),
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage(response));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

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
      if (event.type === 'trace') handlers.onTrace?.(mapTrace(event.event));
      if (event.type === 'tool_call' || event.type === 'tool_result' || event.type === 'tool_error') {
        handlers.onTool?.(event);
      }
      if (event.type === 'tool_review') handlers.onToolReview?.(event.tool_review);
      if (event.type === 'token') handlers.onToken?.(event.content);
      if (event.type === 'done') handlers.onDone?.(event);
      if (event.type === 'error') handlers.onError?.(event.message);
    }
  }
}

export async function resumeDag(
  taskId: string,
  dag: Dag,
  reviewLevel: ReviewLevel,
  handlers: {
    onStatus?: (status: string) => void;
    onDag?: (dag: Dag) => void;
    onTrace?: (event: TraceEvent) => void;
    onTool?: (event: ToolStreamEvent) => void;
    onToolReview?: (review: ToolReview) => void;
    onToken?: (content: string) => void;
    onDone?: (payload: DonePayload) => void;
    onError?: (message: string) => void;
  },
): Promise<void> {
  const response = await fetch(`${API_BASE}/messages/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_id: taskId, dag, review_level: reviewLevel }),
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage(response));
  }
  await readStream(response, handlers);
}

export async function resumeToolReview(
  approved: boolean,
  reviewLevel: ReviewLevel,
  handlers: {
    onStatus?: (status: string) => void;
    onDag?: (dag: Dag) => void;
    onTrace?: (event: TraceEvent) => void;
    onTool?: (event: ToolStreamEvent) => void;
    onToolReview?: (review: ToolReview) => void;
    onToken?: (content: string) => void;
    onDone?: (payload: DonePayload) => void;
    onError?: (message: string) => void;
  },
): Promise<void> {
  const response = await fetch(`${API_BASE}/messages/resume-tool`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved, review_level: reviewLevel }),
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage(response));
  }
  await readStream(response, handlers);
}

async function readStream(
  response: Response,
  handlers: {
    onStatus?: (status: string) => void;
    onDag?: (dag: Dag) => void;
    onTrace?: (event: TraceEvent) => void;
    onTool?: (event: ToolStreamEvent) => void;
    onToolReview?: (review: ToolReview) => void;
    onToken?: (content: string) => void;
    onDone?: (payload: DonePayload) => void;
    onError?: (message: string) => void;
  },
) {
  const reader = response.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buffer = '';

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
      if (event.type === 'trace') handlers.onTrace?.(mapTrace(event.event));
      if (event.type === 'tool_call' || event.type === 'tool_result' || event.type === 'tool_error') {
        handlers.onTool?.(event);
      }
      if (event.type === 'tool_review') handlers.onToolReview?.(event.tool_review);
      if (event.type === 'token') handlers.onToken?.(event.content);
      if (event.type === 'done') handlers.onDone?.(event);
      if (event.type === 'error') handlers.onError?.(event.message);
    }
  }
}

export function mapTrace(event: BackendTrace): TraceEvent {
  const status = event.event_type.endsWith('failed')
    ? 'failed'
    : event.event_type.endsWith('started') || event.event_type.endsWith('called')
      ? 'running'
      : 'completed';
  const type = event.event_type.startsWith('dag')
    ? 'dag'
    : event.event_type.startsWith('node')
      ? 'node'
      : event.event_type.startsWith('tool')
        ? 'tool'
        : 'model';
  return {
    ...event,
    id: event.event_id,
    type,
    label: event.node_id ? `${event.event_type} · ${event.node_id}` : event.event_type,
    detail: traceDetail(event),
    status,
    timestamp: new Date(event.created_at).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }),
  };
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const payload = await response.json();
    return payload.detail ?? response.statusText;
  } catch {
    return response.statusText;
  }
}

function traceDetail(event: BackendTrace): string {
  const payload = event.payload ?? {};
  if (typeof payload.error === 'string') return payload.error;
  if (typeof payload.name === 'string') {
    const suffix = typeof payload.content === 'string' ? `: ${clip(payload.content)}` : '';
    return `${payload.name}${suffix}`;
  }
  if (typeof payload.stop_reason === 'string') {
    return `stop_reason=${payload.stop_reason}, steps=${payload.steps ?? '?'}`;
  }
  if (Object.keys(payload).length === 0) return event.dag_id;
  return clip(JSON.stringify(payload));
}

function clip(value: string): string {
  return value.length > 180 ? `${value.slice(0, 177)}...` : value;
}
