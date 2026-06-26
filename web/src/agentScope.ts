export type AgentScopeMode = 'none' | 'selected' | 'registered';

export interface ChatCapabilityScopePayload {
  capabilityIds?: string[] | null;
  skills?: string[];
  agentScope?: AgentScopeMode;
  agentIds?: string[];
}

export function pruneSelectedAgentIds(selectedIds: string[], agents: Array<{ id: string }>): string[] {
  const availableAgentIds = new Set(agents.map((agent) => agent.id));
  return selectedIds.filter((id) => availableAgentIds.has(id));
}

export function chatScopeRequestFields(scope?: ChatCapabilityScopePayload): Record<string, unknown> {
  if (!scope) return {};
  const capabilityIds = scope.capabilityIds;
  if (capabilityIds?.some((capabilityId) => capabilityId.startsWith('agent.'))) {
    throw new Error('Agent capabilities must use agentScope and agentIds.');
  }

  const fields: Record<string, unknown> = {};
  if (Object.prototype.hasOwnProperty.call(scope, 'capabilityIds')) fields.capability_ids = capabilityIds;
  if (Object.prototype.hasOwnProperty.call(scope, 'skills')) fields.skills = scope.skills ?? [];
  const agentScope = scope.agentScope ?? 'none';
  const agentIds = scope.agentIds ?? [];
  if (agentScope === 'none') {
    if (agentIds.length) throw new Error('agentIds require agentScope selected.');
    return fields;
  }
  fields.agent_scope = agentScope;
  if (agentScope === 'selected') fields.agent_ids = agentIds;
  if (agentScope === 'registered' && agentIds.length) {
    throw new Error('agentIds are not accepted when agentScope is registered.');
  }
  return fields;
}
