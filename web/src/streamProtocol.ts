export type RunKind = 'tool' | 'dynamic_dag' | 'static_dag';

export interface RunStartedStreamEvent {
  kind: RunKind;
}

export interface ResponseDeltaStreamEvent {
  delta: string;
  response_id: string;
  model_step: number | null;
  run_id: string | null;
  dag_id: string | null;
  node_id: string | null;
  parent_capability_id: string | null;
}

export function runStartedPayload(data: Record<string, unknown>): RunStartedStreamEvent {
  return { kind: runKind(data.kind) };
}

export function responseDeltaPayload(data: Record<string, unknown>): ResponseDeltaStreamEvent {
  return {
    delta: String(data.delta ?? ''),
    response_id: requiredString(data.response_id, 'response_id'),
    model_step: nullableNumber(data.model_step),
    run_id: nullableString(data.run_id),
    dag_id: nullableString(data.dag_id),
    node_id: nullableString(data.node_id),
    parent_capability_id: nullableString(data.parent_capability_id),
  };
}

export function shouldStreamChatContent(
  requestedTarget: 'auto' | 'tool' | 'dag',
  resolvedKind: RunKind | null,
): boolean {
  if (resolvedKind !== null) return resolvedKind === 'tool';
  return requestedTarget === 'tool';
}

function runKind(value: unknown): RunKind {
  if (value === 'tool' || value === 'dynamic_dag' || value === 'static_dag') return value;
  throw new Error(`Unsupported run kind: ${String(value)}`);
}

function nullableString(value: unknown): string | null {
  return value === null || value === undefined ? null : String(value);
}

function requiredString(value: unknown, field: string): string {
  if (value === null || value === undefined || value === '') {
    throw new Error(`Missing ${field}`);
  }
  return String(value);
}

function nullableNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}
