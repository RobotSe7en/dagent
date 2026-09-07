import type { CapabilityReviewDecision, CapabilityReviewItem, ReviewEventPayload } from './types';

export type CapabilityReviewSelection = boolean | CapabilityReviewDecision[];

export function capabilityReviewCalls(review: ReviewEventPayload): CapabilityReviewItem[] {
  if (review.capability_calls?.length) return review.capability_calls;
  if (!review.capability_call) return [];
  const payload = review.payload ?? {};
  return [{
    ...review.capability_call,
    message: review.message,
    risk: payload.risk === 'medium' || payload.risk === 'high' ? payload.risk : 'low',
    reason: payload.reason === 'boundary_violation' ? 'boundary_violation' : 'risk',
    boundary_paths: Array.isArray(payload.boundary_paths) ? payload.boundary_paths.map(String) : [],
    error: typeof payload.error === 'string' ? payload.error : null,
  }];
}

export function capabilityDecisionBody(selection: CapabilityReviewSelection) {
  return typeof selection === 'boolean'
    ? { approved: selection }
    : { capability_decisions: selection };
}

export function capabilityDecisionApproved(selection: CapabilityReviewSelection, invocationId: string): boolean {
  if (typeof selection === 'boolean') return selection;
  const decision = selection.find((item) => item.invocation_id === invocationId);
  if (!decision) throw new Error(`Missing review decision: ${invocationId}`);
  return decision.approved;
}

export function completeCapabilityDecisions(
  review: ReviewEventPayload, choices: Record<string, boolean>,
): CapabilityReviewDecision[] | null {
  const calls = capabilityReviewCalls(review);
  if (!calls.length || calls.some((call) => typeof choices[call.invocation_id] !== 'boolean')) return null;
  return calls.map((call) => ({ invocation_id: call.invocation_id, approved: choices[call.invocation_id] }));
}
