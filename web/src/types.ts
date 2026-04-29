export type RiskLevel = 'low' | 'medium' | 'high';
export type BoundaryMode = 'read_only' | 'write_limited' | 'full';

export interface Boundary {
  mode: BoundaryMode;
  allowed_paths: string[];
  forbidden_tools: string[];
  allowed_commands: string[];
  forbidden_commands: string[];
}

export interface DagNode {
  id: string;
  title: string;
  goal: string;
  agent: string | null;
  tools: string[];
  skills: string[];
  boundary: Boundary;
  risk: RiskLevel;
  risk_reason: string;
  expected_output: string;
  max_steps: number;
  timeout_seconds: number;
}

export interface DagEdge {
  source: string;
  target: string;
  reason: string;
}

export interface Dag {
  dag_id: string;
  task_id: string;
  version: number;
  status: 'draft' | 'review_required' | 'approved' | 'running' | 'completed' | 'failed';
  nodes: DagNode[];
  edges: DagEdge[];
}

export interface TraceEvent {
  id: string;
  type: 'dag' | 'node' | 'tool' | 'model';
  label: string;
  detail: string;
  status: 'queued' | 'running' | 'completed' | 'failed';
  timestamp: string;
}

