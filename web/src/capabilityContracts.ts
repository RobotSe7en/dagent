export type CapabilityIdPrefix = 'tool' | 'agent' | 'mcp' | 'skill' | 'memory';

const capabilityIdSegmentPattern = /^[A-Za-z0-9_]+$/;
const capabilityIdSegmentCounts: Record<CapabilityIdPrefix, number> = {
  tool: 2,
  agent: 2,
  mcp: 3,
  skill: 2,
  memory: 2,
};

export function capabilityDisplayName(capability: { id: string }): string {
  return capability.id;
}

export function isValidCapabilityId(value: string): boolean {
  if (value !== value.trim()) return false;
  const parts = value.split('.');
  if (parts.some((part) => !part || !capabilityIdSegmentPattern.test(part))) return false;
  const prefix = parts[0] as CapabilityIdPrefix;
  return Object.prototype.hasOwnProperty.call(capabilityIdSegmentCounts, prefix)
    && parts.length === capabilityIdSegmentCounts[prefix];
}

export function cleanWorkspaceKeyDraft(value: string, options: { requireLeadingLetter?: boolean } = {}): string {
  const cleaned = value
    .replace(/[^A-Za-z0-9_]+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '');
  if (!cleaned) return '';
  if (options.requireLeadingLetter && !/^[A-Za-z]/.test(cleaned)) return `agent_${cleaned}`;
  return cleaned;
}
