export type CapabilityIdPrefix = 'tool' | 'agent' | 'mcp' | 'skill' | 'memory';

const capabilityIdSegmentPattern = /^[A-Za-z0-9_]+$/;
const capabilityIdPrefixes = new Set<CapabilityIdPrefix>(['tool', 'agent', 'mcp', 'skill', 'memory']);

export function capabilityDisplayName(capability: { display_name?: string | null; name?: string | null; id?: string | null }): string {
  return capability.display_name?.trim() || capability.name?.trim() || capability.id?.trim() || '';
}

export function isValidCapabilityId(value: string): boolean {
  if (value !== value.trim()) return false;
  const parts = value.split('.');
  if (parts.some((part) => !part || !capabilityIdSegmentPattern.test(part))) return false;
  const prefix = parts[0] as CapabilityIdPrefix;
  return capabilityIdPrefixes.has(prefix) && parts.length >= 2;
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
