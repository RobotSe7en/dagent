export interface PythonToolDiscoveryState {
  requestId: number;
  sourceKey: string;
  namesEditedAt: number;
}

export interface PythonToolDiscoveryRequest {
  requestId: number;
  sourceKey: string;
  namesEditedAtStart: number;
}

export function pythonToolDiscoverySourceKey(source: 'path' | 'managed', value: string): string {
  return `${source}:${value}`;
}

export function shouldApplyPythonToolDiscoveryResult(
  current: PythonToolDiscoveryState,
  request: PythonToolDiscoveryRequest,
): boolean {
  return current.requestId === request.requestId
    && current.sourceKey === request.sourceKey
    && current.namesEditedAt === request.namesEditedAtStart;
}
