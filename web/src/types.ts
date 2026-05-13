export type RiskLevel = 'low' | 'medium' | 'high';
export type BoundaryMode = 'read_only' | 'write_limited' | 'full';
export type ReviewLevel = 'fast' | 'careful';

export interface Boundary {
  mode: BoundaryMode;
  allowed_paths?: string[];
  allowed_commands?: string[];
}

export interface DagNode {
  id: string;
  tool?: string | null;
  args?: Record<string, unknown>;
  boundary?: Boundary;
  risk?: RiskLevel;
  status?: 'planned' | 'ready' | 'running' | 'completed' | 'failed' | 'skipped';
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
  status:
    | 'draft'
    | 'awaiting_dag_review'
    | 'review_required'
    | 'approved'
    | 'running'
    | 'completed'
    | 'failed'
    | 'aborted';
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

export interface NodeExecutionRecord {
  record_id: string;
  task_id: string;
  dag_id: string;
  dag_version: number;
  node_id: string;
  tool: string | null;
  args: Record<string, unknown>;
  output: string;
  error: string | null;
  status: 'completed' | 'failed';
  stop_reason: string;
  steps: number;
  created_at: string;
}

export interface ToolStreamEvent {
  type: 'tool_call' | 'tool_result' | 'tool_error';
  tool_call_id: string;
  name: string;
  arguments: Record<string, unknown>;
  content?: string;
}

export interface ToolCallPayload {
  tool_call_id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ReviewEventPayload {
  review_id: string;
  kind: 'initial_dag' | 'dag_replan' | 'tool_review';
  message: string;
  dag?: Dag;
  tool_call?: ToolCallPayload;
  payload?: Record<string, unknown>;
}

export interface RunResult {
  dag_id: string;
  completed: boolean;
  trace_records?: NodeExecutionRecord[];
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

export interface ValidationIssue {
  message: string;
  node_id?: string | null;
}

export interface ValidationFeedbackEvent {
  type: 'retry' | 'validation_passed';
  passed?: boolean;
  reason?: string;
  summary: string;
  issues: ValidationIssue[];
}
