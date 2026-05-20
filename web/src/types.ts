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

export interface CapabilityPolicy {
  risk: RiskLevel;
  requires_review: boolean;
  sandbox_required: boolean;
  network: boolean;
  secrets: string[];
}

export interface CapabilityDefinition {
  id: string;
  name: string;
  kind: CapabilityKind;
  description: string;
  parameters: Record<string, unknown>;
  policy: CapabilityPolicy;
  config: Record<string, unknown>;
  enabled: boolean;
}

export interface CapabilityNodePayload {
  type: 'capability';
  invocation: CapabilityInvocation;
}

export interface StartNodePayload {
  type: 'start';
}

export type DagNodePayload = CapabilityNodePayload | StartNodePayload;

export interface DagNode {
  id: string;
  title?: string;
  goal?: string | null;
  instructions?: string | null;
  payload: DagNodePayload;
  status?: 'planned' | 'ready' | 'running' | 'completed' | 'failed' | 'skipped';
  inputs?: string[];
  outputs?: string[];
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

export interface DagSpec {
  id: string;
  name: string;
  version?: number;
  description?: string;
  input_schema?: Record<string, unknown>;
  artifacts?: Record<string, unknown>;
  nodes: DagNode[];
  edges: DagEdge[];
  metadata?: Record<string, unknown>;
}

export interface DagRun {
  run_id: string;
  spec_id?: string | null;
  workspace_path: string;
  dag: Dag;
  trace: RunTrace;
  status: 'planned' | 'running' | 'completed' | 'failed';
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

export interface AgentProfile {
  name: string;
  role: string;
  description: string;
  layers: string[];
  layer_contents: Record<string, string>;
  memory_file?: string | null;
  memory: string;
  output_format: string;
}

export interface ProfileWarning {
  name: string;
  error: string;
}

export type WorkspaceKey = 'chat' | 'orchestration' | 'tools' | 'agents';
