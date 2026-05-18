export type RiskLevel = 'low' | 'medium' | 'high';
export type BoundaryMode = 'read_only' | 'write_limited' | 'full';
export type ReviewLevel = 'fast' | 'careful';

export interface Boundary {
  mode: BoundaryMode;
  allowed_paths?: string[];
  allowed_commands?: string[];
}

export type CapabilityKind = 'tool' | 'mcp' | 'skill' | 'shell' | 'custom_tool' | 'agent' | 'memory' | 'file';

export interface CapabilityInvocation {
  invocation_id?: string;
  capability_id: string;
  kind: CapabilityKind;
  arguments: Record<string, unknown>;
  boundary?: Boundary;
  risk?: RiskLevel;
}

export interface DagNode {
  id: string;
  invocation: CapabilityInvocation;
  node_type?: 'capability' | 'start';
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
    | 'review_required'
    | 'approved'
    | 'running'
    | 'completed'
    | 'failed'
    | 'aborted';
  nodes: DagNode[];
  edges: DagEdge[];
}

export interface TraceLogEvent {
  event_id?: string;
  event_type?: string;
  dag_id?: string;
  node_id?: string | null;
  payload?: Record<string, unknown>;
  created_at?: string;
  id: string;
  type: 'dag' | 'node' | 'capability' | 'model';
  label: string;
  detail: string;
  status: 'queued' | 'running' | 'completed' | 'failed';
  timestamp: string;
}

export type CapabilityResultStatus = 'completed' | 'failed';

export interface CapabilityResult {
  invocation_id: string;
  capability_id: string;
  kind: CapabilityKind;
  status: CapabilityResultStatus;
  content?: string;
  error?: string | null;
  stop_reason?: string;
  steps?: number;
  trace?: RunTrace | null;
}

export type RunTraceStatus =
  | 'planned'
  | 'running'
  | 'awaiting_review'
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'cancelled';

export type RunTraceNodeKind =
  | 'run'
  | 'dag_node'
  | 'agent_loop'
  | 'agent_step'
  | 'model_call'
  | 'capability_call'
  | 'review'
  | 'artifact';

export interface RunTraceError {
  message: string;
  code?: string;
}

export interface CapabilityExecution {
  invocation: CapabilityInvocation;
  result?: CapabilityResult | null;
}

export interface RunTraceNode {
  id: string;
  parent_id?: string | null;
  kind: RunTraceNodeKind;
  status: RunTraceStatus;
  label: string;
  started_at?: string | null;
  ended_at?: string | null;
  step_count: number;
  ref: Record<string, string>;
  input: Record<string, unknown>;
  output?: unknown;
  error?: RunTraceError | null;
  capability_execution?: CapabilityExecution | null;
  children: RunTraceNode[];
}

export interface RunTrace {
  run_id: string;
  root: RunTraceNode;
  artifacts: Record<string, unknown>;
}

export interface CapabilityStreamEvent {
  type: 'capability_call' | 'capability_result' | 'capability_error';
  invocation_id: string;
  capability_id: string;
  arguments: Record<string, unknown>;
  content?: string;
}

export interface CapabilityCallPayload {
  invocation_id: string;
  capability_id: string;
  arguments: Record<string, unknown>;
}

export interface ReviewEventPayload {
  review_id: string;
  kind: 'initial_dag' | 'dag_replan' | 'capability_review';
  message: string;
  dag?: Dag;
  capability_call?: CapabilityCallPayload;
  payload?: Record<string, unknown>;
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
