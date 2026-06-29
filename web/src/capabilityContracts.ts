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

type ToolManagementCapability = {
  kind?: string;
  id?: string | null;
  name?: string | null;
  display_name?: string | null;
  description?: string | null;
  config?: Record<string, unknown> | null;
};

type ToolManagementPythonSource = {
  id: string;
  source?: string | null;
  path?: string | null;
  module?: string | null;
  names?: string[];
  capabilities: string[];
};

export type ToolManagementTreeItem<T> = {
  capability: T;
};

export type ToolManagementGroup<T> = {
  id: string;
  label: string;
  items: ToolManagementTreeItem<T>[];
};

export type ToolManagementPythonSourceGroup<T, S> = {
  id: string;
  label: string;
  source: S;
  items: ToolManagementTreeItem<T>[];
};

export type ToolManagementTree<T, S> = {
  builtin: ToolManagementGroup<T>;
  pythonSources: ToolManagementPythonSourceGroup<T, S>[];
  manual: ToolManagementGroup<T>;
};

export function visibleToolManagementCapabilities<T extends ToolManagementCapability>(
  capabilities: T[],
  query: string,
): T[] {
  const normalizedQuery = query.toLowerCase();
  return capabilities.filter((capability) => {
    if (capability.kind !== 'tool') return false;
    return matchesToolCapability(capability, normalizedQuery);
  });
}

export function buildToolManagementTree<T extends ToolManagementCapability, S extends ToolManagementPythonSource>(
  capabilities: T[],
  pythonSources: S[],
  query: string,
): ToolManagementTree<T, S> {
  const normalizedQuery = query.toLowerCase();
  const toolCapabilities = capabilities.filter((capability) => capability.kind === 'tool');
  const capabilityById = new Map(
    toolCapabilities
      .filter((capability): capability is T & { id: string } => Boolean(capability.id))
      .map((capability) => [capability.id, capability]),
  );
  const pythonCapabilityIds = new Set<string>();
  const pythonSourceGroups = pythonSources.flatMap((source) => {
    const sourceMatches = matchesPythonToolSource(source, normalizedQuery);
    for (const capabilityId of source.capabilities) {
      pythonCapabilityIds.add(capabilityId);
    }
    const items = source.capabilities.flatMap((capabilityId) => {
      const capability = capabilityById.get(capabilityId);
      if (!capability) return [];
      if (normalizedQuery && !sourceMatches && !matchesToolCapability(capability, normalizedQuery)) return [];
      return [{ capability }];
    });
    if (!items.length) return [];
    const label = source.path?.trim() || source.module?.trim() || source.id;
    return [{
      id: source.id,
      label,
      source,
      items,
    }];
  });
  const builtinItems: ToolManagementTreeItem<T>[] = [];
  const manualItems: ToolManagementTreeItem<T>[] = [];
  for (const capability of toolCapabilities) {
    if (!capability.id || pythonCapabilityIds.has(capability.id)) continue;
    if (!matchesToolCapability(capability, normalizedQuery)) continue;
    const item = { capability };
    if (isManualToolCapability(capability)) {
      manualItems.push(item);
    } else {
      builtinItems.push(item);
    }
  }
  return {
    builtin: {
      id: 'builtin',
      label: '内置工具',
      items: builtinItems,
    },
    pythonSources: pythonSourceGroups,
    manual: {
      id: 'manual',
      label: '手工工具',
      items: manualItems,
    },
  };
}

function matchesToolCapability(capability: ToolManagementCapability, normalizedQuery: string): boolean {
  if (!normalizedQuery) return true;
  return [
    capability.id ?? '',
    capability.name ?? '',
    capability.display_name ?? '',
    capability.description ?? '',
    capability.kind ?? '',
  ].some((value) => value.toLowerCase().includes(normalizedQuery));
}

function matchesPythonToolSource(source: ToolManagementPythonSource, normalizedQuery: string): boolean {
  if (!normalizedQuery) return true;
  return [
    source.id,
    source.source ?? '',
    source.path ?? '',
    source.module ?? '',
    ...(source.names ?? []),
  ].some((value) => value.toLowerCase().includes(normalizedQuery));
}

function isManualToolCapability(capability: ToolManagementCapability): boolean {
  return Boolean(capability.config && Object.prototype.hasOwnProperty.call(capability.config, 'template'));
}
