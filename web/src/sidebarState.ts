export function nextExpandedSkillNames(
  current: Set<string>,
  name: string,
  selected: boolean,
): Set<string> {
  const next = new Set(current);
  if (selected && next.has(name)) {
    next.delete(name);
  } else {
    next.add(name);
  }
  return next;
}

export function nextMcpResourceSelection(
  name: string,
  toolId: string | null = null,
): { name: string; toolId: string } {
  return { name, toolId: toolId ?? '' };
}

export function resolveSelectedMcpToolId(
  current: string,
  availableToolIds: string[],
): string {
  return current && availableToolIds.includes(current) ? current : '';
}
