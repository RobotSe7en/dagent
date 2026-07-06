import type { ApiRunEvent, ApiRunResult, ApiRunState, StreamEnvelope } from './api';
import { dispatchStreamEnvelope, mapRunTrace } from './api';
import type {
  CapabilityStreamEvent,
  ReviewEventPayload,
  TraceLogEvent,
} from './types';
import {
  appendCapabilityReviewDecisionTimeline,
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
  if (!result) return partialMessagesFromPersistedRunEvents(events);
  const traceSnapshot = result.state?.trace ? mapRunTrace(result.state.trace) : [];
  return messagesFromPersistedRunResult(result, traceSnapshot, events);
}

function partialMessagesFromPersistedRunEvents(events: ApiRunEvent[]): ChatMessage[] {
  const fallbackContent = fallbackContentFromPersistedRunEvents(events);
  const timeline = timelineFromPersistedRunEvents(events, fallbackContent);
  if (!timeline.length) return [];
  return [{
    role: 'assistant',
    kind: 'text',
    content: textContentFromTimeline(timeline) || fallbackContent,
    timeline,
  }];
}

export function messagesFromPersistedRunResult(
  result: ApiRunResult,
  traceSnapshot: TraceLogEvent[],
  events: ApiRunEvent[] = [],
): ChatMessage[] {
  const state = result.state ?? null;
  if (state?.kind === 'dynamic_dag') {
    return dynamicDagMessagesFromPersistedRunResult(result, traceSnapshot, events);
  }
  return chatMessagesFromPersistedRunResult(result, traceSnapshot, events);
}

function chatMessagesFromPersistedRunResult(
  result: ApiRunResult,
  traceSnapshot: TraceLogEvent[],
  events: ApiRunEvent[],
): ChatMessage[] {
  const state = result.state ?? null;
  const dagSnapshot = state?.dag ?? undefined;
  const reviewMessage = state?.pending_review?.message?.trim() ?? '';
  const output = result.output_text.trim();
  const fallbackContent = output || reviewMessage;
  const messages: ChatMessage[] = [];

  for (const message of inputMessagesFromRunState(state)) {
    const role = message.role;
    if (role !== 'user' && role !== 'assistant') continue;
    const content = visibleInputChatContentFromInternalMessage(message).trim();
    if (!content) continue;
    appendPersistedChatMessage(messages, role, content);
  }

  const groups = persistedRunEventGroups(events);
  if (groups.length) {
    appendPersistedChatEventGroups(messages, groups, result, traceSnapshot);
    return messages;
  }

  if (
    fallbackContent
    && !messages.some((message) => message.role === 'assistant' && message.content.trim() === fallbackContent)
  ) {
    appendPersistedChatMessage(messages, 'assistant', fallbackContent);
  }

  const nextAssistantIndex = lastAssistantMessageIndex(messages);
  if (nextAssistantIndex !== -1) {
    const message = messages[nextAssistantIndex];
    let timeline = message.timeline ?? [];
    if (dagSnapshot) timeline = upsertDagTimeline(timeline, dagSnapshot);
    messages[nextAssistantIndex] = {
      ...message,
      content: message.content || fallbackContent,
      timeline,
      dagSnapshot,
      traceSnapshot,
    };
  } else if (dagSnapshot) {
    let timeline: MessageTimelineItem[] = [];
    if (dagSnapshot) timeline = upsertDagTimeline(timeline, dagSnapshot);
    messages.push({
      role: 'assistant',
      kind: 'text',
      content: fallbackContent,
      timeline,
      dagSnapshot,
      traceSnapshot,
    });
  }
  return messages;
}

function appendPersistedChatEventGroups(
  messages: ChatMessage[],
  groups: ApiRunEvent[][],
  finalResult: ApiRunResult,
  finalTraceSnapshot: TraceLogEvent[],
): void {
  const finalGroup = groups[groups.length - 1];
  let assistantCursor = 0;
  let previousAssistantIndex = -1;
  let previousInputBoundary: number | null = null;
  for (const group of groups) {
    const segmentResult = finishedRunResultFromEvents(group) ?? (group === finalGroup ? finalResult : null);
    const segmentState = segmentResult?.state ?? (group === finalGroup ? finalResult.state ?? null : null);
    const segmentTrace = segmentResult === finalResult || segmentState === finalResult.state
      ? finalTraceSnapshot
      : segmentState?.trace
        ? mapRunTrace(segmentState.trace)
        : [];
    const dagSnapshot = segmentState?.dag ?? (group === finalGroup ? finalResult.state?.dag ?? undefined : undefined);
    const reviewMessage = segmentState?.pending_review?.message?.trim() ?? '';
    const output = segmentResult?.output_text.trim() ?? (group === finalGroup ? finalResult.output_text.trim() : '');
    const fallbackContent = output || reviewMessage || fallbackContentFromPersistedRunEvents(group);
    let timeline = timelineFromPersistedRunEvents(group, fallbackContent, output || fallbackContent);
    if (dagSnapshot) timeline = upsertDagTimeline(timeline, dagSnapshot);
    const replayedText = textContentFromTimeline(timeline);
    const content = replayedText || fallbackContent;
    if (!content && !timeline.length && !dagSnapshot) continue;
    const segmentMessage: ChatMessage = {
      role: 'assistant',
      kind: 'text',
      content,
      timeline,
      dagSnapshot,
      traceSnapshot: segmentTrace,
    };
    const inputBoundary = inputBoundaryFromRunState(segmentState);

    if (
      previousAssistantIndex !== -1
      && inputBoundary !== null
      && inputBoundary === previousInputBoundary
    ) {
      messages[previousAssistantIndex] = mergePersistedAssistantMessage(
        messages[previousAssistantIndex],
        segmentMessage,
      );
      continue;
    }

    const assistantIndex = nextAssistantMessageIndex(messages, assistantCursor);
    if (assistantIndex !== -1) {
      const message = messages[assistantIndex];
      const nextTimeline = timeline.length ? timeline : message.timeline ?? [];
      messages[assistantIndex] = {
        ...message,
        content: content || message.content,
        timeline: nextTimeline,
        dagSnapshot: dagSnapshot ?? message.dagSnapshot,
        traceSnapshot: segmentTrace,
      };
      assistantCursor = assistantIndex + 1;
      previousAssistantIndex = assistantIndex;
      previousInputBoundary = inputBoundary;
      continue;
    }

    messages.push(segmentMessage);
    previousAssistantIndex = messages.length - 1;
    previousInputBoundary = inputBoundary;
    assistantCursor = messages.length;
  }
}

function dynamicDagMessagesFromPersistedRunResult(
  result: ApiRunResult,
  traceSnapshot: TraceLogEvent[],
  events: ApiRunEvent[],
): ChatMessage[] {
  const groups = persistedRunEventGroups(events);
  if (!groups.length) {
    return dynamicDagMessagesFromResultSegment(result, traceSnapshot, events, new Set());
  }

  const messages: ChatMessage[] = [];
  const seenUserRequests = new Set<string>();
  const finalGroup = groups[groups.length - 1];
  for (const group of groups) {
    const segmentResult = finishedRunResultFromEvents(group) ?? (group === finalGroup ? result : null);
    if (!segmentResult) {
      appendDynamicDagSegmentMessages(messages, partialMessagesFromPersistedRunEvents(group));
      continue;
    }
    const segmentTrace = segmentResult === result || segmentResult.state === result.state
      ? traceSnapshot
      : segmentResult.state?.trace
        ? mapRunTrace(segmentResult.state.trace)
        : [];
    appendDynamicDagSegmentMessages(
      messages,
      dynamicDagMessagesFromResultSegment(segmentResult, segmentTrace, group, seenUserRequests),
    );
  }
  return messages;
}

function dynamicDagMessagesFromResultSegment(
  result: ApiRunResult,
  traceSnapshot: TraceLogEvent[],
  events: ApiRunEvent[],
  seenUserRequests: Set<string>,
): ChatMessage[] {
  const state = result.state ?? null;
  const dagSnapshot = state?.dag ?? undefined;
  const reviewMessage = state?.pending_review?.message?.trim() ?? '';
  const output = result.output_text.trim();
  const fallbackContent = output || (state?.status === 'awaiting_review' && dagSnapshot ? '' : reviewMessage);
  const messages: ChatMessage[] = [];
  const userRequest = dynamicDagUserRequest(state);
  if (userRequest && !seenUserRequests.has(userRequest)) {
    seenUserRequests.add(userRequest);
    messages.push({
      role: 'user',
      kind: 'text',
      content: userRequest,
      timeline: [{ type: 'text', content: userRequest }],
    });
  }

  let timeline = timelineFromPersistedRunEvents(events, fallbackContent, output);
  if (dagSnapshot) timeline = upsertDagTimeline(timeline, dagSnapshot);
  const content = textContentFromTimeline(timeline) || fallbackContent;
  if (content || timeline.length || dagSnapshot) {
    messages.push({
      role: 'assistant',
      kind: 'text',
      content,
      timeline,
      dagSnapshot,
      traceSnapshot,
    });
  }
  return messages;
}

function appendDynamicDagSegmentMessages(messages: ChatMessage[], segmentMessages: ChatMessage[]): void {
  for (const message of segmentMessages) {
    const last = messages[messages.length - 1];
    if (message.role === 'assistant' && last?.role === 'assistant') {
      messages[messages.length - 1] = mergePersistedAssistantMessage(last, message);
      continue;
    }
    messages.push(message);
  }
}

function mergePersistedAssistantMessage(base: ChatMessage, next: ChatMessage): ChatMessage {
  const timeline = mergePersistedAssistantTimeline(base.timeline ?? [], next.timeline ?? []);
  return {
    ...base,
    content: textContentFromTimeline(timeline) || next.content || base.content,
    timeline,
    dagSnapshot: next.dagSnapshot ?? base.dagSnapshot,
    traceSnapshot: next.traceSnapshot?.length ? next.traceSnapshot : base.traceSnapshot,
  };
}

function mergePersistedAssistantTimeline(
  base: MessageTimelineItem[],
  next: MessageTimelineItem[],
): MessageTimelineItem[] {
  let timeline = [...base];
  for (const item of next) {
    if (item.type === 'dag') {
      timeline = upsertDagTimeline(timeline, item.dag);
    } else if (item.type === 'text') {
      timeline = appendTextTimeline(closeReasoningTimeline(timeline), item.content);
    } else {
      timeline = [...timeline, item];
    }
  }
  return timeline;
}

function persistedRunEventGroups(events: ApiRunEvent[]): ApiRunEvent[][] {
  const groups = new Map<string, ApiRunEvent[]>();
  for (const event of [...events].sort((left, right) => left.event_id - right.event_id)) {
    const streamId = event.stream_id || `event_${event.event_id}`;
    const group = groups.get(streamId) ?? [];
    group.push(event);
    groups.set(streamId, group);
  }
  return [...groups.values()];
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
  finalAnswer: string = fallbackContent,
): MessageTimelineItem[] {
  let timeline: MessageTimelineItem[] = [];
  const capabilityReviews: ReviewEventPayload[] = [];
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
      onReview: (review) => {
        if (review.kind === 'capability_review') capabilityReviews.push(review);
      },
    });
  }

  if (finalAnswer) {
    timeline = settleRejectedCapabilityReviews(timeline, capabilityReviews);
  }
  return ensureFinalTextTimeline(timeline, fallbackContent);
}

function fallbackContentFromPersistedRunEvents(events: ApiRunEvent[]): string {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const envelope = streamEnvelope(events[index]);
    const data = recordValue(envelope?.data);
    if (envelope?.type === 'review.required' && typeof data?.message === 'string') {
      return data.message.trim();
    }
    if (envelope?.type === 'run.failed' && typeof data?.message === 'string') {
      return data.message.trim();
    }
  }
  return '';
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

function settleRejectedCapabilityReviews(
  timeline: MessageTimelineItem[],
  reviews: ReviewEventPayload[],
): MessageTimelineItem[] {
  let next = timeline;
  for (const review of reviews) {
    const invocationId = review.capability_call?.invocation_id;
    if (!invocationId || capabilityCallHasResult(next, invocationId)) continue;
    next = appendCapabilityReviewDecisionTimeline(next, review, false);
  }
  return next;
}

function capabilityCallHasResult(timeline: MessageTimelineItem[], invocationId: string): boolean {
  return timeline.some(
    (item) => item.type === 'capability'
      && item.event.invocation_id === invocationId
      && Boolean(item.result),
  );
}

function ensureFinalTextTimeline(
  timeline: MessageTimelineItem[],
  fallbackContent: string,
): MessageTimelineItem[] {
  const finalText = fallbackContent.trim();
  if (!finalText) return timeline;
  const lastText = lastTextTimelineItem(timeline);
  if (lastText?.content.trim() === finalText) return timeline;
  return appendTextTimeline(closeReasoningTimeline(timeline), fallbackContent);
}

function lastTextTimelineItem(timeline: MessageTimelineItem[]): Extract<MessageTimelineItem, { type: 'text' }> | null {
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    const item = timeline[index];
    if (item.type === 'text') return item;
  }
  return null;
}

function lastAssistantMessageIndex(messages: ChatMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'assistant') return index;
  }
  return -1;
}

function nextAssistantMessageIndex(messages: ChatMessage[], startIndex: number): number {
  for (let index = Math.max(0, startIndex); index < messages.length; index += 1) {
    if (messages[index].role === 'assistant') return index;
  }
  return -1;
}

function textContentFromTimeline(timeline: MessageTimelineItem[]): string {
  return timeline
    .flatMap((item) => item.type === 'text' ? [item.content] : [])
    .join('');
}

function dynamicDagUserRequest(state: ApiRunState | null): string {
  if (typeof state?.user_request === 'string' && state.user_request.trim()) {
    return state.user_request.trim();
  }
  for (const message of inputMessagesFromRunState(state)) {
    if (message.role !== 'user') continue;
    const content = visibleInputChatContentFromInternalMessage(message).trim();
    if (content) return content;
  }
  return '';
}

function inputMessagesFromRunState(state: ApiRunState | null): Record<string, unknown>[] {
  if (!state) return [];
  const internalMessages = state.internal_messages ?? [];
  const count = normalizedInputMessageCount(state, internalMessages.length);
  if (count !== null) return internalMessages.slice(0, count);
  if (state.kind !== 'tool' && typeof state.user_request === 'string' && state.user_request.trim()) {
    return [{ role: 'user', content: state.user_request }];
  }
  return internalMessages;
}

function inputBoundaryFromRunState(state: ApiRunState | null): number | null {
  if (!state) return null;
  return normalizedInputMessageCount(state, (state.internal_messages ?? []).length);
}

function normalizedInputMessageCount(state: ApiRunState, messageCount: number): number | null {
  const count = state.input_message_count;
  if (typeof count !== 'number' || !Number.isInteger(count) || count < 0) return null;
  return Math.min(count, messageCount);
}

function visibleInputChatContentFromInternalMessage(message: Record<string, unknown>): string {
  const content = visibleChatContentFromInternalMessage(message);
  return message.role === 'user' ? visibleUserInputContent(content) : content;
}

function visibleUserInputContent(content: string): string {
  const withoutTaskId = stripTaskIdHeader(content);
  if (withoutTaskId.trimStart().startsWith('DAG observation:')) return '';
  return withoutTaskId;
}

function stripTaskIdHeader(content: string): string {
  return content.replace(/^Task id:\s*\S+\s*\n+/, '');
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
