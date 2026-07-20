import type { ApiConversationMessage } from './api';
import type { ReviewEventPayload } from './types';

export function latestPendingReviewFromApiConversationMessages(
  items: ApiConversationMessage[],
): ReviewEventPayload | null {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const message = items[index];
    if (message.role !== 'assistant') continue;
    if (message.status !== 'awaiting_review') return null;
    return message.pending_review ?? null;
  }
  return null;
}
