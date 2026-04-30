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
  event_id?: string;
  event_type?: string;
  dag_id?: string;
  node_id?: string | null;
  payload?: Record<string, unknown>;
  created_at?: string;
  id: string;
  type: 'dag' | 'node' | 'tool' | 'model';
  label: string;
  detail: string;
  status: 'queued' | 'running' | 'completed' | 'failed';
  timestamp: string;
}

export interface ToolStreamEvent {
  type: 'tool_call' | 'tool_result' | 'tool_error';
  tool_call_id: string;
  name: string;
  arguments: Record<string, unknown>;
  content?: string;
}

export interface RunResult {
  dag_id: string;
  completed: boolean;
  node_results: Record<
    string,
    {
      node_id: string;
      final_response: string;
      completed: boolean;
      stop_reason: string;
      steps: number;
    }
  >;
  traces: TraceEvent[];
}
