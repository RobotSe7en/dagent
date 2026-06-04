type StreamEvent = Record<string, any>;

function isRecord(value: unknown): value is StreamEvent {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function normalizeStreamEvent(event: StreamEvent): any {
  const data = isRecord(event.data) ? event.data : {};
  const eventType = event.type === 'status' && typeof data.type === 'string'
    ? data.type
    : event.type;
  return {
    ...data,
    ...event,
    type: eventType,
  };
}
