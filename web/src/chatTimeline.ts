import type {
  CapabilityStreamEvent,
  Dag,
  ReviewEventPayload,
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

export function appendValidatingTimeline(
  timeline: MessageTimelineItem[] | undefined,
): MessageTimelineItem[] {
  const items = [...(timeline ?? [])];
  const last = items[items.length - 1];
  if (last?.type === 'validating') return items;
  items.push({ type: 'validating' });
  return items;
}

export function appendValidationTimeline(
  timeline: MessageTimelineItem[] | undefined,
  event: ValidationFeedbackEvent,
): MessageTimelineItem[] {
  const items = [...(timeline ?? [])];
  const last = items[items.length - 1];
  if (last?.type === 'validating') {
    items[items.length - 1] = { type: 'validation', event };
    return items;
  }
  items.push({ type: 'validation', event });
  return items;
}

export function appendCapabilityReviewDecisionTimeline(
  timeline: MessageTimelineItem[] | undefined,
  review: ReviewEventPayload,
  approved: boolean,
  feedback?: string,
): MessageTimelineItem[] {
  if (approved || review.kind !== 'capability_review' || !review.capability_call) return timeline ?? [];
  const items = [...(timeline ?? [])];
  const invocation = review.capability_call;
  const content = feedback?.trim()
    ? `人工审核已拒绝。\n\n反馈：${feedback.trim()}`
    : '人工审核已拒绝。';
  const result: CapabilityStreamEvent = {
    type: 'capability.call.failed',
    invocation_id: invocation.invocation_id,
    capability_id: invocation.capability_id,
    arguments: invocation.arguments,
    content,
  };
  const existingIndex = items.findIndex(
    (item) => item.type === 'capability' && item.event.invocation_id === invocation.invocation_id,
  );

  if (existingIndex !== -1) {
    const existing = items[existingIndex];
    if (existing.type === 'capability') {
      items[existingIndex] = { ...existing, result };
    }
    return items;
  }

  items.push({ type: 'capability', event: result, result });
  return items;
}

export function upsertDagTimeline(
  timeline: MessageTimelineItem[] | undefined,
  dag: Dag,
): MessageTimelineItem[] {
  const items = [...(timeline ?? [])];
  const dagKey = dag.task_id || dag.dag_id;
  const existingIndex = items.findIndex(
    (item) => item.type === 'dag' && (item.dag.task_id || item.dag.dag_id) === dagKey,
  );
  if (existingIndex !== -1) {
    const existing = items[existingIndex];
    if (existing.type === 'dag' && existing.dag.status === 'rejected' && dag.status === 'running') {
      return items;
    }
    items[existingIndex] = { type: 'dag', dag };
    return items;
  }
  const last = items[items.length - 1];
  if (last?.type === 'dag' && last.dag.status === 'rejected' && dag.status === 'running') {
    return items;
  }
  if (last?.type === 'dag' && (last.dag.task_id || last.dag.dag_id) === dagKey && last.dag.version === dag.version) {
    items[items.length - 1] = { type: 'dag', dag };
  } else {
    items.push({ type: 'dag', dag });
  }
  return items;
}

export function upsertDagMessageTimeline(
  messages: ChatMessage[],
  dag: Dag,
): ChatMessage[] {
  const dagKey = dag.task_id || dag.dag_id;
  const existingMessageIndex = messages.findIndex((message) =>
    message.role === 'assistant'
    && message.timeline?.some((item) => item.type === 'dag' && (item.dag.task_id || item.dag.dag_id) === dagKey),
  );

  if (existingMessageIndex !== -1) {
    return messages.map((message, index) => index === existingMessageIndex
      ? { ...message, timeline: upsertDagTimeline(message.timeline, dag) }
      : message);
  }

  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === 'assistant' && (message.kind ?? 'text') === 'text') {
      return messages.map((item, itemIndex) => itemIndex === index
        ? { ...item, timeline: upsertDagTimeline(item.timeline, dag) }
        : item);
    }
  }

  return [...messages, { role: 'assistant', kind: 'text', content: '', timeline: [{ type: 'dag', dag }] }];
}

function hasUnclosedThink(content: string): boolean {
  return (content.match(/<think>/g) || []).length > (content.match(/<\/think>/g) || []).length;
}
