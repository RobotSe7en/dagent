import type { DagEdge } from './types';

export function pruneEdgesToNodeIds(edges: DagEdge[], nodeIds: Set<string>): DagEdge[] {
  return edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
}
