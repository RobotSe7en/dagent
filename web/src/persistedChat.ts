import type { ApiRunEvent, ApiRunResult, ApiRunState, StreamEnvelope } from './api';
import { dispatchStreamEnvelope, mapRunTrace } from './api';
import type {
  CapabilityStreamEvent,
  TraceLogEvent,
} from './types';
import {
  appendReasoningTimeline,
  appendTextTimeline,
  appendValidatingTimeline,
  appendValidationTimeline,
  closeReasoningTimeline,
  upsertDagTimeline,
  type ChatMessage,
  type MessageTimelineItem,
} from './chatTimeline';

export function finishedRunResultFromEvents(events: ApiRunEvent[]): ApiRunResult | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    const envelope = streamEnvelope(event);
    if (event.event_type !== 'run.finished' && envelope?.type !== 'run.finished') continue;
    const data = recordValue(envelope?.data);
    const result = data ? recordValue(data.result) : null;
    if (!result) continue;
    return {
      output_text: typeof result.output_text === 'string' ? result.output_text : '',
      state: recordValue(result.state) ? result.state as ApiRunState : null,
    };
  }
  return null;
}

export function chatMessagesFromPersistedRunEvents(
  events: ApiRunEvent[],
  result: ApiRunResult | null,
): ChatMessage[] {
  if (!result) return [];
  const traceSnapshot = result.state?.trace ? mapRunTrace(result.state.trace) : [];
  return messagesFromPersistedRunResult(result, traceSnapshot, events);
}

export function messagesFromPersistedRunResult(
  result: ApiRunResult,
  traceSnapshot: TraceLogEvent[],
  events: ApiRunEvent[] = [],
): ChatMessage[] {
  const state = result.state ?? null;
  const dagSnapshot = state?.dag ?? undefined;
  const reviewMessage = state?.pending_review?.message?.trim() ?? '';
  const output = result.output_text.trim();
  const fallbackContent = output || reviewMessage;
  const messages: ChatMessage[] = [];

  for (const message of state?.internal_messages ?? []) {
    const role = message.role;
    if (role !== 'user' && role !== 'assistant') continue;
    const content = visibleChatContentFromInternalMessage(message).trim();
    if (!content) continue;
    appendPersistedChatMessage(messages, role, content);
  }

  const assistantIndex = lastAssistantMessageIndex(messages);
  const lastAssistantContent = assistantIndex === -1 ? '' : messages[assistantIndex].content.trim();
  const eventTimeline = timelineFromPersistedRunEvents(events, fallbackContent || lastAssistantContent);
  const replayedText = textContentFromTimeline(eventTimeline);

  if (
    fallbackContent
    && !eventTimeline.length
    && !messages.some((message) => message.role === 'assistant' && message.content.trim() === fallbackContent)
  ) {
    appendPersistedChatMessage(messages, 'assistant', fallbackContent);
  }

  const nextAssistantIndex = lastAssistantMessageIndex(messages);
  if (nextAssistantIndex !== -1) {
    const message = messages[nextAssistantIndex];
    let timeline = eventTimeline.length ? eventTimeline : message.timeline ?? [];
    if (dagSnapshot) timeline = upsertDagTimeline(timeline, dagSnapshot);
    messages[nextAssistantIndex] = {
      ...message,
      content: replayedText || message.content || fallbackContent,
      timeline,
      dagSnapshot,
      traceSnapshot,
    };
  } else if (dagSnapshot || eventTimeline.length) {
    let timeline = eventTimeline;
    if (dagSnapshot) timeline = upsertDagTimeline(timeline, dagSnapshot);
    messages.push({
      role: 'assistant',
      kind: 'text',
      content: replayedText || fallbackContent,
      timeline,
      dagSnapshot,
      traceSnapshot,
    });
  }
  return messages;
}

function appendPersistedChatMessage(
  messages: ChatMessage[],
  role: 'user' | 'assistant',
  content: string,
): void {
  const last = messages[messages.length - 1];
  if (role === 'assistant' && last?.role === 'assistant') {
    messages[messages.length - 1] = {
      ...last,
      content: last.content ? `${last.content}\n\n${content}` : content,
      timeline: [...(last.timeline ?? []), { type: 'text', content }],
    };
    return;
  }
  messages.push({
    role,
    kind: 'text',
    content,
    timeline: [{ type: 'text', content }],
  });
}

function timelineFromPersistedRunEvents(
  events: ApiRunEvent[],
  fallbackContent: string,
): MessageTimelineItem[] {
  let timeline: MessageTimelineItem[] = [];
  const orderedEvents = [...events].sort((left, right) => left.event_id - right.event_id);

  for (const event of orderedEvents) {
    const envelope = streamEnvelope(event);
    if (!envelope) continue;
    dispatchStreamEnvelope(envelope, {
      onReasoning: (item) => {
        timeline = appendReasoningTimeline(timeline, item.delta);
      },
      onContent: (item) => {
        timeline = appendTextTimeline(closeReasoningTimeline(timeline), item.delta);
      },
      onDag: (dag) => {
        timeline = upsertDagTimeline(timeline, dag);
      },
      onCapability: (item) => {
        if (item.type === 'capability.call.completed' && item.content?.startsWith('[PENDING_REVIEW]')) return;
        timeline = appendCapabilityTimeline(timeline, item);
      },
      onValidating: () => {
        timeline = appendValidatingTimeline(timeline);
      },
      onRetry: (item) => {
        timeline = appendValidationTimeline(timeline, item);
      },
    });
  }

  if (fallbackContent && !timeline.some((item) => item.type === 'text' || item.type === 'reasoning')) {
    timeline = appendTextTimeline(closeReasoningTimeline(timeline), fallbackContent);
  }
  return timeline;
}

function appendCapabilityTimeline(
  timeline: MessageTimelineItem[],
  event: CapabilityStreamEvent,
): MessageTimelineItem[] {
  const items = [...timeline];
  if (event.type === 'capability.call.completed' || event.type === 'capability.call.failed') {
    const existingIndex = findMatchingCapabilityCall(items, event.invocation_id);
    if (existingIndex !== -1) {
      const item = items[existingIndex];
      if (item.type === 'capability') {
        items[existingIndex] = { ...item, result: event };
      }
      return items;
    }
  }
  items.push({ type: 'capability', event });
  return items;
}

function findMatchingCapabilityCall(timeline: MessageTimelineItem[], invocationId: string): number {
  for (let i = timeline.length - 1; i >= 0; i -= 1) {
    const item = timeline[i];
    if (item.type === 'capability' && item.event.invocation_id === invocationId && item.event.type === 'capability.call.started') {
      return i;
    }
  }
  return -1;
}

function lastAssistantMessageIndex(messages: ChatMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'assistant') return index;
  }
  return -1;
}

function textContentFromTimeline(timeline: MessageTimelineItem[]): string {
  return timeline
    .flatMap((item) => item.type === 'text' ? [item.content] : [])
    .join('');
}

function visibleChatContentFromInternalMessage(message: Record<string, unknown>): string {
  const content = message.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map((item) => {
      if (typeof item === 'string') return item;
      const record = recordValue(item);
      if (!record) return '';
      if (typeof record.text === 'string') return record.text;
      if (typeof record.content === 'string') return record.content;
      return '';
    }).filter(Boolean).join('\n');
  }
  return '';
}

function streamEnvelope(event: ApiRunEvent): StreamEnvelope | null {
  return recordValue(event.payload) ? event.payload : null;
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}
