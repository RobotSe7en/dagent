import type { Dag, TraceEvent } from './types';

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000';

interface BackendTrace {
  event_id: string;
  event_type: string;
  dag_id: string;
  node_id?: string | null;
  payload?: Record<string, unknown>;
  created_at: string;
}

export interface ExecuteResponse {
  run_id: string;
  dag: Dag;
  result: {
    dag_id: string;
    completed: boolean;
    node_results: Record<
      string,
      {
        node_id: string;
        final_response: string;
        completed: boolean;
        stop_reason: string;
        steps: number;
      }
    >;
    traces: BackendTrace[];
  };
  message_markdown: string;
}

export async function approveDag(taskId: string): Promise<Dag> {
  const payload = await apiFetch<{ dag: Dag }>(`/dags/${taskId}/approve`, { method: 'POST' });
  return payload.dag;
}

export async function saveDag(taskId: string, dag: Dag): Promise<Dag> {
  const payload = await apiFetch<{ dag: Dag }>(`/dags/${taskId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dag }),
  });
  return payload.dag;
}

export async function executeDag(taskId: string): Promise<ExecuteResponse> {
  return apiFetch<ExecuteResponse>(`/dags/${taskId}/execute`, { method: 'POST' });
}

export async function streamTask(
  message: string,
  handlers: {
    onStatus?: (status: string) => void;
    onDag?: (dag: Dag) => void;
    onToken?: (content: string) => void;
    onDone?: (payload: { task_id: string; dag: Dag; message_markdown: string }) => void;
    onError?: (message: string) => void;
  },
): Promise<void> {
  const response = await fetch(`${API_BASE}/tasks/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
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

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return response.json() as Promise<T>;
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
