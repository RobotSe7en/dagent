import type {
  CapabilityStreamEvent,
  Dag,
  TraceLogEvent,
  ValidationFeedbackEvent,
} from './types';

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  kind?: 'text';
  capabilityEvents?: CapabilityStreamEvent[];
  timeline?: MessageTimelineItem[];
  dagSnapshot?: Dag;
  traceSnapshot?: TraceLogEvent[];
}

export type MessageTimelineItem =
  | { type: 'text'; content: string }
  | { type: 'reasoning'; content: string; closed: boolean }
  | { type: 'dag'; dag: Dag }
  | { type: 'capability'; event: CapabilityStreamEvent; result?: CapabilityStreamEvent }
  | { type: 'validation'; event: ValidationFeedbackEvent }
  | { type: 'validating' };

export function appendTextTimeline(
  timeline: MessageTimelineItem[] | undefined,
  content: string,
): MessageTimelineItem[] {
  if (!content) return timeline ?? [];
  const items = [...(timeline ?? [])];
  const last = items[items.length - 1];
  if (last?.type === 'text') {
    items[items.length - 1] = { type: 'text', content: `${last.content}${content}` };
    return items;
  }
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.type === 'text' && hasUnclosedThink(item.content)) {
      items[i] = { type: 'text', content: `${item.content}${content}` };
      return items;
    }
  }
  items.push({ type: 'text', content });
  return items;
}

export function appendReasoningTimeline(
  timeline: MessageTimelineItem[] | undefined,
  content: string,
): MessageTimelineItem[] {
  if (!content) return timeline ?? [];
  const items = [...(timeline ?? [])];
  const last = items[items.length - 1];
  if (last?.type === 'reasoning' && !last.closed) {
    items[items.length - 1] = {
      ...last,
      content: `${last.content}${content}`,
    };
    return items;
  }
  items.push({ type: 'reasoning', content, closed: false });
  return items;
}

export function closeReasoningTimeline(
  timeline: MessageTimelineItem[] | undefined,
): MessageTimelineItem[] {
  if (!timeline?.length) return timeline ?? [];
  for (let i = timeline.length - 1; i >= 0; i--) {
    const item = timeline[i];
    if (item.type === 'reasoning' && !item.closed) {
      const items = [...timeline];
      items[i] = { ...item, closed: true };
      return items;
    }
  }
  return timeline;
}

export function upsertDagTimeline(
  timeline: MessageTimelineItem[] | undefined,
  dag: Dag,
): MessageTimelineItem[] {
  const items = [...(timeline ?? [])];
  const dagKey = dag.task_id || dag.dag_id;
  const existingIndex = items.findIndex(
    (item) => item.type === 'dag' && (item.dag.task_id || item.dag.dag_id) === dagKey && item.dag.version === dag.version,
  );
  if (existingIndex !== -1) {
    items[existingIndex] = { type: 'dag', dag };
    return items;
  }
  const last = items[items.length - 1];
  if (last?.type === 'dag' && (last.dag.task_id || last.dag.dag_id) === dagKey && last.dag.version === dag.version) {
    items[items.length - 1] = { type: 'dag', dag };
  } else {
    items.push({ type: 'dag', dag });
  }
  return items;
}

function hasUnclosedThink(content: string): boolean {
  return (content.match(/<think>/g) || []).length > (content.match(/<\/think>/g) || []).length;
}
