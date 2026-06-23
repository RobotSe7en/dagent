export type RiskLevel = 'low' | 'medium' | 'high';
export type ReviewLevel = 'fast' | 'careful';

export type ValuePathItem = string | number;

export type ValueExpr =
  | { type: 'graph_input'; path?: ValuePathItem[] }
  | { type: 'node_output'; node_id: string; field?: 'value' | 'content' | 'status' | 'steps'; path?: ValuePathItem[] }
  | { type: 'artifact'; artifact_id: string; field?: 'path' | 'paths' | 'absolute_path' | 'absolute_paths' }
  | { type: 'format'; template: string; values?: Record<string, unknown> };

export interface ValueBinding {
  $expr: ValueExpr;
}

export type BoundaryValue = string | ValueBinding;

export interface Boundary {
  allowed_paths?: BoundaryValue[];
}

export type CapabilityKind = 'tool' | 'mcp' | 'skill' | 'agent' | 'memory';

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
  output_schema: Record<string, unknown>;
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

export interface MapNodePayload {
  type: 'map';
  items: unknown;
  invocation: CapabilityInvocation;
  max_items?: number;
  max_concurrency?: number;
}

export interface SubgraphNodePayload {
  type: 'subgraph';
  spec: DagSpec;
  input?: unknown;
}

export interface LoopNodePayload {
  type: 'loop';
  body: DagSpec;
  until: unknown;
  max_iterations: number;
  input?: unknown;
}

export type DagNodePayload =
  | CapabilityNodePayload
  | StartNodePayload
  | MapNodePayload
  | SubgraphNodePayload
  | LoopNodePayload;

export interface DagNode {
  id: string;
  title?: string;
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

export interface DagSpec {
  id: string;
  name?: string;
  version?: number;
  description?: string;
  input_schema?: Record<string, unknown>;
  artifacts?: Record<string, Artifact>;
  nodes: DagNode[];
  edges: DagEdge[];
  output?: unknown;
  metadata?: Record<string, unknown>;
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
    | 'awaiting_review'
    | 'completed'
    | 'failed'
    | 'rejected'
    | 'aborted';
  nodes: DagNode[];
  edges: DagEdge[];
}

export interface UserDag {
  id: string;
  name: string;
  version?: number;
  description?: string;
  input_schema?: Record<string, unknown>;
  artifacts?: Record<string, Artifact>;
  nodes: UserDagNode[];
  edges: DagEdge[];
  metadata?: Record<string, unknown>;
}

export interface UserDagNode {
  id: string;
  target: string;
  inputs?: Record<string, unknown>;
  artifact_inputs?: string[];
  artifact_outputs?: string[];
  title?: string;
  boundary?: Boundary | null;
  agent?: UserDagAgentConfig | null;
}

export interface UserDagAgentConfig {
  capabilities?: string[] | null;
  skills?: string[] | null;
}

export interface DagValidationIssue {
  severity: 'error' | 'warning';
  code: string;
  message: string;
  node_id?: string | null;
  path?: ValuePathItem[];
}

export interface DagValidationResult {
  valid: boolean;
  issues: DagValidationIssue[];
}

export interface Artifact {
  id: string;
  paths: string[];
  description?: string;
  required?: boolean;
  metadata?: Record<string, unknown>;
}

export type RunArtifactPreviewKind = 'markdown' | 'code' | 'text';
export type RunArtifactFileSource = 'dag_artifact' | 'run_file';

export interface RunArtifactFile {
  id: string;
  artifact_id?: string | null;
  source: RunArtifactFileSource;
  path: string;
  name: string;
  media_type: string;
  preview_kind?: RunArtifactPreviewKind | null;
  previewable: boolean;
  size?: number | null;
  status: string;
  error?: string | null;
  preview_url?: string | null;
}

export interface RunArtifactsResponse {
  run_id: string;
  workspace_path?: string | null;
  artifacts: Record<string, unknown>;
  files: RunArtifactFile[];
  files_truncated?: boolean;
  file_limit?: number;
  visit_limit?: number;
}

export interface RunArtifactPreview {
  run_id: string;
  path: string;
  name: string;
  media_type: string;
  preview_kind: RunArtifactPreviewKind;
  content: string;
  size: number;
  truncated: boolean;
  truncated_at: number;
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
  status: 'queued' | 'running' | 'awaiting_review' | 'completed' | 'failed' | 'rejected';
  timestamp: string;
}

export type CapabilityResultStatus = 'completed' | 'failed';

export interface CapabilityResult {
  invocation_id: string;
  capability_id: string;
  kind: CapabilityKind;
  status: CapabilityResultStatus;
  content?: string;
  value?: unknown;
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
  value?: unknown;
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
  type: 'capability.call.started' | 'capability.call.completed' | 'capability.call.failed';
  invocation_id: string;
  capability_id: string;
  arguments?: Record<string, unknown>;
  content?: string;
  run_id?: string | null;
  dag_id?: string | null;
  node_id?: string | null;
  parent_capability_id?: string | null;
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
  proposed_dag?: Dag | null;
  capability_call?: CapabilityCallPayload;
  payload?: Record<string, unknown>;
}

export interface ValidationIssue {
  message: string;
  node_id?: string | null;
}

export interface ValidationFeedbackEvent {
  type: 'validation.retry' | 'validation.passed';
  passed?: boolean;
  reason?: string;
  summary: string;
  issues: ValidationIssue[];
}

export interface AgentProfile {
  id: string;
  name: string;
  description: string;
  content: string;
  source: 'builtin' | 'managed' | 'config';
  editable: boolean;
  deletable: boolean;
}

export interface ProfileWarning {
  name: string;
  error: string;
}

export type WorkspaceKey = 'chat' | 'orchestration' | 'tools' | 'agents' | 'models';

export interface SkillSummary {
  name: string;
  description: string;
  category?: string | null;
  path: string;
  managed: boolean;
}

export interface SkillDetail {
  skill: SkillSummary;
  name: string;
  description: string;
  category?: string | null;
  path: string;
  skill_dir?: string | null;
  metadata: Record<string, unknown>;
  content: string;
  linked_files: Record<string, string[]>;
}

export interface SkillFileDetail {
  skill: SkillSummary;
  file_path: string;
  path: string;
  skill_dir: string;
  content: string;
}

export interface MCPServerConfig {
  command: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string | null;
  enabled?: boolean;
  risk?: RiskLevel;
  connect_timeout?: number;
  tool_timeout?: number;
  include_tools?: string[];
  exclude_tools?: string[];
}

export interface MCPServer {
  name: string;
  source: 'memory' | 'config' | 'runtime';
  config: MCPServerConfig;
  status: 'disabled' | 'connected' | 'error' | 'pending';
  error?: string | null;
  tools: CapabilityDefinition[];
}

export interface ModelProvider {
  id: string;
  name: string;
  source: 'config' | 'runtime';
  active: boolean;
  base_url: string;
  model: string;
  api_key_env?: string | null;
  api_key_configured: boolean;
  api_key_saved: boolean;
  timeout_seconds: number;
  strip_thinking: boolean;
  reasoning?: Record<string, unknown> | null;
  extra_request_args: Record<string, unknown>;
  extra_body: Record<string, unknown>;
}

export type ModelApiKeyAction = 'preserve' | 'replace' | 'clear';

export interface ModelProviderInput {
  id: string;
  name: string;
  base_url: string;
  model: string;
  api_key?: string | null;
  api_key_action: ModelApiKeyAction;
  api_key_env?: string | null;
  timeout_seconds: number;
  strip_thinking: boolean;
  reasoning?: Record<string, unknown> | null;
  extra_request_args: Record<string, unknown>;
  extra_body: Record<string, unknown>;
}
