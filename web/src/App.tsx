import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Background,
  Controls,
  Edge,
  Handle,
  MiniMap,
  Node,
  Position,
  ReactFlow,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  useReactFlow,
  type Connection,
  type EdgeChange,
  type NodeChange,
  type ReactFlowInstance,
  type XYPosition,
} from '@xyflow/react';
import {
  AlertTriangle,
  Bot,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleStop,
  Copy,
  Crosshair,
  Database,
  Download,
  File,
  FileText,
  Folder,
  GitBranch,
  LayoutDashboard,
  Loader,
  Maximize2,
  MessageSquare,
  Plus,
  Play,
  RefreshCw,
  Save,
  Search,
  Send,
  Settings,
  SlidersHorizontal,
  Trash2,
  Upload,
  UserCog,
  Wrench,
  X,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import {
  createAgent,
  createConversation,
  createMcpServer,
  createModelProvider,
  createOrchestrationSession,
  createProject,
  createProjectFolder,
  createProjectConversation,
  createPythonTool,
  deleteAgent,
  deleteCapability,
  deleteConversation,
  deleteMcpServer,
  deleteModelProvider,
  deleteProject,
  deleteProjectConversation,
  deleteProjectFile,
  deletePythonTool,
  deleteSkill,
  getSkill,
  getSkillFile,
  getValidationStatus,
  getOnlyOfficeSettings,
  getOrchestrationSessionByConversation,
  installSkill,
  listAgents,
  listCapabilities,
  listConversations,
  listSavedDags,
  listMcpServers,
  listModels,
  listProjectConversations,
  listProjectFiles,
  listProjects,
  listPythonTools,
  listProfiles,
  listRunArtifacts,
  listRunEvents,
  listSkills,
  mapRunTrace,
  previewRunArtifact,
  previewProjectFile,
  projectFileDownloadUrl,
  renameProjectFile,
  reloadMcpServers,
  reloadPythonTools,
  resumeCapabilityReview,
  resumeDagReview,
  runSavedDagStream,
  saveSavedDag,
  setCapabilityEnabled,
  setValidationEnabled as apiSetValidation,
  streamMessagesTask,
  streamTask,
  testCapability,
  uploadSavedDagArtifact,
  uploadPythonTool,
  updateMcpServer,
  updateModelProvider,
  updateOnlyOfficeSettings,
  updateOrchestrationSession,
  updateProject,
  updatePythonTool,
  uploadProjectFiles,
  updateAgent,
  validatePythonTool,
  activateModelProvider,
  validateDag,
  createProfile,
  updateProfile,
  deleteProfile,
  discoverPythonToolNames,
} from './api';
import type { ApiRunEvent, ApiRunResult, ApiRunState, ChatStreamMessage } from './api';
import type {
  AgentPreset,
  AgentPresetInput,
  AgentProfile,
  ApiConversation,
  ApiProject,
  CapabilityDefinition,
  CapabilityInvocation,
  CapabilityKind,
  CapabilityNodePayload,
  CapabilityResult,
  Dag,
  DagEdge,
  DagNode,
  DagRun,
  DagSpec,
  DagValidationIssue,
  UserDag,
  ProfileWarning,
  ReviewEventPayload,
  ValidationFeedbackEvent,
  ReviewLevel,
  ProjectFileItem,
  ProjectFilePreview,
  RunArtifactFile,
  RunArtifactPreview,
  RiskLevel,
  CapabilityStreamEvent,
  TraceLogEvent,
  WorkspaceKey,
  Artifact,
  MCPServer,
  MCPServerConfig,
  ModelApiKeyAction,
  ModelProvider,
  ModelProviderInput,
  OnlyOfficeSettings,
  OrchestrationSession,
  PythonToolConfig,
  PythonToolEntry,
  SavedDag,
  SkillDetail,
  SkillFileDetail,
  SkillSummary,
  BoundaryValue,
  UserDagAgentConfig,
  UserDagNode,
  ValueBinding,
} from './types';
import { pruneSelectedAgentIds, type AgentScopeMode } from './agentScope';
import {
  buildMcpManagementTree,
  buildToolManagementTree,
  capabilityDisplayName,
  cleanWorkspaceKeyDraft,
  visibleToolManagementCapabilities,
} from './capabilityContracts';
import {
  pythonToolDiscoverySourceKey,
  shouldApplyPythonToolDiscoveryResult,
  type PythonToolDiscoveryState,
} from './pythonToolDiscovery';
import { canvasCenterNodePosition } from './canvasPositions';
import {
  nextExpandedSkillNames,
  nextMcpResourceSelection,
  resolveSelectedMcpToolId,
} from './sidebarState';
import {
  buildSchemaArgumentFields,
  coerceArgumentValue,
  ensureSchemaArguments,
  formatArgumentValue,
  parseArgumentValue,
  resetSchemaArguments,
  visibleCapabilitiesForPicker,
  type ArgumentValueType,
} from './schemaArguments';
import { pruneEdgesToNodeIds } from './dagEdges';
import {
  artifactPathExpr,
  buildPendingUploadGroups,
  createUploadedFileArtifacts,
  isUploadedFileArtifact,
  removeArtifactBinding,
  uploadFormFilename,
  updateArtifactBinding,
  upsertArtifact,
  visiblePendingUploadGroups,
  type UploadSourceFile,
} from './dagArtifacts';
import {
  appendRunTranscriptCapability,
  appendRunTranscriptTraceEvent,
  appendRunTranscriptToken,
  buildRunDialogSummary,
  runTranscriptFromTraceEvents,
  type RunDialogSummary,
  type RunTranscriptItem,
} from './orchestrationRun';
import {
  appendCapabilityReviewDecisionTimeline,
  appendReasoningTimeline,
  appendTextTimeline,
  appendValidatingTimeline,
  appendValidationTimeline,
  closeReasoningTimeline,
  upsertDagMessageTimeline,
  type ChatMessage,
  type MessageTimelineItem,
} from './chatTimeline';
import {
  artifactPreviewDownloadUrl,
  artifactPreviewMode,
  isBrowserArtifactPreviewKind,
  renderBrowserArtifactPreview,
  shouldFetchTextArtifactPreview,
  type ArtifactPreviewRenderHandle,
} from './artifactPreview';
import {
  artifactFolderIdsForPath,
  buildWorkbenchArtifactTree,
  buildWorkbenchArtifacts,
  type WorkbenchArtifactTreeNode,
  type WorkbenchArtifactItem,
} from './workbenchArtifacts';
import { createUiId } from './uiIds';
import {
  bindingLabel,
  buildVariableCatalog,
  collectNodeOutputRefs,
  isValueBinding,
  removeNodeOutputRefs,
  wouldCreateCycle,
  type VariableCatalog,
  type VariableCatalogItem,
} from './valueBindings';
import dagentMark from './assets/dagent-mark.svg';

const riskClass: Record<RiskLevel, string> = {
  low: 'risk-low',
  medium: 'risk-medium',
  high: 'risk-high',
};

const riskLevels: RiskLevel[] = ['low', 'medium', 'high'];
const reviewLevels: ReviewLevel[] = ['fast', 'careful'];
const capabilityKinds: CapabilityKind[] = ['tool', 'mcp', 'skill', 'agent', 'memory'];
const riskRank: Record<RiskLevel, number> = { low: 0, medium: 1, high: 2 };
const emptyDag: Dag = {
  dag_id: 'dag_empty',
  task_id: '',
  version: 1,
  status: 'draft',
  nodes: [],
  edges: [],
};

const defaultMcpConfig: { name: string } & MCPServerConfig = {
  name: 'local',
  transport: 'stdio',
  command: '',
  args: [],
  env: {},
  url: '',
  headers: {},
  enabled: true,
  risk: 'medium',
  connect_timeout: 30,
  tool_timeout: 60,
};

const defaultPythonToolConfig: PythonToolConfig = {
  id: 'local_tools',
  source: 'path',
  path: '',
  module: '',
  names: [],
  enabled: true,
};

const defaultModelDraft: ModelProviderInput = {
  id: 'user-model',
  name: '',
  base_url: 'https://api.openai.com/v1',
  model: '',
  api_key: null,
  api_key_action: 'replace',
  api_key_env: '',
  timeout_seconds: 60,
  strip_thinking: false,
  reasoning: null,
  extra_request_args: {},
  extra_body: {},
};

const defaultOnlyOfficeSettings: OnlyOfficeSettings = {
  enabled: false,
  document_server_url: null,
  public_api_base: null,
  jwt_secret: null,
  lang: 'zh',
};

const workspaceItems: Array<{ key: WorkspaceKey; label: string; icon: React.ReactNode }> = [
  { key: 'chat', label: '智能工作台', icon: <LayoutDashboard size={16} /> },
  { key: 'orchestration', label: '智能体编排', icon: <GitBranch size={16} /> },
  { key: 'tools', label: '能力管理', icon: <Wrench size={16} /> },
  { key: 'agents', label: '智能体管理', icon: <Bot size={16} /> },
  { key: 'system', label: '系统管理', icon: <Settings size={16} /> },
];

const workspacePlaceholderLabels: Record<Exclude<WorkspaceKey, 'chat'>, string> = {
  orchestration: 'AI 编排工作区',
  tools: '能力管理工作区',
  system: '系统管理工作区',
  agents: '智能体管理工作区',
};

type SearchableValue = string | number | boolean | null | undefined;

function normalizeSearchQuery(query: string): string {
  return query.trim().toLowerCase();
}

function matchesSearchQuery(values: SearchableValue[], query: string): boolean {
  if (!query) return true;
  return values.some((value) => String(value ?? '').toLowerCase().includes(query));
}

function joinProjectPath(base: string, name: string): string {
  return [base, name].map((part) => part.trim().replace(/^\/+|\/+$/g, '')).filter(Boolean).join('/');
}

function parentProjectPath(path: string): string {
  const parts = path.split('/').filter(Boolean);
  parts.pop();
  return parts.join('/');
}

function SidebarSearchField({
  value,
  onChange,
  placeholder = '搜索…',
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="sidebar-search-field">
      <Search size={13} />
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
      />
    </label>
  );
}

function isCapabilityNode(node: DagNode): node is DagNode & { payload: CapabilityNodePayload } {
  return node.payload.type === 'capability';
}

interface NodeReviewInfo {
  risk: RiskLevel;
  hasBoundary: boolean;
  reviewAttention: boolean;
}

function normalizeInvocation(invocation: CapabilityInvocation): CapabilityInvocation {
  return {
    ...invocation,
    capability_id: invocation.capability_id ?? '',
    kind: invocation.kind ?? 'tool',
    arguments: invocation.arguments ?? {},
    boundary: {
      allowed_paths: invocation.boundary?.allowed_paths ?? ['.'],
    },
    risk: invocation.risk ?? 'low',
  };
}

function normalizeNode(node: DagNode): DagNode {
  if (node.payload.type === 'capability') {
    return {
      ...node,
      payload: {
        type: 'capability',
        invocation: normalizeInvocation(node.payload.invocation),
      },
      status: node.status ?? 'planned',
    };
  }
  if (node.payload.type === 'map') {
    return {
      ...node,
      payload: {
        ...node.payload,
        invocation: normalizeInvocation(node.payload.invocation),
      },
      status: node.status ?? 'planned',
    };
  }
  if (node.payload.type === 'subgraph') {
    return {
      ...node,
      payload: {
        ...node.payload,
        spec: normalizeDagSpec(node.payload.spec),
      },
      status: node.status ?? 'planned',
    };
  }
  if (node.payload.type === 'loop') {
    return {
      ...node,
      payload: {
        ...node.payload,
        body: normalizeDagSpec(node.payload.body),
      },
      status: node.status ?? 'planned',
    };
  }
  return {
    ...node,
    payload: { type: 'start' },
    status: node.status ?? 'planned',
  };
}

function normalizeDagSpec(spec: DagSpec): DagSpec {
  return {
    ...spec,
    nodes: (spec.nodes ?? []).map(normalizeNode),
    edges: spec.edges ?? [],
  };
}

function nodeReviewInfo(node: DagNode): NodeReviewInfo {
  const normalized = normalizeNode(node);
  const payload = normalized.payload;
  if (payload.type === 'capability' || payload.type === 'map') {
    return invocationReviewInfo(payload.invocation);
  }
  if (payload.type === 'subgraph') {
    return nodesReviewInfo(payload.spec.nodes ?? []);
  }
  if (payload.type === 'loop') {
    return nodesReviewInfo(payload.body.nodes ?? []);
  }
  return {
    risk: 'low',
    hasBoundary: false,
    reviewAttention: false,
  };
}

function invocationReviewInfo(invocation: CapabilityInvocation): NodeReviewInfo {
  const risk = invocation.risk ?? 'low';
  return {
    risk,
    hasBoundary: true,
    reviewAttention: risk !== 'low',
  };
}

function nodesReviewInfo(nodes: DagNode[]): NodeReviewInfo {
  return nodes.reduce<NodeReviewInfo>(
    (summary, node) => mergeReviewInfo(summary, nodeReviewInfo(node)),
    {
      risk: 'low',
      hasBoundary: false,
      reviewAttention: false,
    },
  );
}

function mergeReviewInfo(left: NodeReviewInfo, right: NodeReviewInfo): NodeReviewInfo {
  const risk = riskRank[right.risk] > riskRank[left.risk] ? right.risk : left.risk;
  return {
    risk,
    hasBoundary: left.hasBoundary || right.hasBoundary,
    reviewAttention: left.reviewAttention || right.reviewAttention,
  };
}

function normalizeUserDagNode(node: UserDagNode): UserDagNode {
  return {
    id: node.id,
    target: node.target ?? '',
    inputs: node.inputs ?? {},
    artifact_inputs: node.artifact_inputs ?? [],
    artifact_outputs: node.artifact_outputs ?? [],
    title: node.title ?? '',
    boundary: node.boundary ?? null,
    agent: normalizeUserDagAgentConfig(node.agent),
  };
}

function normalizeComparableUserDag(spec: UserDag): UserDag {
  return {
    ...spec,
    version: spec.version ?? 1,
    description: spec.description ?? '',
    input_schema: spec.input_schema ?? {},
    artifacts: spec.artifacts ?? {},
    nodes: (spec.nodes ?? []).map(normalizeUserDagNode),
    edges: spec.edges ?? [],
    metadata: spec.metadata ?? {},
  };
}

function stableJsonValue(value: unknown): string {
  return JSON.stringify(sortJsonValue(value));
}

function sortJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortJsonValue);
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return Object.keys(record).sort().reduce<Record<string, unknown>>((result, key) => {
      const item = record[key];
      if (item !== undefined) result[key] = sortJsonValue(item);
      return result;
    }, {});
  }
  return value;
}

function savedDagMatchesEditorSpec(saved: SavedDag, spec: UserDag): boolean {
  return saved.name === spec.name
    && saved.description === (spec.description ?? '')
    && stableJsonValue(normalizeComparableUserDag(saved.spec)) === stableJsonValue(normalizeComparableUserDag(spec));
}

function normalizeUserDagAgentConfig(agent?: UserDagAgentConfig | null): UserDagAgentConfig | undefined {
  if (!agent) return undefined;
  const capabilities = Array.isArray(agent.capabilities) ? agent.capabilities : undefined;
  const skills = Array.isArray(agent.skills) ? agent.skills : undefined;
  if (capabilities === undefined && skills === undefined) return undefined;
  return {
    capabilities: capabilities ?? [],
    skills: skills ?? [],
  };
}

function isAgentTarget(target: string): boolean {
  return target.trim().startsWith('agent.');
}

function isCustomAgentScope(agent?: UserDagAgentConfig | null): boolean {
  return Boolean(agent && (Array.isArray(agent.capabilities) || Array.isArray(agent.skills)));
}

function capabilityKindFromTarget(target: string): CapabilityKind {
  const prefix = target.split('.', 1)[0] as CapabilityKind;
  return capabilityKinds.includes(prefix) ? prefix : 'tool';
}

function riskFromTarget(target: string): RiskLevel {
  return target.startsWith('agent.') ? 'medium' : 'low';
}

function dagNodeFromUserNode(node: UserDagNode): DagNode {
  const normalized = normalizeUserDagNode(node);
  return normalizeNode({
    id: normalized.id,
    title: normalized.title,
    payload: {
      type: 'capability',
      invocation: {
        capability_id: normalized.target,
        kind: capabilityKindFromTarget(normalized.target),
        arguments: normalized.inputs ?? {},
        boundary: normalized.boundary ?? {
          allowed_paths: ['.'],
        },
        risk: riskFromTarget(normalized.target),
      },
    },
    inputs: normalized.artifact_inputs ?? [],
    outputs: normalized.artifact_outputs ?? [],
    status: 'planned',
  });
}

function userNodeFromDagNode(node: DagNode & { payload: CapabilityNodePayload }): UserDagNode {
  const normalized = normalizeNode(node) as DagNode & { payload: CapabilityNodePayload };
  const invocation = normalizeInvocation(normalized.payload.invocation);
  return {
    id: normalized.id,
    title: normalized.title ?? '',
    target: invocation.capability_id,
    inputs: invocation.arguments ?? {},
    artifact_inputs: normalized.inputs ?? [],
    artifact_outputs: normalized.outputs ?? [],
    boundary: invocation.boundary ?? null,
  };
}

function createEmptyUserDag(): UserDag {
  return {
    id: `custom_dag_${Date.now()}`,
    name: 'Untitled DAG',
    version: 1,
    description: '',
    input_schema: {},
    artifacts: {},
    nodes: [],
    edges: [],
    metadata: {},
  };
}

function runtimeDagFromUserDag(spec: UserDag): Dag {
  return {
    dag_id: spec.id,
    task_id: spec.id,
    version: spec.version ?? 1,
    status: 'draft',
    nodes: (spec.nodes ?? []).map(dagNodeFromUserNode),
    edges: spec.edges ?? [],
  };
}

function runtimeDagFromUnknown(value: unknown): Dag | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  if (!Array.isArray(item.nodes) || !Array.isArray(item.edges)) return null;
  const dagId = typeof item.dag_id === 'string' ? item.dag_id : typeof item.task_id === 'string' ? item.task_id : 'dag';
  const status = typeof item.status === 'string' ? item.status as Dag['status'] : 'draft';
  return {
    dag_id: dagId,
    task_id: typeof item.task_id === 'string' ? item.task_id : dagId,
    version: typeof item.version === 'number' ? item.version : 1,
    status,
    nodes: item.nodes.map((node) => normalizeNode(node as DagNode)),
    edges: item.edges as DagEdge[],
  };
}

function userDagFromRuntimeDag(spec: UserDag, dag: Dag): UserDag {
  const agentConfigByNodeId = new Map(
    (spec.nodes ?? []).map((node) => [node.id, normalizeUserDagAgentConfig(node.agent)]),
  );
  const nodes = dag.nodes.filter(isCapabilityNode).map((node) => {
    const userNode = userNodeFromDagNode(node);
    const agent = isAgentTarget(userNode.target) ? agentConfigByNodeId.get(userNode.id) : undefined;
    return { ...userNode, agent };
  });
  const nodeIds = new Set(nodes.map((node) => node.id));
  return {
    ...spec,
    version: spec.version ?? 1,
    nodes,
    edges: pruneEdgesToNodeIds(dag.edges, nodeIds),
  };
}

function validateUserDagDraft(spec: UserDag): string | null {
  if (!spec.id.trim()) return 'DAG id is required.';
  if (!/^[A-Za-z][A-Za-z0-9_-]*$/.test(spec.id)) return 'DAG id must start with a letter and use letters, numbers, _ or -.';
  if (!spec.name.trim()) return 'DAG name is required.';
  const nodeIds = new Set<string>();
  for (const node of spec.nodes) {
    if (!node.id.trim()) return 'Every node needs an id.';
    if (!/^[A-Za-z][A-Za-z0-9_-]*$/.test(node.id)) return `Node '${node.id}' has an invalid id.`;
    if (nodeIds.has(node.id)) return `Node '${node.id}' is duplicated.`;
    nodeIds.add(node.id);
    const target = node.target.trim();
    const kind = capabilityKindFromTarget(target);
    if (!target) return `Node '${node.id}' needs a target.`;
    if (kind === 'skill') return `Node '${node.id}' cannot target a skill directly; use an agent target.`;
    if (node.agent && !isAgentTarget(target)) return `Node '${node.id}' has agent settings but does not target an agent.`;
  }
  for (const edge of spec.edges) {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) {
      return `Edge ${edge.source} -> ${edge.target} references a missing node.`;
    }
  }
  return null;
}

type ParsedDagRunInput =
  | { ok: true; hasInput: false }
  | { ok: true; hasInput: true; value: unknown }
  | { ok: false; message: string };

function parseDagRunInput(value: string): ParsedDagRunInput {
  const trimmed = value.trim();
  if (!trimmed) return { ok: true, hasInput: false };
  try {
    return { ok: true, hasInput: true, value: JSON.parse(trimmed) };
  } catch {
    return { ok: false, message: 'DAG input must be valid JSON.' };
  }
}

function dagValidationIssueMessage(issues: DagValidationIssue[]): string {
  const issue = issues.find((item) => item.severity === 'error') ?? issues[0];
  if (!issue) return 'DAG validation failed.';
  const owner = issue.node_id ? `节点 ${issue.node_id}: ` : '';
  return `${owner}${issue.message}`;
}

function capabilityKindLabel(kind: CapabilityKind): string {
  if (kind === 'agent') return 'Agent';
  if (kind === 'mcp') return 'MCP';
  if (kind === 'tool') return 'Tool';
  if (kind === 'memory') return 'Memory';
  return 'Skill';
}

function capabilityOptionGroups(capabilities: CapabilityDefinition[]): Array<{ kind: CapabilityKind; label: string; items: CapabilityDefinition[] }> {
  const order: CapabilityKind[] = ['agent', 'tool', 'mcp', 'memory', 'skill'];
  return order
    .map((kind) => ({
      kind,
      label: capabilityKindLabel(kind),
      items: capabilities.filter((capability) => capability.kind === kind),
    }))
    .filter((group) => group.items.length);
}

function capabilityRisk(capability?: CapabilityDefinition): RiskLevel {
  return capability?.policy?.risk ?? 'low';
}

type ChatTarget = 'auto' | 'tool' | 'dag';
type ChatScopeMode = 'all' | 'custom';
type OrchestrationMode = 'dynamic' | 'static';
type OrchestrationSessionKind = OrchestrationSession['kind'];
type OrchestrationContext = {
  conversation: ApiConversation;
  session: OrchestrationSession;
  request: { projectId?: string | null; conversationId: string };
};
type ToolDirectoryTab = 'tools' | 'skills' | 'mcp';
type ChatWorkspaceSub = 'conversations' | 'projects';
type ProjectDraft = { name: string; slug: string; description: string };
type ProjectFileDialogKind = 'folder' | 'rename' | 'delete';
type TextFilePreview = RunArtifactPreview | ProjectFilePreview;
type AgentManagementSub = 'profiles' | 'presets';
type SystemManagementSub = 'models' | 'onlyoffice';
type TokenChannel = 'reasoning' | 'content';
type DynamicChatMessage = ChatStreamMessage & { timelineOrder: number };
type DynamicTraceLogEvent = TraceLogEvent & { timelineOrder: number };
type StaticDagEditorDraft = {
  spec: UserDag;
  savedDagId: string | null;
  revision: number | null;
  layout: Record<string, unknown>;
  layoutPositions: Record<string, XYPosition>;
};
type SavedDagView = {
  savedDagId: string;
  projectId: string | null;
  name: string;
  description: string;
  revision: number;
  spec: UserDag;
  layout: Record<string, unknown>;
  layoutPositions: Record<string, XYPosition>;
};

interface QueuedAssistantToken {
  channel: TokenChannel;
  content: string;
}

interface DesignDagNodeData {
  nodeId: string;
  title: string;
  detail: string;
  kind: string;
  risk: RiskLevel;
  reviewAttention: boolean;
  status: string;
}

function graphFromDag(dag: Dag, layoutPositions: Record<string, XYPosition> = {}): { nodes: Node[]; edges: Edge[] } {
  const depths = nodeDepths(dag);
  const laneCounts = new Map<number, number>();
  const nodes = dag.nodes.map((rawItem) => {
    const item = normalizeNode(rawItem);
    const payload = item.payload;
    const invocation = payload.type === 'capability' || payload.type === 'map' ? payload.invocation : null;
    const reviewInfo = nodeReviewInfo(item);
    const risk = reviewInfo.risk;
    const reviewAttention = reviewInfo.reviewAttention;
    const status = item.status ?? 'planned';
    const depth = depths.get(item.id) ?? 0;
    const lane = laneCounts.get(depth) ?? 0;
    const detail = nodeDisplayDetail(item);
    laneCounts.set(depth, lane + 1);
    return {
      id: item.id,
      position: layoutPositions[item.id] ?? { x: 80 + depth * 300, y: 70 + lane * 170 },
      className: `status-${status}${reviewAttention ? ' review-attention-node' : ''}`,
      data: {
        nodeId: item.id,
        title: nodeDisplayTitle(item),
        detail,
        kind: invocation?.kind ?? item.payload.type,
        risk,
        reviewAttention,
        status,
      },
      type: 'designDag',
      width: 192,
      height: 64,
      handles: [
        { id: 'in', type: 'target' as const, position: Position.Left, x: -4, y: 28, width: 8, height: 8 },
        { id: 'out', type: 'source' as const, position: Position.Right, x: 188, y: 28, width: 8, height: 8 },
      ],
    };
  });
  const edges = dag.edges.map((edge) => ({
    id: `${edge.source}-${edge.target}`,
    source: edge.source,
    sourceHandle: 'out',
    target: edge.target,
    targetHandle: 'in',
    label: edge.reason,
    animated: dag.status === 'running',
    style: { stroke: '#94a3b8', strokeWidth: 1.5 },
  }));
  return { nodes, edges };
}

function nextHorizontalNodePosition(nodes: Node[]): XYPosition {
  if (!nodes.length) return { x: 80, y: 70 };
  const ordered = [...nodes].sort((left, right) => left.position.x - right.position.x);
  const last = ordered[ordered.length - 1];
  const first = ordered[0];
  return {
    x: Math.round(last.position.x + 240),
    y: Math.round(first.position.y),
  };
}

function nodePositionsFromNodes(nodes: Node[]): Record<string, XYPosition> {
  return Object.fromEntries(
    nodes.map((node) => [node.id, {
      x: Math.round(node.position.x),
      y: Math.round(node.position.y),
    }]),
  );
}

function pruneNodePositions(positions: Record<string, XYPosition>, dag: Dag): Record<string, XYPosition> {
  const nodeIds = new Set(dag.nodes.map((node) => node.id));
  return Object.fromEntries(
    Object.entries(positions).filter(([id]) => nodeIds.has(id)),
  );
}

function layoutPositionsFromSavedLayout(layout: Record<string, unknown>): Record<string, XYPosition> {
  const items = Array.isArray(layout.nodes) ? layout.nodes : [];
  const positions: Record<string, XYPosition> = {};
  for (const item of items) {
    if (!item || typeof item !== 'object') continue;
    const node = item as Record<string, unknown>;
    if (typeof node.id !== 'string') continue;
    const x = typeof node.x === 'number' ? node.x : undefined;
    const y = typeof node.y === 'number' ? node.y : undefined;
    if (x === undefined || y === undefined) continue;
    positions[node.id] = { x: Math.round(x), y: Math.round(y) };
  }
  return positions;
}

function savedLayoutWithNodePositions(
  layout: Record<string, unknown>,
  positions: Record<string, XYPosition>,
): Record<string, unknown> {
  return {
    ...layout,
    nodes: Object.entries(positions).map(([id, position]) => ({
      id,
      x: Math.round(position.x),
      y: Math.round(position.y),
    })),
  };
}

function DesignDagNode({ data, selected }: any) {
  const nodeData = data as DesignDagNodeData;
  return (
    <div
      className={selected ? 'orchestration-node-card selected' : 'orchestration-node-card'}
      data-kind={nodeData.kind}
      data-risk={nodeData.risk}
      data-status={nodeData.status}
    >
      <Handle className="orchestration-handle" id="in" position={Position.Left} type="target" />
      <span className="orchestration-node-icon">
        <GitBranch size={15} />
      </span>
      <span className="orchestration-node-copy">
        <strong title={nodeData.title}>{nodeData.title}</strong>
        <em title={nodeData.detail}>{nodeData.detail}</em>
      </span>
      {nodeData.reviewAttention ? <span className={`risk-chip risk-${nodeData.risk}`}>{nodeData.risk}</span> : null}
      <Handle className="orchestration-handle" id="out" position={Position.Right} type="source" />
    </div>
  );
}

const designNodeTypes = {
  designDag: DesignDagNode,
};

function nodeDisplayTitle(node: DagNode): string {
  const title = node.title?.trim();
  if (title) return title;
  if (node.payload.type === 'capability') {
    return node.payload.invocation.capability_id || '未命名节点';
  }
  return node.payload.type === 'start' ? '入口节点' : '未命名节点';
}

function nodeDisplayDetail(node: DagNode): string {
  const payload = node.payload;
  if (payload.type === 'capability') {
    return payload.invocation.capability_id
      ? `${payload.invocation.capability_id} ${JSON.stringify(payload.invocation.arguments)}`
      : 'capability not set';
  }
  if (payload.type === 'map') {
    return payload.invocation.capability_id
      ? `map ${payload.invocation.capability_id} ${JSON.stringify(payload.invocation.arguments)}`
      : 'map capability not set';
  }
  if (payload.type === 'subgraph') {
    return `subgraph ${payload.spec.name || payload.spec.id}`;
  }
  if (payload.type === 'loop') {
    return `loop ${payload.body.name || payload.body.id}`;
  }
  return 'internal start';
}

function isDagConfirmable(dag: Dag): boolean {
  return !['completed', 'failed', 'aborted', 'running', 'awaiting_review', 'rejected'].includes(dag.status);
}

function dynamicDagForPrompt(dag: Dag) {
  return {
    dag_id: dag.dag_id,
    task_id: dag.task_id,
    version: dag.version,
    status: dag.status,
    nodes: (dag.nodes ?? []).map(normalizeNode),
    edges: dag.edges ?? [],
  };
}

function dynamicPromptWithDagContext(prompt: string, dag: Dag): string {
  const trimmed = prompt.trim();
  if (!dag.nodes.length) return trimmed;
  return [
    trimmed,
    '',
    '当前可编辑 DAG 快照如下。请基于这个 DAG 和上面的修改要求继续调整，不要忽略用户在画布中的手动编辑。',
    '',
    '```json',
    JSON.stringify(dynamicDagForPrompt(dag), null, 2),
    '```',
  ].join('\n');
}

function buildDynamicDagMessages(history: DynamicChatMessage[], prompt: string, dag: Dag): ChatStreamMessage[] {
  return [
    ...history.map((message) => ({ role: message.role, content: message.content })),
    { role: 'user', content: dynamicPromptWithDagContext(prompt, dag) },
  ];
}

function isAbortError(value: unknown): boolean {
  return value instanceof Error && value.name === 'AbortError';
}

function finishedRunResultFromEvents(events: ApiRunEvent[]): ApiRunResult | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.event_type !== 'run.finished' && event.payload.type !== 'run.finished') continue;
    const data = recordValue(event.payload.data);
    const result = data ? recordValue(data.result) : null;
    if (!result) continue;
    return {
      output_text: typeof result.output_text === 'string' ? result.output_text : '',
      state: recordValue(result.state) ? result.state as ApiRunState : null,
    };
  }
  return null;
}

function visibleChatContentFromInternalMessage(message: Record<string, unknown>): string {
  const content = message.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map((item) => {
      if (typeof item === 'string') return item;
      const record = recordValue(item);
      if (!record) return '';
      if (typeof record.text === 'string') return record.text;
      if (typeof record.content === 'string') return record.content;
      return '';
    }).filter(Boolean).join('\n');
  }
  return '';
}

function messagesFromPersistedRunResult(result: ApiRunResult, traceSnapshot: TraceLogEvent[]): ChatMessage[] {
  const state = result.state ?? null;
  const dagSnapshot = state?.dag ?? undefined;
  const reviewMessage = state?.pending_review?.message?.trim() ?? '';
  const output = result.output_text.trim();
  const fallbackContent = output || reviewMessage;
  const messages: ChatMessage[] = (state?.internal_messages ?? []).flatMap((message): ChatMessage[] => {
    const role = message.role;
    if (role !== 'user' && role !== 'assistant') return [];
    const content = visibleChatContentFromInternalMessage(message).trim();
    if (!content) return [];
    const timeline: MessageTimelineItem[] = [{ type: 'text', content }];
    return [{
      role: role as ChatMessage['role'],
      kind: 'text' as const,
      content,
      timeline,
    }];
  });
  if (
    fallbackContent
    && !messages.some((message) => message.role === 'assistant' && message.content.trim() === fallbackContent)
  ) {
    const timeline: MessageTimelineItem[] = [{ type: 'text', content: fallbackContent }];
    messages.push({
      role: 'assistant',
      kind: 'text',
      content: fallbackContent,
      timeline,
    });
  }
  let assistantIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'assistant') {
      assistantIndex = index;
      break;
    }
  }
  if (assistantIndex !== -1) {
    const message = messages[assistantIndex];
    const timeline: MessageTimelineItem[] = dagSnapshot
      ? [{ type: 'dag', dag: dagSnapshot }, ...(message.timeline ?? [])]
      : message.timeline ?? [];
    messages[assistantIndex] = {
      ...message,
      timeline,
      dagSnapshot,
      traceSnapshot,
    };
  } else if (dagSnapshot) {
    const timeline: MessageTimelineItem[] = [{ type: 'dag', dag: dagSnapshot }];
    messages.push({
      role: 'assistant',
      kind: 'text',
      content: fallbackContent,
      timeline,
      dagSnapshot,
      traceSnapshot,
    });
  }
  return messages;
}

function artifactPreviewCacheKey(item: WorkbenchArtifactItem): string {
  if (!item.runId || !item.path) return '';
  return `${item.runId}:${item.path}:${item.size ?? 'unknown'}`;
}

export function App() {
  const [activeWorkspace, setActiveWorkspace] = useState<WorkspaceKey>('chat');
  const [dag, setDag] = useState<Dag>(emptyDag);
  const [selectedId, setSelectedId] = useState<string>('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [runState, setRunState] = useState<ApiRunState | null>(null);
  const [draft, setDraft] = useState('');
  const [target, setTarget] = useState<ChatTarget>('auto');
  const [reviewLevel, setReviewLevel] = useState<ReviewLevel>('careful');
  const [chatScopeMode, setChatScopeMode] = useState<ChatScopeMode>('all');
  const [selectedChatCapabilityIds, setSelectedChatCapabilityIds] = useState<string[]>([]);
  const [selectedChatSkillNames, setSelectedChatSkillNames] = useState<string[]>([]);
  const [chatAgentScope, setChatAgentScope] = useState<AgentScopeMode>('none');
  const [selectedChatAgentIds, setSelectedChatAgentIds] = useState<string[]>([]);
  const [capabilityScopeOpen, setCapabilityScopeOpen] = useState(false);
  const [projects, setProjects] = useState<ApiProject[]>([]);
  const [chatSub, setChatSub] = useState<ChatWorkspaceSub>('conversations');
  const [selectedProjectId, setSelectedProjectId] = useState('');
  const [conversations, setConversations] = useState<ApiConversation[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState('');
  const [projectError, setProjectError] = useState<string | null>(null);
  const [projectCreateOpen, setProjectCreateOpen] = useState(false);
  const [projectEditOpen, setProjectEditOpen] = useState(false);
  const [projectDeleteOpen, setProjectDeleteOpen] = useState(false);
  const [projectDraft, setProjectDraft] = useState({ name: '', slug: '', description: '' });
  const [conversationDeleteTargetId, setConversationDeleteTargetId] = useState('');
  const [projectFilePath, setProjectFilePath] = useState('');
  const [projectFiles, setProjectFiles] = useState<ProjectFileItem[]>([]);
  const [projectFilesLoading, setProjectFilesLoading] = useState(false);
  const [projectFilesError, setProjectFilesError] = useState<string | null>(null);
  const [selectedProjectFilePath, setSelectedProjectFilePath] = useState('');
  const [projectFilePreview, setProjectFilePreview] = useState<ProjectFilePreview | null>(null);
  const [projectFilePreviewLoading, setProjectFilePreviewLoading] = useState(false);
  const [projectFilePreviewError, setProjectFilePreviewError] = useState<string | null>(null);
  const [projectFileDialog, setProjectFileDialog] = useState<{ kind: ProjectFileDialogKind; file?: ProjectFileItem } | null>(null);
  const [projectFileDraft, setProjectFileDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [trace, setTrace] = useState<TraceLogEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [navCollapsed, setNavCollapsed] = useState(false);
  const [artifactPanelOpen, setArtifactPanelOpen] = useState(false);
  const [selectedArtifactId, setSelectedArtifactId] = useState('');
  const [runArtifactFiles, setRunArtifactFiles] = useState<RunArtifactFile[]>([]);
  const [runArtifactLoading, setRunArtifactLoading] = useState(false);
  const [runArtifactError, setRunArtifactError] = useState<string | null>(null);
  const [pendingChatUploads, setPendingChatUploads] = useState<File[]>([]);
  const [artifactPreviews, setArtifactPreviews] = useState<Record<string, RunArtifactPreview>>({});
  const [artifactPreviewLoadingId, setArtifactPreviewLoadingId] = useState('');
  const [artifactPreviewError, setArtifactPreviewError] = useState<{ id: string; message: string } | null>(null);
  const [validationEnabled, setValidationEnabled] = useState(false);
  const [validationPending, setValidationPending] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [dagReview, setDagReview] = useState<ReviewEventPayload | null>(null);
  const [dagReviewFeedback, setDagReviewFeedback] = useState('');
  const [capabilityReview, setCapabilityReview] = useState<ReviewEventPayload | null>(null);
  const [capabilityReviewFeedback, setCapabilityReviewFeedback] = useState('');
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const validationRequestIdRef = useRef(0);
  const tokenQueueRef = useRef<QueuedAssistantToken[]>([]);
  const tokenTimerRef = useRef<number | null>(null);
  const tokenDrainResolversRef = useRef<Array<() => void>>([]);
  const contentStreamedRef = useRef(false);
  const streamAbortRef = useRef<AbortController | null>(null);
  const runArtifactRequestRef = useRef(0);
  const conversationHydrationRequestRef = useRef(0);
  const orchestrationHydrationRequestRef = useRef(0);
  const orchestrationHydratedKeyRef = useRef('');
  const conversationsRef = useRef<ApiConversation[]>([]);
  const savedDagsRef = useRef<SavedDag[]>([]);
  const projectFilesRequestRef = useRef(0);
  const projectFilePreviewRequestRef = useRef(0);
  const [capabilities, setCapabilities] = useState<CapabilityDefinition[]>([]);
  const [consoleError, setConsoleError] = useState<string | null>(null);
  const [savedDags, setSavedDags] = useState<SavedDag[]>([]);
  const [editorSavedDagId, setEditorSavedDagId] = useState<string | null>(null);
  const [editorSavedDagProjectId, setEditorSavedDagProjectId] = useState<string | null>(null);
  const [editorSavedDagRevision, setEditorSavedDagRevision] = useState<number | null>(null);
  const [editorSavedDagLayout, setEditorSavedDagLayout] = useState<Record<string, unknown>>({});
  const [editorUserDag, setEditorUserDag] = useState<UserDag>(() => createEmptyUserDag());
  const [editorDag, setEditorDag] = useState<Dag>(() => runtimeDagFromUserDag(editorUserDag));
  const [editorLayoutPositions, setEditorLayoutPositionsState] = useState<Record<string, XYPosition>>({});
  const editorLayoutPositionsRef = useRef<Record<string, XYPosition>>({});
  const [editorDagDrafts, setEditorDagDraftsState] = useState<Record<string, StaticDagEditorDraft>>({});
  const editorDagDraftsRef = useRef<Record<string, StaticDagEditorDraft>>({});
  const [editorSelectedId, setEditorSelectedId] = useState('');
  const [editorTrace, setEditorTrace] = useState<TraceLogEvent[]>([]);
  const [editorRun, setEditorRun] = useState<DagRun | null>(null);
  const [editorRunTimeline, setEditorRunTimeline] = useState<RunTranscriptItem[]>([]);
  const [editorMessage, setEditorMessage] = useState('');
  const [editorRunning, setEditorRunning] = useState(false);
  const editorRunInFlightRef = useRef(false);
  const [editorRunInputText, setEditorRunInputText] = useState('');
  const [editingArtifactId, setEditingArtifactId] = useState('');
  const [orchestrationMode, setOrchestrationMode] = useState<OrchestrationMode>('dynamic');
  const [dynamicPrompt, setDynamicPrompt] = useState('');
  const [dynamicAdjust, setDynamicAdjust] = useState(true);
  const [dynamicDag, setDynamicDag] = useState<Dag>(emptyDag);
  const dynamicDagRef = useRef<Dag>(emptyDag);
  const [dynamicLayoutPositions, setDynamicLayoutPositionsState] = useState<Record<string, XYPosition>>({});
  const dynamicLayoutPositionsRef = useRef<Record<string, XYPosition>>({});
  const [dynamicSelectedId, setDynamicSelectedId] = useState('');
  const [dynamicRunState, setDynamicRunState] = useState<ApiRunState | null>(null);
  const dynamicTimelineOrderRef = useRef(0);
  const [dynamicTrace, setDynamicTrace] = useState<DynamicTraceLogEvent[]>([]);
  const [dynamicMessages, setDynamicMessages] = useState<DynamicChatMessage[]>([]);
  const [dynamicFinalAnswer, setDynamicFinalAnswer] = useState('');
  const [dynamicFinalAnswerOrder, setDynamicFinalAnswerOrder] = useState(0);
  const [dynamicMessage, setDynamicMessage] = useState('');
  const [dynamicMessageOrder, setDynamicMessageOrder] = useState(0);
  const [dynamicRunning, setDynamicRunning] = useState(false);
  const [agentManagementSub, setAgentManagementSub] = useState<AgentManagementSub>('profiles');
  const [systemManagementSub, setSystemManagementSub] = useState<SystemManagementSub>('models');
  const [profiles, setProfiles] = useState<AgentProfile[]>([]);
  const [profileWarnings, setProfileWarnings] = useState<ProfileWarning[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState('');
  const [creatingProfile, setCreatingProfile] = useState(false);
  const [agentPresets, setAgentPresets] = useState<AgentPreset[]>([]);
  const [agentPresetErrors, setAgentPresetErrors] = useState<Record<string, string>>({});
  const [selectedAgentPresetId, setSelectedAgentPresetId] = useState('');
  const [creatingAgentPreset, setCreatingAgentPreset] = useState(false);
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [mcpServers, setMcpServers] = useState<MCPServer[]>([]);
  const [pythonTools, setPythonTools] = useState<PythonToolEntry[]>([]);
  const [models, setModels] = useState<ModelProvider[]>([]);
  const [onlyOfficeSettings, setOnlyOfficeSettings] = useState<OnlyOfficeSettings>(defaultOnlyOfficeSettings);
  const [activeModelId, setActiveModelId] = useState('config');
  const [selectedModelId, setSelectedModelId] = useState('config');
  const [creatingModel, setCreatingModel] = useState(false);
  const [toolsDirectoryTab, setToolsDirectoryTab] = useState<ToolDirectoryTab>('tools');
  const [capabilityCreationIntent, setCapabilityCreationIntent] = useState<ToolDirectoryTab | null>(null);
  const [toolsDirectoryQuery, setToolsDirectoryQuery] = useState('');
  const [selectedToolCapabilityId, setSelectedToolCapabilityId] = useState('');
  const [selectedToolSkillName, setSelectedToolSkillName] = useState('');
  const [selectedToolMcpName, setSelectedToolMcpName] = useState('');
  const [selectedToolMcpToolId, setSelectedToolMcpToolId] = useState('');
  const [selectedSkillDetail, setSelectedSkillDetail] = useState<SkillDetail | null>(null);
  const [selectedSkillFileDetail, setSelectedSkillFileDetail] = useState<SkillFileDetail | null>(null);
  const [skillMessage, setSkillMessage] = useState('');
  const [skillImport, setSkillImport] = useState({ name: '', description: '', category: '', content: '' });
  const setEditorLayoutPositions = useCallback((positions: Record<string, XYPosition>) => {
    editorLayoutPositionsRef.current = positions;
    setEditorLayoutPositionsState(positions);
  }, []);
  const updateEditorDagDrafts = useCallback((updater: (current: Record<string, StaticDagEditorDraft>) => Record<string, StaticDagEditorDraft>) => {
    const nextDrafts = updater(editorDagDraftsRef.current);
    editorDagDraftsRef.current = nextDrafts;
    setEditorDagDraftsState(nextDrafts);
  }, []);
  const setDynamicLayoutPositions = useCallback((positions: Record<string, XYPosition>) => {
    dynamicLayoutPositionsRef.current = positions;
    setDynamicLayoutPositionsState(positions);
  }, []);
  const selectToolMcpResource = useCallback((name: string, toolId: string | null = null) => {
    const selection = nextMcpResourceSelection(name, toolId);
    setSelectedToolMcpName(selection.name);
    setSelectedToolMcpToolId(selection.toolId);
  }, []);
  const nextDynamicTimelineOrder = useCallback(() => {
    dynamicTimelineOrderRef.current += 1;
    return dynamicTimelineOrderRef.current;
  }, []);
  const appendDynamicMessage = useCallback((role: DynamicChatMessage['role'], content: string) => {
    setDynamicMessages((items) => [...items, { role, content, timelineOrder: nextDynamicTimelineOrder() }]);
  }, [nextDynamicTimelineOrder]);
  const setDynamicStatusMessage = useCallback((content: string) => {
    setDynamicMessage(content);
    setDynamicMessageOrder(content ? nextDynamicTimelineOrder() : 0);
  }, [nextDynamicTimelineOrder]);
  const clearDynamicFinalAnswer = useCallback(() => {
    setDynamicFinalAnswer('');
    setDynamicFinalAnswerOrder(0);
  }, []);
  const setOrderedDynamicFinalAnswer = useCallback((content: string) => {
    setDynamicFinalAnswer(content);
    setDynamicFinalAnswerOrder(content ? nextDynamicTimelineOrder() : 0);
  }, [nextDynamicTimelineOrder]);

  const chatScopeLabel = chatCapabilityScopeLabel(
    chatScopeMode,
    selectedChatCapabilityIds.length,
    selectedChatSkillNames.length,
    chatAgentScope,
    selectedChatAgentIds.length,
  );
  const activeRunId = runState?.run_id ?? null;
  const chatArtifacts = useMemo(
    () => buildWorkbenchArtifacts({
      dag,
      runId: activeRunId,
      runFiles: runArtifactFiles,
    }),
    [activeRunId, dag, runArtifactFiles],
  );
  const artifactDrawerOpen = artifactPanelOpen;
  const selectedArtifact = chatArtifacts.find((item) => item.id === selectedArtifactId) ?? null;
  const selectedArtifactPreviewKey = selectedArtifact ? artifactPreviewCacheKey(selectedArtifact) : '';
  const selectedArtifactPreview = selectedArtifactPreviewKey ? artifactPreviews[selectedArtifactPreviewKey] ?? null : null;
  const selectedArtifactPreviewLoading = Boolean(
    selectedArtifactPreviewKey && artifactPreviewLoadingId === selectedArtifactPreviewKey,
  );
  const selectedArtifactPreviewError = (
    selectedArtifactPreviewKey && artifactPreviewError?.id === selectedArtifactPreviewKey
      ? artifactPreviewError.message
      : null
  );
  const selectedProject = useMemo(
    () => projects.find((project) => project.id === selectedProjectId) ?? null,
    [projects, selectedProjectId],
  );
  const selectedConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === selectedConversationId) ?? null,
    [conversations, selectedConversationId],
  );
  const orchestrationConversationIdsKey = useMemo(() => conversations
    .filter((conversation) => conversation.kind === 'dynamic_dag' || conversation.kind === 'static_dag')
    .map((conversation) => `${conversation.kind}:${conversation.project_id ?? ''}:${conversation.id}`)
    .join('|'), [conversations]);
  const selectedChatConversation = selectedConversation?.kind === 'chat' ? selectedConversation : null;
  const conversationDeleteTarget = useMemo(
    () => conversations.find((conversation) => conversation.id === conversationDeleteTargetId) ?? null,
    [conversations, conversationDeleteTargetId],
  );
  const selectedProjectFile = useMemo(
    () => projectFiles.find((file) => file.path === selectedProjectFilePath) ?? null,
    [projectFiles, selectedProjectFilePath],
  );
  const selectedConversationProject = useMemo(
    () => projects.find((project) => project.id === selectedChatConversation?.project_id) ?? null,
    [projects, selectedChatConversation?.project_id],
  );
  const activeConversationContext = selectedChatConversation
    ? { projectId: selectedChatConversation.project_id, conversationId: selectedChatConversation.id }
    : undefined;
  const dynamicGraph = useMemo(() => graphFromDag(dynamicDag, dynamicLayoutPositions), [dynamicDag, dynamicLayoutPositions]);
  const selectedSidebarSkill = useMemo(
    () => skills.find((skill) => skillLookupName(skill) === selectedToolSkillName) ?? skills[0],
    [selectedToolSkillName, skills],
  );

  const refreshRunArtifacts = useCallback(async () => {
    const requestId = runArtifactRequestRef.current + 1;
    runArtifactRequestRef.current = requestId;
    if (!activeRunId) {
      setRunArtifactFiles([]);
      setRunArtifactError(null);
      setRunArtifactLoading(false);
      return;
    }
    setRunArtifactLoading(true);
    setRunArtifactError(null);
    try {
      const payload = await listRunArtifacts(activeRunId);
      if (runArtifactRequestRef.current !== requestId) return;
      setRunArtifactFiles(payload.files);
      setArtifactPreviews({});
      setArtifactPreviewError(null);
      setArtifactPreviewLoadingId('');
    } catch (exc) {
      if (runArtifactRequestRef.current !== requestId) return;
      setRunArtifactFiles([]);
      setRunArtifactError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      if (runArtifactRequestRef.current === requestId) setRunArtifactLoading(false);
    }
  }, [activeRunId]);

  useEffect(() => {
    conversationsRef.current = conversations;
  }, [conversations]);

  useEffect(() => {
    savedDagsRef.current = savedDags;
  }, [savedDags]);

  useEffect(() => {
    void refreshRunArtifacts();
  }, [refreshRunArtifacts]);

  const loadPersistedConversations = useCallback(async (projectItems: ApiProject[]) => {
    const [standaloneConversations, projectConversationGroups] = await Promise.all([
      listConversations(),
      Promise.all(projectItems.map((project) => listProjectConversations(project.id))),
    ]);
    return [
      ...standaloneConversations,
      ...projectConversationGroups.flat(),
    ];
  }, []);

  const refreshConversations = useCallback(async (projectItems: ApiProject[] = projects) => {
    const conversationItems = await loadPersistedConversations(projectItems);
    setConversations(conversationItems);
    return conversationItems;
  }, [loadPersistedConversations, projects]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const items = await listProjects();
      if (cancelled) return;
      setProjects(items);
      setSelectedProjectId((current) => (
        current && items.some((project) => project.id === current)
          ? current
          : items[0]?.id ?? ''
      ));
      const conversationItems = await loadPersistedConversations(items);
      if (cancelled) return;
      setConversations(conversationItems);
      setSelectedConversationId((current) => (
        current && conversationItems.some((conversation) => (
          conversation.id === current
          && conversation.kind === 'chat'
          && !conversation.project_id
        ))
          ? current
          : ''
      ));
      setProjectError(null);
    })()
      .catch((exc) => {
        if (cancelled) return;
        setProjects([]);
        setConversations([]);
        setSelectedProjectId('');
        setSelectedConversationId('');
      setProjectError(exc instanceof Error ? exc.message : String(exc));
    });
    return () => {
      cancelled = true;
    };
  }, [loadPersistedConversations]);

  const refreshProjectFiles = useCallback(async () => {
    const requestId = projectFilesRequestRef.current + 1;
    projectFilesRequestRef.current = requestId;
    if (!selectedProject || activeWorkspace !== 'chat' || chatSub !== 'projects' || selectedChatConversation) {
      setProjectFiles([]);
      setProjectFilesLoading(false);
      return;
    }
    const projectId = selectedProject.id;
    const path = projectFilePath;
    setProjectFilesLoading(true);
    setProjectFilesError(null);
    try {
      const payload = await listProjectFiles(projectId, path);
      if (projectFilesRequestRef.current !== requestId) return;
      setProjectFiles(payload.files);
      setProjectFilePath(payload.path);
    } catch (exc) {
      if (projectFilesRequestRef.current !== requestId) return;
      setProjectFiles([]);
      setProjectFilesError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      if (projectFilesRequestRef.current === requestId) setProjectFilesLoading(false);
    }
  }, [activeWorkspace, chatSub, projectFilePath, selectedChatConversation, selectedProject]);

  useEffect(() => {
    void refreshProjectFiles();
  }, [refreshProjectFiles]);

  useEffect(() => {
    projectFilePreviewRequestRef.current += 1;
    setProjectFilePath('');
    setProjectFiles([]);
    setProjectFilesError(null);
    setSelectedProjectFilePath('');
    setProjectFilePreview(null);
    setProjectFilePreviewLoading(false);
    setProjectFilePreviewError(null);
  }, [selectedProjectId]);

  useEffect(() => {
    setRunArtifactFiles([]);
    setArtifactPreviews({});
    setArtifactPreviewError(null);
    setArtifactPreviewLoadingId('');
    setSelectedArtifactId('');
  }, [activeRunId]);

  // 流式结束后再拉一次产物：run_id 在多轮对话中可能不变，仅靠 activeRunId 变化无法刷新本轮新增的文件。
  const streamingWasActiveRef = useRef(false);
  useEffect(() => {
    if (streamingWasActiveRef.current && !streaming) {
      void refreshRunArtifacts();
    }
    streamingWasActiveRef.current = streaming;
  }, [streaming, refreshRunArtifacts]);

  // 产物从无到有时自动展开抽屉，避免用户手动点开才看到新产物。
  const artifactsPresentRef = useRef(false);
  useEffect(() => {
    const hasArtifacts = chatArtifacts.length > 0;
    if (hasArtifacts && !artifactsPresentRef.current) {
      setArtifactPanelOpen(true);
    }
    artifactsPresentRef.current = hasArtifacts;
  }, [chatArtifacts.length]);

  useEffect(() => {
    if (
      !artifactPanelOpen
      || !selectedArtifact?.runId
      || !selectedArtifact.path
      || !shouldFetchTextArtifactPreview(selectedArtifact)
    ) {
      return;
    }
    const cacheKey = artifactPreviewCacheKey(selectedArtifact);
    if (!cacheKey || artifactPreviews[cacheKey]) return;
    let cancelled = false;
    setArtifactPreviewLoadingId(cacheKey);
    setArtifactPreviewError(null);
    void previewRunArtifact(selectedArtifact.runId, selectedArtifact.path)
      .then((preview) => {
        if (cancelled) return;
        if (preview.run_id !== selectedArtifact.runId || preview.path !== selectedArtifact.path) return;
        setArtifactPreviews((current) => ({ ...current, [cacheKey]: preview }));
      })
      .catch((exc) => {
        if (cancelled) return;
        setArtifactPreviewError({
          id: cacheKey,
          message: exc instanceof Error ? exc.message : String(exc),
        });
      })
      .finally(() => {
        if (!cancelled) setArtifactPreviewLoadingId('');
      });
    return () => {
      cancelled = true;
    };
  }, [artifactPanelOpen, artifactPreviews, selectedArtifact]);

  const copySelectedArtifact = useCallback(() => {
    const content = selectedArtifactPreview?.content ?? '';
    if (!content || !navigator.clipboard) return;
    void navigator.clipboard.writeText(content);
  }, [selectedArtifactPreview]);

  const selectToolsDirectoryTab = useCallback((tab: ToolDirectoryTab) => {
    setToolsDirectoryTab(tab);
    setCapabilityCreationIntent(null);
  }, []);

  const requestCapabilityCreation = useCallback((tab: ToolDirectoryTab) => {
    setActiveWorkspace('tools');
    setToolsDirectoryTab(tab);
    setCapabilityCreationIntent(tab);
    if (tab === 'mcp') {
      setSelectedToolMcpName('');
    }
  }, []);

  const requestModelCreation = useCallback(() => {
    setActiveWorkspace('system');
    setSystemManagementSub('models');
    setCreatingModel(true);
  }, []);

  const selectAgentManagementSub = useCallback((sub: AgentManagementSub) => {
    setAgentManagementSub(sub);
    if (sub === 'profiles') {
      setCreatingAgentPreset(false);
    } else {
      setCreatingProfile(false);
    }
  }, []);

  const requestProfileCreation = useCallback(() => {
    setActiveWorkspace('agents');
    setAgentManagementSub('profiles');
    setCreatingAgentPreset(false);
    setCreatingProfile(true);
  }, []);

  const requestAgentPresetCreation = useCallback(() => {
    setActiveWorkspace('agents');
    setAgentManagementSub('presets');
    setCreatingProfile(false);
    setCreatingAgentPreset(true);
  }, []);

  const selectedNode = dag.nodes.find((node) => node.id === selectedId) ?? dag.nodes[0];
  const graph = useMemo(() => graphFromDag(dag), [dag]);
  const [nodes, setNodes] = useState<Node[]>(graph.nodes);
  const [edges, setEdges] = useState<Edge[]>(graph.edges);
  const editorGraph = useMemo(() => graphFromDag(editorDag, editorLayoutPositions), [editorDag, editorLayoutPositions]);
  const [editorNodes, setEditorNodes] = useState<Node[]>(editorGraph.nodes);
  const [editorEdges, setEditorEdges] = useState<Edge[]>(editorGraph.edges);
  const editorArtifacts = useMemo(
    () => Object.values(editorUserDag.artifacts ?? {}).sort(compareArtifactsByPath),
    [editorUserDag.artifacts],
  );
  const visibleSavedDags = useMemo(() => savedDags.map((saved) => {
    const runtimeDag = runtimeDagFromUserDag(saved.spec);
    const persistedLayoutPositions = pruneNodePositions(
      layoutPositionsFromSavedLayout(saved.layout),
      runtimeDag,
    );
    if (saved.id === editorSavedDagId) {
      const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
      const layoutPositions = pruneNodePositions(editorLayoutPositionsRef.current, editorDag);
      return {
        savedDagId: saved.id,
        projectId: saved.project_id ?? null,
        name: spec.name || saved.name,
        description: spec.description ?? saved.description,
        revision: editorSavedDagRevision ?? saved.revision,
        spec,
        layout: savedLayoutWithNodePositions(editorSavedDagLayout, layoutPositions),
        layoutPositions,
      };
    }
    const draft = editorDagDrafts[saved.id];
    const draftSpec = draft?.spec ?? saved.spec;
    return {
      savedDagId: saved.id,
      projectId: saved.project_id ?? null,
      name: draftSpec.name || saved.name,
      description: draftSpec.description ?? saved.description,
      revision: draft?.revision ?? saved.revision,
      spec: draftSpec,
      layout: draft?.layout ?? saved.layout,
      layoutPositions: draft?.layoutPositions ?? persistedLayoutPositions,
    };
  }), [
    editorDag,
    editorDagDrafts,
    editorSavedDagId,
    editorSavedDagLayout,
    editorSavedDagRevision,
    editorUserDag,
    savedDags,
  ]);
  const editingArtifact = editingArtifactId ? editorUserDag.artifacts?.[editingArtifactId] ?? null : null;

  const refreshConsoleData = useCallback(async () => {
    setConsoleError(null);
    try {
      const [
        nextCapabilities,
        nextSpecs,
        nextProfiles,
        nextAgents,
        nextSkills,
        nextMcpServers,
        nextPythonTools,
        nextModels,
        nextOnlyOfficeSettings,
      ] = await Promise.all([
        listCapabilities(),
        listSavedDags(),
        listProfiles(),
        listAgents(),
        listSkills(),
        listMcpServers(),
        listPythonTools(),
        listModels(),
        getOnlyOfficeSettings(),
      ]);
      setCapabilities(nextCapabilities);
      setSavedDags(nextSpecs);
      setProfiles(nextProfiles.profiles);
      setProfileWarnings(nextProfiles.warnings);
      setAgentPresets(nextAgents.agents);
      setAgentPresetErrors(nextAgents.errors);
      setSkills(nextSkills);
      setMcpServers(nextMcpServers);
      setPythonTools(nextPythonTools);
      setModels(nextModels.models);
      setOnlyOfficeSettings(nextOnlyOfficeSettings);
      setActiveModelId(nextModels.active_model_id);
      setSelectedProfileId((current) => (
        current && nextProfiles.profiles.some((profile) => profile.id === current)
          ? current
          : nextProfiles.profiles[0]?.id || ''
      ));
      setSelectedAgentPresetId((current) => (
        current && nextAgents.agents.some((preset) => preset.id === current)
          ? current
          : nextAgents.agents[0]?.id || ''
      ));
    } catch (exc) {
      setConsoleError(exc instanceof Error ? exc.message : String(exc));
    }
  }, []);

  const refreshAgentData = useCallback(async (preferredProfileId?: string, preferredAgentPresetId?: string) => {
    const [nextCapabilities, nextProfiles, nextAgents] = await Promise.all([
      listCapabilities(),
      listProfiles(),
      listAgents(),
    ]);
    setCapabilities(nextCapabilities);
    setProfiles(nextProfiles.profiles);
    setProfileWarnings(nextProfiles.warnings);
    setAgentPresets(nextAgents.agents);
    setAgentPresetErrors(nextAgents.errors);
    setSelectedProfileId((current) => {
      if (preferredProfileId && nextProfiles.profiles.some((profile) => profile.id === preferredProfileId)) {
        return preferredProfileId;
      }
      if (current && nextProfiles.profiles.some((profile) => profile.id === current)) return current;
      return nextProfiles.profiles[0]?.id || '';
    });
    setSelectedAgentPresetId((current) => {
      if (preferredAgentPresetId && nextAgents.agents.some((preset) => preset.id === preferredAgentPresetId)) {
        return preferredAgentPresetId;
      }
      if (current && nextAgents.agents.some((preset) => preset.id === current)) return current;
      return nextAgents.agents[0]?.id || '';
    });
  }, []);

  const createManagedProfile = useCallback(async (name: string, content: string) => {
    const profile = await createProfile({ name, content });
    setCreatingProfile(false);
    await refreshAgentData(profile.id);
    return profile;
  }, [refreshAgentData]);

  const updateManagedProfile = useCallback(async (name: string, content: string) => {
    const profile = await updateProfile(name, content);
    await refreshAgentData(profile.id);
    return profile;
  }, [refreshAgentData]);

  const removeManagedProfile = useCallback(async (name: string) => {
    await deleteProfile(name);
    setCreatingProfile(false);
    await refreshAgentData();
  }, [refreshAgentData]);

  const createAgentPreset = useCallback(async (payload: AgentPresetInput) => {
    const preset = await createAgent(payload);
    setCreatingAgentPreset(false);
    await refreshAgentData(undefined, preset.id);
    return preset;
  }, [refreshAgentData]);

  const updateAgentPreset = useCallback(async (name: string, payload: Omit<AgentPresetInput, 'name'>) => {
    const preset = await updateAgent(name, payload);
    await refreshAgentData(undefined, preset.id);
    return preset;
  }, [refreshAgentData]);

  const removeAgentPreset = useCallback(async (name: string) => {
    await deleteAgent(name);
    setCreatingAgentPreset(false);
    await refreshAgentData();
  }, [refreshAgentData]);

  const openSkillDetail = useCallback(async (skill: SkillSummary) => {
    const lookup = skillLookupName(skill);
    setSelectedToolSkillName(lookup);
    setSkillMessage(`Loading ${lookup}...`);
    try {
      const detail = await getSkill(lookup);
      setSelectedSkillDetail(detail);
      setSelectedSkillFileDetail(null);
      setSkillMessage('');
    } catch (exc) {
      setSelectedSkillDetail(null);
      setSelectedSkillFileDetail(null);
      setSkillMessage(exc instanceof Error ? exc.message : String(exc));
    }
  }, []);

  useEffect(() => {
    if (toolsDirectoryTab !== 'skills') return;
    if (!selectedSidebarSkill) {
      setSelectedSkillDetail(null);
      setSelectedSkillFileDetail(null);
      return;
    }
    const lookup = skillLookupName(selectedSidebarSkill);
    if (selectedSkillDetail && skillLookupName(selectedSkillDetail.skill) === lookup) return;
    void openSkillDetail(selectedSidebarSkill);
  }, [openSkillDetail, selectedSidebarSkill, selectedSkillDetail, toolsDirectoryTab]);

  const selectToolSkill = useCallback((name: string) => {
    setSelectedToolSkillName(name);
    setSelectedSkillFileDetail(null);
  }, []);

  const selectSkillFile = useCallback(async (filePath: string | null) => {
    if (!filePath) {
      setSelectedSkillFileDetail(null);
      return;
    }
    const skill = selectedSkillDetail?.skill ?? selectedSidebarSkill;
    if (!skill) return;
    const lookup = skillLookupName(skill);
    setSkillMessage(`Loading ${filePath}...`);
    try {
      const detail = await getSkillFile(lookup, filePath);
      setSelectedSkillFileDetail(detail);
      setSkillMessage('');
    } catch (exc) {
      setSelectedSkillFileDetail(null);
      setSkillMessage(exc instanceof Error ? exc.message : String(exc));
    }
  }, [selectedSidebarSkill, selectedSkillDetail]);

  const loadSkillFile = useCallback(async (file: File | undefined) => {
    if (!file) return;
    if (file.name.toLowerCase().endsWith('.zip')) {
      setSkillMessage('Installing skill package...');
      try {
        const detail = await installSkill({ file });
        setSelectedSkillDetail(detail);
        setSelectedSkillFileDetail(null);
        setSelectedToolSkillName(skillLookupName(detail.skill));
        setCapabilityCreationIntent(null);
        setSkillMessage(`Installed ${skillLookupName(detail.skill)}.`);
        try {
          await refreshConsoleData();
        } catch (exc) {
          setSkillMessage(`Installed ${skillLookupName(detail.skill)}, but refresh failed: ${exc instanceof Error ? exc.message : String(exc)}`);
        }
      } catch (exc) {
        setSkillMessage(exc instanceof Error ? exc.message : String(exc));
      }
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      setSkillImport((current) => ({ ...current, content: String(reader.result || '') }));
      setCapabilityCreationIntent('skills');
    };
    reader.readAsText(file);
  }, [refreshConsoleData]);

  const installSkillDraft = useCallback(async () => {
    setSkillMessage('Installing skill...');
    try {
      const detail = await installSkill({
        content: skillImport.content,
        name: skillImport.name || undefined,
        description: skillImport.description || undefined,
        category: skillImport.category || undefined,
      });
      setSelectedSkillDetail(detail);
      setSelectedSkillFileDetail(null);
      setSelectedToolSkillName(skillLookupName(detail.skill));
      setCapabilityCreationIntent(null);
      setSkillMessage(`Installed ${skillLookupName(detail.skill)}.`);
      try {
        await refreshConsoleData();
      } catch (exc) {
        setSkillMessage(`Installed ${skillLookupName(detail.skill)}, but refresh failed: ${exc instanceof Error ? exc.message : String(exc)}`);
      }
    } catch (exc) {
      setSkillMessage(exc instanceof Error ? exc.message : String(exc));
    }
  }, [refreshConsoleData, skillImport]);

  const removeManagedSkill = useCallback(async () => {
    const skill = selectedSkillDetail?.skill ?? selectedSidebarSkill;
    if (!skill || !isManagedSkill(skill)) return;
    setSkillMessage('Deleting skill...');
    try {
      await deleteSkill(skillLookupName(skill));
      setSelectedSkillDetail(null);
      setSelectedSkillFileDetail(null);
      setSelectedToolSkillName('');
      setSkillMessage(`Deleted ${skillLookupName(skill)}.`);
      try {
        await refreshConsoleData();
      } catch (exc) {
        setSkillMessage(`Deleted ${skillLookupName(skill)}, but refresh failed: ${exc instanceof Error ? exc.message : String(exc)}`);
      }
    } catch (exc) {
      setSkillMessage(exc instanceof Error ? exc.message : String(exc));
    }
  }, [refreshConsoleData, selectedSidebarSkill, selectedSkillDetail]);

  useEffect(() => {
    const requestId = ++validationRequestIdRef.current;
    getValidationStatus()
      .then((enabled) => {
        if (validationRequestIdRef.current === requestId) {
          setValidationEnabled(enabled);
          setValidationError(null);
        }
      })
      .catch((exc) => {
        if (validationRequestIdRef.current === requestId) {
          setValidationError(exc instanceof Error ? exc.message : String(exc));
        }
      });
  }, []);

  useEffect(() => {
    void refreshConsoleData();
  }, [refreshConsoleData]);

  useEffect(() => {
    const enabledIds = new Set(capabilities.filter((capability) => capability.enabled).map((capability) => capability.id));
    setSelectedChatCapabilityIds((items) => items.filter((id) => enabledIds.has(id)));
  }, [capabilities]);

  useEffect(() => {
    setSelectedToolCapabilityId((current) =>
      current && capabilities.some((capability) => capability.id === current)
        ? current
        : capabilities[0]?.id ?? '',
    );
  }, [capabilities]);

  useEffect(() => {
    const availableSkills = new Set(skills.map((skill) => skillLookupName(skill)));
    setSelectedChatSkillNames((items) => items.filter((name) => availableSkills.has(name)));
    setSelectedToolSkillName((current) =>
      current && availableSkills.has(current)
        ? current
        : skills[0] ? skillLookupName(skills[0]) : '',
    );
  }, [skills]);

  useEffect(() => {
    setSelectedChatAgentIds((items) => pruneSelectedAgentIds(items, agentPresets));
  }, [agentPresets]);

  useEffect(() => {
    setSelectedToolMcpName((current) =>
      current && mcpServers.some((server) => server.name === current)
        ? current
        : mcpServers[0]?.name ?? '',
    );
  }, [mcpServers]);

  useEffect(() => {
    const selectedServer = mcpServers.find((server) => server.name === selectedToolMcpName);
    setSelectedToolMcpToolId((current) =>
      resolveSelectedMcpToolId(current, selectedServer?.tools.map((tool) => tool.id) ?? []),
    );
  }, [mcpServers, selectedToolMcpName]);

  useEffect(() => {
    setSelectedModelId((current) =>
      current && models.some((model) => model.id === current)
        ? current
        : activeModelId || models[0]?.id || 'config',
    );
  }, [activeModelId, models]);

  useEffect(() => {
    const element = messageListRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
  }, [messages, streaming]);

  useEffect(() => {
    if (!chatArtifacts.length) {
      setSelectedArtifactId('');
      return;
    }
    setSelectedArtifactId((current) =>
      chatArtifacts.some((item) => item.id === current) ? current : '',
    );
  }, [chatArtifacts]);

  const toggleValidation = async () => {
    if (validationPending) return;
    const requestId = ++validationRequestIdRef.current;
    const next = !validationEnabled;
    setValidationPending(true);
    setValidationError(null);
    try {
      const actual = await apiSetValidation(next);
      if (validationRequestIdRef.current === requestId) {
        setValidationEnabled(actual);
      }
    } catch (exc) {
      if (validationRequestIdRef.current === requestId) {
        setValidationError(exc instanceof Error ? exc.message : String(exc));
      }
    } finally {
      if (validationRequestIdRef.current === requestId) {
        setValidationPending(false);
      }
    }
  };

  const syncDag = useCallback((nextDag: Dag) => {
    setDag(nextDag);
    const nextGraph = graphFromDag(nextDag);
    setNodes(nextGraph.nodes);
    setEdges(nextGraph.edges);
    if (!nextDag.nodes.some((node) => node.id === selectedId)) {
      setSelectedId(nextDag.nodes[0]?.id ?? '');
    }
  }, [selectedId]);

  const syncEditorDag = useCallback((nextDag: Dag, layoutPositions?: Record<string, XYPosition>) => {
    setEditorDag(nextDag);
    const nextPositions = pruneNodePositions(layoutPositions ?? editorLayoutPositionsRef.current, nextDag);
    setEditorLayoutPositions(nextPositions);
    const nextGraph = graphFromDag(nextDag, nextPositions);
    setEditorNodes(nextGraph.nodes);
    setEditorEdges(nextGraph.edges);
    setEditorSelectedId((current) =>
      nextDag.nodes.some((node) => node.id === current) ? current : nextDag.nodes[0]?.id ?? '',
    );
  }, [setEditorLayoutPositions]);

  const setEditorUserDagAndRuntimeDag = useCallback((
    spec: UserDag,
    layoutPositions: Record<string, XYPosition> = {},
    saved: {
      savedDagId?: string | null;
      projectId?: string | null;
      revision?: number | null;
      layout?: Record<string, unknown>;
    } = {},
  ) => {
    const normalizedSpec = {
      ...spec,
      version: spec.version ?? 1,
      description: spec.description ?? '',
      input_schema: spec.input_schema ?? {},
      artifacts: spec.artifacts ?? {},
      nodes: (spec.nodes ?? []).map(normalizeUserDagNode),
      edges: spec.edges ?? [],
      metadata: spec.metadata ?? {},
    };
    setEditorSavedDagId(saved.savedDagId ?? null);
    setEditorSavedDagProjectId(saved.projectId ?? null);
    setEditorSavedDagRevision(saved.revision ?? null);
    setEditorSavedDagLayout(saved.layout ?? {});
    setEditorUserDag(normalizedSpec);
    syncEditorDag(runtimeDagFromUserDag(normalizedSpec), layoutPositions);
    setEditorTrace([]);
    setEditorRun(null);
    setEditorRunTimeline([]);
    setEditorMessage('');
  }, [syncEditorDag]);

  const patchEditorUserDag = (patch: Partial<UserDag>) => {
    setEditorUserDag((current) => ({
      ...current,
      ...patch,
    }));
    if (patch.id) {
      setEditorDag((current) => ({ ...current, dag_id: patch.id as string, task_id: patch.id as string }));
    }
  };

  const updateEditorDag = (updater: (current: Dag) => Dag) => {
    const nextDag = updater(editorDag);
    syncEditorDag(nextDag);
    setEditorUserDag((current) => userDagFromRuntimeDag(current, nextDag));
  };

  const rememberCurrentEditorDraft = useCallback(() => {
    if (!editorUserDag.id) return;
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    const layoutPositions = pruneNodePositions(editorLayoutPositionsRef.current, editorDag);
    const draftKey = editorSavedDagId ?? spec.id;
    updateEditorDagDrafts((current) => ({
      ...current,
      [draftKey]: {
        spec,
        savedDagId: editorSavedDagId,
        revision: editorSavedDagRevision,
        layout: savedLayoutWithNodePositions(editorSavedDagLayout, layoutPositions),
        layoutPositions,
      },
    }));
  }, [
    editorDag,
    editorSavedDagId,
    editorSavedDagLayout,
    editorSavedDagRevision,
    editorUserDag,
    updateEditorDagDrafts,
  ]);

  const updateLastAssistantText = (updater: (message: ChatMessage) => ChatMessage) => {
    setMessages((items) => {
      const copy = [...items];
      const last = copy[copy.length - 1];
      if (last?.role === 'assistant' && (last.kind ?? 'text') === 'text') {
        copy[copy.length - 1] = updater(last);
        return copy;
      }
      copy.push(updater({ role: 'assistant', kind: 'text', content: '' }));
      return copy;
    });
  };

  const appendAssistantContent = (content: string) => {
    updateLastAssistantText((message) => ({
      ...message,
      content: `${message.content}${content}`,
      timeline: appendTextTimeline(closeReasoningTimeline(message.timeline), content),
    }));
  };

  const appendAssistantReasoning = (content: string) => {
    updateLastAssistantText((message) => ({
      ...message,
      timeline: appendReasoningTimeline(message.timeline, content),
    }));
  };

  const closeAssistantReasoning = () => {
    setMessages((items) => {
      const copy = [...items];
      const last = copy[copy.length - 1];
      if (last?.role !== 'assistant' || (last.kind ?? 'text') !== 'text') return items;
      if (!last.timeline?.some((item) => item.type === 'reasoning' && !item.closed)) return items;
      copy[copy.length - 1] = {
        ...last,
        timeline: closeReasoningTimeline(last.timeline),
      };
      return copy;
    });
  };

  const stopTokenTimer = () => {
    if (tokenTimerRef.current !== null) {
      window.clearInterval(tokenTimerRef.current);
      tokenTimerRef.current = null;
    }
  };

  function beginStreamRequest(): AbortSignal {
    streamAbortRef.current?.abort();
    const controller = new AbortController();
    streamAbortRef.current = controller;
    return controller.signal;
  }

  function clearStreamRequest(signal: AbortSignal) {
    if (streamAbortRef.current?.signal === signal) {
      streamAbortRef.current = null;
    }
  }

  function restoreDagReviewAfterAbort(
    previousDagReview: ReviewEventPayload,
    previousDagReviewFeedback: string,
    previousDag: Dag,
    previousMessages: ChatMessage[],
  ) {
    setDagReview(previousDagReview);
    setDagReviewFeedback(previousDagReviewFeedback);
    setReviewOpen(true);
    syncDag(previousDag);
    setMessages(previousMessages);
  }

  function restoreCapabilityReviewAfterAbort(
    previousCapabilityReview: ReviewEventPayload,
    previousCapabilityReviewFeedback: string,
    previousMessages: ChatMessage[],
  ) {
    setCapabilityReview(previousCapabilityReview);
    setCapabilityReviewFeedback(previousCapabilityReviewFeedback);
    setMessages(previousMessages);
  }

  const resolveTokenDrain = () => {
    const resolvers = tokenDrainResolversRef.current;
    tokenDrainResolversRef.current = [];
    resolvers.forEach((resolve) => resolve());
  };

  const flushTokenQueue = () => {
    const next = tokenQueueRef.current.shift();
    if (!next) {
      stopTokenTimer();
      resolveTokenDrain();
      return;
    }

    const chunk = next.content.slice(0, 14);
    const rest = next.content.slice(14);
    if (rest) {
      tokenQueueRef.current.unshift({ ...next, content: rest });
    }
    if (next.channel === 'reasoning') {
      appendAssistantReasoning(chunk);
    } else {
      appendAssistantContent(chunk);
    }
  };

  const flushQueuedTokensNow = () => {
    const pending = tokenQueueRef.current;
    tokenQueueRef.current = [];
    stopTokenTimer();
    resolveTokenDrain();
    pending.forEach((item) => {
      if (item.channel === 'reasoning') {
        appendAssistantReasoning(item.content);
      } else {
        appendAssistantContent(item.content);
      }
    });
  };

  const ensureTokenTimer = () => {
    if (tokenTimerRef.current !== null) return;
    tokenTimerRef.current = window.setInterval(flushTokenQueue, 24);
  };

  const enqueueAssistantToken = (channel: TokenChannel, content: string) => {
    if (!content) return;
    const shouldFlushImmediately = tokenQueueRef.current.length === 0 && tokenTimerRef.current === null;
    tokenQueueRef.current.push({ channel, content });
    if (shouldFlushImmediately) {
      flushTokenQueue();
    }
    ensureTokenTimer();
  };

  const enqueueReasoningToken = (content: string) => {
    enqueueAssistantToken('reasoning', content);
  };

  const enqueueContentToken = (content: string) => {
    if (!content) return;
    contentStreamedRef.current = true;
    enqueueAssistantToken('content', content);
  };

  const enqueueFinalAnswer = (finalAnswer: string) => {
    if (!finalAnswer) return;
    tokenQueueRef.current.push({ channel: 'content', content: finalAnswer });
    ensureTokenTimer();
  };

  const enqueueFinalAnswerIfMissing = (finalAnswer: string) => {
    if (contentStreamedRef.current) return;
    enqueueFinalAnswer(finalAnswer);
  };

  const waitForTokenQueue = () => {
    if (tokenQueueRef.current.length === 0 && tokenTimerRef.current === null) {
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => {
      tokenDrainResolversRef.current.push(resolve);
    });
  };

  const attachDagToLastAssistant = (nextDag: Dag) => {
    setMessages((items) => upsertDagMessageTimeline(items, nextDag).map((message) => {
      const hasDag = message.timeline?.some(
        (item) => item.type === 'dag' && (item.dag.task_id || item.dag.dag_id) === (nextDag.task_id || nextDag.dag_id),
      );
      return hasDag ? { ...message, dagSnapshot: nextDag } : message;
    }));
  };

  const shouldOpenDagReview = (nextDag: Dag, pendingReview?: unknown) =>
    Boolean(pendingReview) || nextDag.status === 'review_required';

  const handlePendingReview = useCallback((pendingReview?: ReviewEventPayload | null) => {
    if (!pendingReview) return;
    if (pendingReview.kind === 'capability_review') {
      setCapabilityReviewFeedback('');
      setCapabilityReview(pendingReview as ReviewEventPayload);
      return;
    }
    setDagReviewFeedback('');
    setDagReview(pendingReview);
  }, []);

  const applyPersistedRunResult = useCallback((result: ApiRunResult) => {
    const nextState = result.state ?? null;
    const nextDag = nextState?.dag ?? null;
    const nextReview = nextState?.pending_review ?? null;
    const nextTrace = nextState?.trace ? mapRunTrace(nextState.trace) : [];
    setRunState(nextState);
    setTrace(nextTrace);
    setMessages(messagesFromPersistedRunResult(result, nextTrace));
    setError(null);
    setDagReview(null);
    setDagReviewFeedback('');
    setCapabilityReview(null);
    setCapabilityReviewFeedback('');
    if (nextDag) {
      syncDag(nextDag);
      setReviewOpen(shouldOpenDagReview(nextDag, nextReview));
    } else {
      syncDag(emptyDag);
      setReviewOpen(false);
    }
    handlePendingReview(nextReview);
    contentStreamedRef.current = Boolean(result.output_text.trim());
    tokenQueueRef.current = [];
    stopTokenTimer();
  }, [handlePendingReview, syncDag]);

  const hydrateConversationSnapshot = useCallback(async (conversation: ApiConversation) => {
    if (!conversation.last_run_id) return;
    const requestId = conversationHydrationRequestRef.current + 1;
    conversationHydrationRequestRef.current = requestId;
    try {
      const events = await listRunEvents(conversation.last_run_id);
      if (conversationHydrationRequestRef.current !== requestId) return;
      const result = finishedRunResultFromEvents(events);
      if (!result) return;
      applyPersistedRunResult(result);
    } catch (exc) {
      if (conversationHydrationRequestRef.current !== requestId) return;
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, [applyPersistedRunResult]);

  useEffect(() => {
    if (!selectedChatConversation?.last_run_id || streaming || messages.length || runState) return;
    void hydrateConversationSnapshot(selectedChatConversation);
  }, [
    hydrateConversationSnapshot,
    messages.length,
    runState,
    selectedChatConversation,
    selectedChatConversation?.last_run_id,
    streaming,
  ]);

  const appendTrace = (event: Omit<TraceLogEvent, 'id' | 'timestamp'>): TraceLogEvent => {
    const timestamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const nextEvent = { ...event, id: createUiId('trace'), timestamp };
    setTrace((items) => [...items, nextEvent]);
    return nextEvent;
  };

  const appendRuntimeTrace = (event: TraceLogEvent) => {
    setTrace((items) => [...items, event]);
    updateLastAssistantText((message) => ({
      ...message,
      traceSnapshot: [...(message.traceSnapshot ?? []), event],
    }));
  };

  const appendValidationFeedback = (event: ValidationFeedbackEvent) => {
    flushQueuedTokensNow();
    closeAssistantReasoning();
    updateLastAssistantText((message) => ({
      ...message,
      timeline: appendValidationTimeline(message.timeline, event),
    }));
  };

  const appendValidating = () => {
    flushQueuedTokensNow();
    closeAssistantReasoning();
    updateLastAssistantText((message) => ({
      ...message,
      timeline: appendValidatingTimeline(message.timeline),
    }));
  };

  const appendCapabilityMessage = (event: CapabilityStreamEvent) => {
    if (event.type === 'capability.call.completed' && event.content?.startsWith('[PENDING_REVIEW]')) return;
    flushQueuedTokensNow();
    closeAssistantReasoning();
    updateLastAssistantText((message) => {
      const capabilityEvents = [...(message.capabilityEvents ?? []), event];
      const timeline = [...(message.timeline ?? [])];
      if (event.type === 'capability.call.completed' || event.type === 'capability.call.failed') {
        const idx = findMatchingCapabilityCall(timeline, event.invocation_id);
        if (idx !== -1) {
          const item = timeline[idx] as { type: 'capability'; event: CapabilityStreamEvent; result?: CapabilityStreamEvent };
          timeline[idx] = { ...item, result: event };
          return { ...message, capabilityEvents, timeline };
        }
      }
      timeline.push({ type: 'capability', event });
      return { ...message, capabilityEvents, timeline };
    });
  };

  const onNodesChange = useCallback((changes: NodeChange[]) => setNodes((nds) => applyNodeChanges(changes, nds)), []);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => setEdges((eds) => applyEdgeChanges(changes, eds)), []);
  const onEditorNodesChange = useCallback((changes: NodeChange[]) => {
    setEditorNodes((nds) => {
      const next = applyNodeChanges(changes, nds);
      setEditorLayoutPositions(nodePositionsFromNodes(next));
      return next;
    });
  }, [setEditorLayoutPositions]);
  const onEditorEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEditorEdges((eds) => {
      const next = applyEdgeChanges(changes, eds);
      const nextDagEdges = next
        .filter((edge) => edge.source && edge.target)
        .map((edge) => ({
          source: edge.source,
          target: edge.target,
          reason: 'User dependency.',
        }));
      setEditorDag((current) => ({ ...current, edges: nextDagEdges }));
      setEditorUserDag((current) => userDagFromRuntimeDag(current, { ...editorDag, edges: nextDagEdges }));
      return next;
    });
  }, [editorDag]);
  const onEditorConnect = useCallback((connection: Connection) => {
    if (!connection.source || !connection.target || connection.source === connection.target) return;
    const nextEdge = {
      source: connection.source,
      target: connection.target,
      reason: 'User dependency.',
    };
    setEditorEdges((eds) => addEdge({ ...connection, id: `${connection.source}-${connection.target}` }, eds));
    updateEditorDag((current) => ({
      ...current,
      edges: [
        ...current.edges.filter((edge) => !(edge.source === nextEdge.source && edge.target === nextEdge.target)),
        nextEdge,
      ],
    }));
  }, [editorDag]);

  const syncDynamicDag = (nextDag: Dag) => {
    const normalized = {
      ...nextDag,
      nodes: nextDag.nodes.map(normalizeNode),
      edges: nextDag.edges ?? [],
    };
    dynamicDagRef.current = normalized;
    setDynamicDag(normalized);
    setDynamicLayoutPositions(pruneNodePositions(dynamicLayoutPositionsRef.current, normalized));
    setDynamicSelectedId((current) => normalized.nodes.some((node) => node.id === current) ? current : '');
  };

  function preserveDynamicDagEdges(nextDag: Dag): Dag {
    const nextEdges = nextDag.edges ?? [];
    if (nextEdges.length || !dynamicDagRef.current.edges.length) return nextDag;
    const nodeIds = new Set(nextDag.nodes.map((node) => node.id));
    const preservedEdges = dynamicDagRef.current.edges.filter(
      (edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target),
    );
    return preservedEdges.length ? { ...nextDag, edges: preservedEdges } : nextDag;
  }

  const hydrateOrchestrationConversation = useCallback(async (conversation: ApiConversation) => {
    const requestId = orchestrationHydrationRequestRef.current + 1;
    orchestrationHydrationRequestRef.current = requestId;
    try {
      const session = await getOrchestrationSessionByConversation(conversation.id);
      if (orchestrationHydrationRequestRef.current !== requestId || !session) return;
      const selectedNodeId = typeof session.ui_state?.selectedNodeId === 'string'
        ? session.ui_state.selectedNodeId
        : '';
      if (session.kind === 'dynamic_dag') {
        const draftDag = runtimeDagFromUnknown(session.draft_dag);
        if (draftDag) syncDynamicDag(draftDag);
        if (selectedNodeId) setDynamicSelectedId(selectedNodeId);
        if (conversation.last_run_id) {
          const events = await listRunEvents(conversation.last_run_id);
          if (orchestrationHydrationRequestRef.current !== requestId) return;
          const result = finishedRunResultFromEvents(events);
          const nextState = result?.state ?? null;
          setDynamicRunState(nextState);
          if (nextState?.dag) syncDynamicDag(preserveDynamicDagEdges(nextState.dag));
          if (nextState?.trace) {
            setDynamicTrace(mapRunTrace(nextState.trace).map((event) => ({
              ...event,
              timelineOrder: nextDynamicTimelineOrder(),
            })));
          }
          if (result?.output_text) setOrderedDynamicFinalAnswer(result.output_text);
        }
        return;
      }

      let saved = session.saved_dag_id
        ? savedDagsRef.current.find((item) => item.id === session.saved_dag_id) ?? null
        : null;
      if (session.saved_dag_id && !saved) {
        const latestSavedDags = await listSavedDags();
        if (orchestrationHydrationRequestRef.current !== requestId) return;
        savedDagsRef.current = latestSavedDags;
        setSavedDags(latestSavedDags);
        saved = latestSavedDags.find((item) => item.id === session.saved_dag_id) ?? null;
      }
      const draftDag = runtimeDagFromUnknown(session.draft_dag);
      const baseSpec = saved?.spec ?? createEmptyUserDag();
      const baseLayout = saved?.layout ?? {};
      const baseLayoutPositions = saved
        ? pruneNodePositions(layoutPositionsFromSavedLayout(saved.layout), runtimeDagFromUserDag(saved.spec))
        : {};
      const nextSpec = draftDag ? userDagFromRuntimeDag(baseSpec, draftDag) : baseSpec;
      setEditorUserDagAndRuntimeDag(nextSpec, baseLayoutPositions, saved ? {
        savedDagId: saved.id,
        projectId: saved.project_id ?? null,
        revision: saved.revision,
        layout: baseLayout,
      } : {});
      if (draftDag) syncEditorDag(draftDag, baseLayoutPositions);
      if (selectedNodeId) setEditorSelectedId(selectedNodeId);
      if (conversation.last_run_id) {
        const events = await listRunEvents(conversation.last_run_id);
        if (orchestrationHydrationRequestRef.current !== requestId) return;
        const result = finishedRunResultFromEvents(events);
        const nextState = result?.state ?? null;
        if (nextState?.trace) {
          const traceEvents = mapRunTrace(nextState.trace);
          setEditorTrace(traceEvents);
          setEditorRunTimeline(runTranscriptFromTraceEvents(traceEvents));
        }
        if (nextState?.dag && nextState.trace && nextState.run_id) {
          setEditorRun({
            run_id: nextState.run_id,
            spec_id: nextState.spec_id ?? null,
            workspace_path: nextState.workspace_path ?? '',
            dag: nextState.dag,
            trace: nextState.trace,
            status: dagRunStatus(nextState.status),
          });
          syncEditorDag(nextState.dag, baseLayoutPositions);
        }
      }
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      if (conversation.kind === 'dynamic_dag') setDynamicStatusMessage(message);
      else setEditorMessage(message);
    }
  }, [
    nextDynamicTimelineOrder,
    setEditorUserDagAndRuntimeDag,
    setOrderedDynamicFinalAnswer,
    syncEditorDag,
  ]);

  useEffect(() => {
    if (activeWorkspace !== 'orchestration' || dynamicRunning || editorRunning) return;
    const kind: ApiConversation['kind'] = orchestrationMode === 'dynamic' ? 'dynamic_dag' : 'static_dag';
    const conversationItems = conversationsRef.current;
    const current = selectedConversationId
      ? conversationItems.find((conversation) => conversation.id === selectedConversationId && conversation.kind === kind) ?? null
      : null;
    const preferred = current
      ?? conversationItems.find((conversation) => (
        conversation.kind === kind
        && (selectedProjectId ? conversation.project_id === selectedProjectId : !conversation.project_id)
      ))
      ?? conversationItems.find((conversation) => conversation.kind === kind)
      ?? null;
    if (!preferred) return;
    const hydrateKey = `${kind}:${preferred.id}`;
    if (orchestrationHydratedKeyRef.current === hydrateKey) return;
    if (preferred.id !== selectedConversationId) {
      setSelectedConversationId(preferred.id);
      if (preferred.project_id) setSelectedProjectId(preferred.project_id);
      return;
    }
    orchestrationHydratedKeyRef.current = hydrateKey;
    void hydrateOrchestrationConversation(preferred);
  }, [
    activeWorkspace,
    dynamicRunning,
    editorRunning,
    hydrateOrchestrationConversation,
    orchestrationConversationIdsKey,
    orchestrationMode,
    selectedConversationId,
    selectedProjectId,
  ]);

  const updateDynamicDag = (updater: (current: Dag) => Dag) => {
    setDynamicDag((current) => {
      const nextDag = updater(current);
      const normalized = {
        ...nextDag,
        nodes: nextDag.nodes.map(normalizeNode),
        edges: nextDag.edges ?? [],
      };
      dynamicDagRef.current = normalized;
      return normalized;
    });
  };

  const onDynamicNodesChange = useCallback((changes: NodeChange[]) => {
    const next = applyNodeChanges(changes, dynamicGraph.nodes);
    setDynamicLayoutPositions(nodePositionsFromNodes(next));
  }, [dynamicGraph.nodes, setDynamicLayoutPositions]);

  const onDynamicEdgesChange = useCallback((changes: EdgeChange[]) => {
    const next = applyEdgeChanges(changes, dynamicGraph.edges);
    const nextDagEdges = next
      .filter((edge) => edge.source && edge.target)
      .map((edge) => ({
        source: edge.source,
        target: edge.target,
        reason: 'User dependency.',
      }));
    updateDynamicDag((current) => ({ ...current, status: 'draft', edges: nextDagEdges }));
  }, [dynamicGraph.edges]);

  const onDynamicConnect = useCallback((connection: Connection) => {
    if (!connection.source || !connection.target || connection.source === connection.target) return;
    const nextEdge = {
      source: connection.source,
      target: connection.target,
      reason: 'User dependency.',
    };
    updateDynamicDag((current) => ({
      ...current,
      status: 'draft',
      edges: [
        ...current.edges.filter((edge) => !(edge.source === nextEdge.source && edge.target === nextEdge.target)),
        nextEdge,
      ],
    }));
  }, []);

  const onAddDynamicNode = (capability?: CapabilityDefinition, position?: XYPosition) => {
    const selectedCapability = capability ?? capabilities.find((item) => item.enabled);
    const id = uniqueNodeId(dynamicDag);
    const nodePosition = position ?? nextHorizontalNodePosition(dynamicGraph.nodes);
    setDynamicLayoutPositions({
      ...dynamicLayoutPositionsRef.current,
      [id]: nodePosition,
    });
    updateDynamicDag((current) => ({
      ...current,
      status: 'draft',
      nodes: [
        ...current.nodes,
        normalizeNode({
          id,
          title: selectedCapability ? capabilityDisplayName(selectedCapability) : '未命名节点',
          payload: {
            type: 'capability',
            invocation: {
              capability_id: selectedCapability?.id ?? '',
              kind: selectedCapability?.kind ?? 'tool',
              arguments: ensureSchemaArguments({}, selectedCapability?.parameters),
              boundary: {
                allowed_paths: ['.'],
              },
              risk: capabilityRisk(selectedCapability),
            },
          },
          status: 'planned',
        }),
      ],
    }));
    setDynamicSelectedId('');
  };

  const onPatchDynamicNode = (nodeId: string, patch: Partial<DagNode>, nextEdges?: DagEdge[]) => {
    updateDynamicDag((current) => {
      const updatedNodes = current.nodes.map((node) => {
        if (node.id !== nodeId) return node;
        const merged = normalizeNode({ ...node, ...patch });
        if (patch.id && patch.id !== nodeId) {
          setDynamicSelectedId(patch.id);
          const positions = { ...dynamicLayoutPositionsRef.current };
          positions[patch.id] = positions[nodeId] ?? dynamicGraph.nodes.find((item) => item.id === nodeId)?.position ?? nextHorizontalNodePosition(dynamicGraph.nodes);
          delete positions[nodeId];
          setDynamicLayoutPositions(positions);
        }
        return merged;
      });
      const edgesForRename = patch.id && patch.id !== nodeId
        ? current.edges.map((edge) => ({
            ...edge,
            source: edge.source === nodeId ? patch.id as string : edge.source,
            target: edge.target === nodeId ? patch.id as string : edge.target,
          }))
        : current.edges;
      return {
        ...current,
        status: 'draft',
        nodes: updatedNodes,
        edges: nextEdges ?? edgesForRename,
      };
    });
  };

  const onDeleteDynamicNode = (nodeId: string = dynamicSelectedId) => {
    if (!nodeId) return;
    updateDynamicDag((current) => ({
      ...current,
      status: 'draft',
      nodes: current.nodes.filter((node) => node.id !== nodeId),
      edges: current.edges.filter((edge) => edge.source !== nodeId && edge.target !== nodeId),
    }));
    setDynamicSelectedId('');
  };

  const updateDag = (updater: (current: Dag) => Dag) => {
    syncDag(updater(dag));
  };

  const patchSelected = (patch: Partial<DagNode>, nextEdges?: DagEdge[]) => {
    if (!selectedNode) return;
    updateDag((current) => ({
      ...current,
      status: 'draft',
      nodes: current.nodes.map((node) =>
        node.id === selectedNode.id ? normalizeNode({ ...node, ...patch }) : node,
      ),
      edges: nextEdges ?? current.edges,
    }));
  };

  const addNode = () => {
    const id = uniqueNodeId(dag);
    updateDag((current) => {
      const firstInvocation = current.nodes.find(isCapabilityNode)?.payload.invocation;
      const firstCapability = capabilities.find((item) => item.id === firstInvocation?.capability_id);
      return {
        ...current,
        status: 'draft',
        nodes: [
          ...current.nodes,
          normalizeNode({
            id,
            payload: {
              type: 'capability',
              invocation: {
                capability_id: firstInvocation?.capability_id ?? '',
                kind: firstInvocation?.kind ?? 'tool',
                arguments: ensureSchemaArguments({}, firstCapability?.parameters),
                boundary: {
                  allowed_paths: ['.'],
                },
                risk: 'low',
              },
            },
            status: 'planned',
          }),
        ],
      };
    });
    setSelectedId(id);
  };

  const deleteSelected = () => {
    if (!selectedNode) return;
    updateDag((current) => ({
      ...current,
      status: 'draft',
      nodes: current.nodes.filter((node) => node.id !== selectedNode.id),
      edges: current.edges.filter((edge) => edge.source !== selectedNode.id && edge.target !== selectedNode.id),
    }));
  };

  const newEditorUserDag = () => {
    rememberCurrentEditorDraft();
    setEditorUserDagAndRuntimeDag(createEmptyUserDag(), {});
    setEditorRunInputText('');
  };

  const loadEditorUserDag = (saved: SavedDagView) => {
    rememberCurrentEditorDraft();
    const draft = editorDagDraftsRef.current[saved.savedDagId];
    setEditorUserDagAndRuntimeDag(
      draft?.spec ?? saved.spec,
      draft?.layoutPositions ?? saved.layoutPositions,
      {
        savedDagId: saved.savedDagId,
        projectId: saved.projectId,
        revision: draft?.revision ?? saved.revision,
        layout: draft?.layout ?? saved.layout,
      },
    );
  };

  const addEditorNode = (capability?: CapabilityDefinition, position?: XYPosition) => {
    const selectedCapability = capability ?? capabilities.find((item) => item.enabled);
    const id = uniqueNodeId(editorDag);
    const nodePosition = position ?? nextHorizontalNodePosition(editorNodes);
    setEditorLayoutPositions({
      ...editorLayoutPositionsRef.current,
      [id]: nodePosition,
    });
    updateEditorDag((current) => ({
      ...current,
      status: 'draft',
      nodes: [
        ...current.nodes,
        normalizeNode({
          id,
          payload: {
            type: 'capability',
            invocation: {
              capability_id: selectedCapability?.id ?? '',
              kind: selectedCapability?.kind ?? 'tool',
              arguments: ensureSchemaArguments({}, selectedCapability?.parameters),
              boundary: {
                allowed_paths: ['.'],
              },
              risk: capabilityRisk(selectedCapability),
            },
          },
          status: 'planned',
        }),
      ],
    }));
    setEditorSelectedId(id);
  };

  const patchEditorNode = (nodeId: string, patch: Partial<DagNode>, nextEdges?: DagEdge[]) => {
    updateEditorDag((current) => ({
      ...current,
      status: 'draft',
      nodes: current.nodes.map((node) =>
        node.id === nodeId ? normalizeNode({ ...node, ...patch }) : node,
      ),
      edges: nextEdges ?? current.edges,
    }));
  };

  const deleteEditorNode = (nodeId: string = editorSelectedId) => {
    if (!nodeId) return;
    updateEditorDag((current) => ({
      ...current,
      status: 'draft',
      nodes: current.nodes
        .filter((node) => node.id !== nodeId)
        .map((node) => {
          if (!isCapabilityNode(node)) return node;
          const invocation = node.payload.invocation;
          return {
            ...node,
            payload: {
              type: 'capability' as const,
              invocation: {
                ...invocation,
                arguments: removeNodeOutputRefs(invocation.arguments ?? {}, nodeId) as Record<string, unknown>,
              },
            },
          };
        }),
      edges: current.edges.filter((edge) => edge.source !== nodeId && edge.target !== nodeId),
    }));
  };

  const saveEditorDraftSpec = async (
    spec: UserDag,
    savingMessage = '正在保存 DAG...',
    savedMessage?: (saved: SavedDag) => string,
  ): Promise<SavedDag | null> => {
    const validation = validateUserDagDraft(spec);
    if (validation) {
      setEditorMessage(validation);
      return null;
    }
    setEditorMessage('正在校验 DAG...');
    try {
      const validationResult = await validateDag(spec);
      if (!validationResult.valid) {
        setEditorMessage(dagValidationIssueMessage(validationResult.issues));
        return null;
      }
      setEditorMessage(savingMessage);
      const localDag = runtimeDagFromUserDag(spec);
      const localLayoutPositions = pruneNodePositions(editorLayoutPositionsRef.current, localDag);
      const saved = await saveSavedDag({
        name: spec.name,
        description: spec.description ?? '',
        savedDagId: editorSavedDagId,
        projectId: editorSavedDagId ? undefined : selectedProjectId || null,
        expectedRevision: editorSavedDagRevision,
        spec,
        layout: savedLayoutWithNodePositions(editorSavedDagLayout, localLayoutPositions),
      });
      const savedDag = runtimeDagFromUserDag(saved.spec);
      const savedLayoutPositions = pruneNodePositions(
        layoutPositionsFromSavedLayout(saved.layout),
        savedDag,
      );
      updateEditorDagDrafts((current) => {
        const next = { ...current };
        const previousKey = editorSavedDagId ?? spec.id;
        if (previousKey && previousKey !== saved.id) delete next[previousKey];
        next[saved.id] = {
          spec: saved.spec,
          savedDagId: saved.id,
          revision: saved.revision,
          layout: saved.layout,
          layoutPositions: savedLayoutPositions,
        };
        return next;
      });
      setEditorUserDagAndRuntimeDag(saved.spec, savedLayoutPositions, {
        savedDagId: saved.id,
        projectId: saved.project_id ?? null,
        revision: saved.revision,
        layout: saved.layout,
      });
      await refreshConsoleData();
      setEditorMessage(savedMessage ? savedMessage(saved) : `已保存 ${saved.name || saved.spec.name || 'DAG'}。`);
      return saved;
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
      return null;
    }
  };

  const persistEditorUserDag = async (): Promise<SavedDag | null> => {
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    return await saveEditorDraftSpec(spec);
  };

  const ensureEditorDagSavedForRun = async (spec: UserDag): Promise<SavedDag | null> => {
    const saved = editorSavedDagId
      ? savedDags.find((item) => item.id === editorSavedDagId) ?? null
      : null;
    if (saved && savedDagMatchesEditorSpec(saved, spec)) return saved;
    return await saveEditorDraftSpec(spec);
  };

  const createEditorArtifact = () => {
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    const artifactId = uniqueDraftArtifactId(spec.artifacts ?? {}, 'artifact');
    const artifact: Artifact = {
      id: artifactId,
      paths: [`outputs/${artifactId}`],
      description: artifactId,
      required: true,
      metadata: {},
    };
    setEditorUserDag({
      ...spec,
      artifacts: upsertArtifact(spec.artifacts ?? {}, artifact),
    });
    setEditorMessage(`已添加 artifact ${artifactId}。`);
  };

  const saveEditorArtifact = (previousId: string, artifact: Artifact) => {
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    const validation = validateArtifactDraft(artifact, spec.artifacts ?? {}, previousId);
    if (validation) {
      setEditorMessage(validation);
      return false;
    }
    const nextSpec = updateArtifactBinding(spec, previousId, artifact);
    setEditorUserDagAndRuntimeDag(nextSpec, editorLayoutPositionsRef.current, {
      savedDagId: editorSavedDagId,
      projectId: editorSavedDagProjectId,
      revision: editorSavedDagRevision,
      layout: editorSavedDagLayout,
    });
    setEditingArtifactId('');
    setEditorMessage(previousId === artifact.id ? `已更新 artifact ${artifact.id}。` : `已重命名 artifact ${previousId} -> ${artifact.id}。`);
    return true;
  };

  const deleteEditorArtifact = (artifactId: string) => {
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    const nextSpec = removeArtifactBinding(spec, artifactId);
    if (editingArtifactId === artifactId) setEditingArtifactId('');
    setEditorUserDagAndRuntimeDag(nextSpec, editorLayoutPositionsRef.current, {
      savedDagId: editorSavedDagId,
      projectId: editorSavedDagProjectId,
      revision: editorSavedDagRevision,
      layout: editorSavedDagLayout,
    });
    setEditorMessage(`已删除 artifact ${artifactId}。`);
  };

  const queueChatUploads = (fileList: FileList | null) => {
    const files = filesFromList(fileList);
    if (!files.length) return;
    setPendingChatUploads((current) => [...current, ...files]);
  };

  const removePendingChatUploads = (indexes: number[]) => {
    const removeIndexes = new Set(indexes);
    setPendingChatUploads((current) => current.filter((_, itemIndex) => !removeIndexes.has(itemIndex)));
  };

  function isOrchestrationSessionConflict(exc: unknown): boolean {
    return exc instanceof Error && exc.message.includes('Orchestration session already exists');
  }

  const ensureOrchestrationContext = async (
    kind: OrchestrationSessionKind,
    title: string,
    options: {
      targetProjectId?: string | null;
      savedDagId?: string | null;
      draftDag?: Record<string, unknown> | null;
      uiState?: Record<string, unknown>;
    } = {},
  ): Promise<OrchestrationContext | null> => {
    try {
      const targetProjectId = Object.prototype.hasOwnProperty.call(options, 'targetProjectId')
        ? options.targetProjectId ?? null
        : selectedProjectId || null;
      let conversation = (
        selectedConversation?.kind === kind
        && selectedConversation?.project_id === targetProjectId
      ) ? selectedConversation : null;
      if (!conversation) {
        const createdConversation = targetProjectId
          ? await createProjectConversation(targetProjectId, { title, kind })
          : await createConversation({ title, kind });
        conversation = createdConversation;
        setConversations((items) => [
          createdConversation,
          ...items.filter((item) => item.id !== createdConversation.id),
        ]);
        setSelectedConversationId(conversation.id);
        if (conversation.project_id) setSelectedProjectId(conversation.project_id);
      }

      let session = await getOrchestrationSessionByConversation(conversation.id);
      let createdSession = false;
      if (!session) {
        try {
          session = await createOrchestrationSession({
            conversation_id: conversation.id,
            project_id: conversation.project_id,
            kind,
            saved_dag_id: options.savedDagId,
            draft_dag: options.draftDag,
            ui_state: options.uiState ?? {},
          });
          createdSession = true;
        } catch (exc) {
          if (!isOrchestrationSessionConflict(exc)) throw exc;
          session = await getOrchestrationSessionByConversation(conversation.id);
          if (!session) throw exc;
        }
      }
      if (session && !createdSession) {
        const patch: {
          saved_dag_id?: string | null;
          draft_dag?: Record<string, unknown> | null;
          ui_state?: Record<string, unknown>;
        } = {};
        if (Object.prototype.hasOwnProperty.call(options, 'savedDagId')) patch.saved_dag_id = options.savedDagId;
        if (Object.prototype.hasOwnProperty.call(options, 'draftDag')) patch.draft_dag = options.draftDag;
        if (options.uiState) patch.ui_state = options.uiState;
        if (Object.keys(patch).length) {
          session = await updateOrchestrationSession(session.id, patch);
        }
      }
      if (!session) throw new Error('Orchestration session not found.');

      return {
        conversation,
        session,
        request: {
          projectId: conversation.project_id,
          conversationId: conversation.id,
        },
      };
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
      setDynamicStatusMessage(exc instanceof Error ? exc.message : String(exc));
      return null;
    }
  };

  const uploadEditorFiles = async (fileList: FileList | null) => {
    const files = filesFromList(fileList);
    if (!files.length) return;
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    const uploadDraft = createUploadedFileArtifacts(files as UploadSourceFile[], {
      artifacts: spec.artifacts ?? {},
      uploadRoot: 'uploads',
    });
    const saved = await saveEditorDraftSpec(
      { ...spec, artifacts: uploadDraft.artifacts },
      `正在保存并上传 ${files.length} 个文件...`,
      () => `正在上传 ${files.length} 个文件...`,
    );
    if (!saved) return;
    try {
      await Promise.all(uploadDraft.uploads.map((upload, index) =>
        uploadSavedDagArtifact(saved.id, upload.artifact.id, [files[index]], { preserveRelativePath: false }),
      ));
      await refreshConsoleData();
      setEditorMessage(`已上传 ${files.length} 个文件。`);
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const runEditorSpec = async () => {
    if (editorRunning || editorRunInFlightRef.current) return;
    editorRunInFlightRef.current = true;
    setEditorRunning(true);
    try {
      const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
      const parsedInput = parseDagRunInput(editorRunInputText);
      if (!parsedInput.ok) {
        setEditorMessage(parsedInput.message);
        return;
      }
      const saved = await ensureEditorDagSavedForRun(spec);
      if (!saved) return;
      const validation = validateUserDagDraft(spec);
      if (validation) return;
      const context = await ensureOrchestrationContext(
        'static_dag',
        saved.name || saved.spec.name || spec.name || '静态编排',
        {
          targetProjectId: saved.project_id ?? null,
          savedDagId: saved.id,
          draftDag: runtimeDagFromUserDag(saved.spec) as unknown as Record<string, unknown>,
          uiState: { selectedNodeId: editorSelectedId },
        },
      );
      if (!context) return;
      setEditorTrace([]);
      setEditorRun(null);
      setEditorRunTimeline([]);
      setEditorMessage(`Running ${saved.name || saved.spec.name || spec.name || 'DAG'}...`);
      await runSavedDagStream(saved.id, {
        onTrace: (event) => {
          setEditorTrace((items) => [...items, event]);
          setEditorRunTimeline((items) => appendRunTranscriptTraceEvent(items, event));
        },
        onCapability: (event) => {
          setEditorRunTimeline((items) => appendRunTranscriptCapability(items, event));
        },
        onContent: (event) => {
          setEditorRunTimeline((items) => appendRunTranscriptToken(items, event.delta));
        },
        onReview: (review) => {
          setEditorMessage(review.message);
        },
        onDone: (payload) => {
          const runState = payload.result.state;
          if (!runState?.dag || !runState.trace || !runState.run_id) return;
          const dagRun = {
            run_id: runState.run_id,
            spec_id: runState.spec_id ?? null,
            workspace_path: runState.workspace_path ?? '',
            dag: runState.dag,
            trace: runState.trace,
            status: dagRunStatus(runState.status),
          } as const;
          setEditorRun(dagRun);
          syncEditorDag(dagRun.dag);
          void refreshConversations();
          setEditorMessage(`Run ${dagRun.status}.`);
          setEditorRunTimeline((items) => appendRunTranscriptToken(
            items,
            `\n\nRun ${dagRun.status}.`,
          ));
        },
        onError: (message) => {
          setEditorMessage(message);
          setEditorRunTimeline((items) => appendRunTranscriptToken(items, `\n\nRun error: ${message}`));
        },
      }, {
        conversation: context.request,
        ...(parsedInput.hasInput ? { input: parsedInput.value } : {}),
      });
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
    } finally {
      editorRunInFlightRef.current = false;
      setEditorRunning(false);
    }
  };

  function dynamicReviewLevel(): ReviewLevel {
    return 'careful';
  }

  const dynamicHandlers = (conversationContext?: OrchestrationContext['request']) => ({
    onDag: (nextDag: Dag) => {
      syncDynamicDag(preserveDynamicDagEdges(nextDag));
      setDynamicStatusMessage(`DAG ${nextDag.status} · ${nextDag.nodes.length} 节点`);
    },
    onTrace: (event: TraceLogEvent) => {
      setDynamicTrace((items) => [...items, { ...event, timelineOrder: nextDynamicTimelineOrder() }]);
    },
    onReview: (review: ReviewEventPayload) => {
      setDynamicStatusMessage(review.message);
    },
    onDone: (payload: Parameters<NonNullable<Parameters<typeof streamTask>[3]['onDone']>>[0]) => {
      const state = payload.result.state ?? null;
      setDynamicRunState(state);
      if (state?.dag) syncDynamicDag(preserveDynamicDagEdges(state.dag));
      if (conversationContext && state?.run_id) void refreshConversations();
      if (state?.pending_review) {
        clearDynamicFinalAnswer();
        const content = 'DAG 已生成，可编辑节点后点击「运行」。';
        appendDynamicMessage('assistant', content);
        setDynamicStatusMessage('');
      } else {
        const answer = payload.result.output_text ?? '';
        setOrderedDynamicFinalAnswer(answer);
        const status = state?.status ?? 'completed';
        const label = dagStatusLabels[status as Dag['status']] ?? status;
        if (!answer) {
          appendDynamicMessage('assistant', `运行${label}。`);
        }
        setDynamicStatusMessage('');
      }
    },
    onError: (message: string) => {
      appendDynamicMessage('assistant', message);
      setDynamicStatusMessage('');
    },
  });

  const generateDynamicDag = async () => {
    if (!dynamicPrompt.trim() || dynamicRunning) return;
    const prompt = dynamicPrompt.trim();
    const dynamicRequestMessages = buildDynamicDagMessages(dynamicMessages, prompt, dynamicDag);
    appendDynamicMessage('user', prompt);
    setDynamicRunning(true);
    setDynamicRunState(null);
    setDynamicTrace([]);
    clearDynamicFinalAnswer();
    setDynamicStatusMessage(dynamicDag.nodes.length ? '正在根据对话和当前 DAG 重新生成...' : '正在生成 DAG...');
    try {
      const context = await ensureOrchestrationContext(
        'dynamic_dag',
        conversationTitleFromPrompt(prompt),
        {
          targetProjectId: selectedProjectId || null,
          draftDag: dynamicDag as unknown as Record<string, unknown>,
        },
      );
      if (!context) return;
      await streamMessagesTask(
        dynamicRequestMessages,
        'dag',
        dynamicReviewLevel(),
        dynamicHandlers(context.request),
        undefined,
        dynamicAdjust,
        { conversation: context.request },
      );
      setDynamicPrompt('');
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      appendDynamicMessage('assistant', message);
      setDynamicStatusMessage('');
    } finally {
      setDynamicRunning(false);
    }
  };

  const runDynamicDag = async () => {
    const reviewId = dynamicRunState?.pending_review?.review_id;
    if (!reviewId || dynamicRunning || !dynamicDag.nodes.length) return;
    const dag = dynamicDag;
    setDynamicRunning(true);
    setDynamicTrace([]);
    clearDynamicFinalAnswer();
    setDynamicStatusMessage(dynamicAdjust ? '正在运行，后续重规划会再次进入审核...' : '正在按当前 DAG 运行...');
    try {
      const context = await ensureOrchestrationContext(
        'dynamic_dag',
        dag.dag_id || '动态编排',
        {
          targetProjectId: selectedProjectId || null,
          draftDag: dag as unknown as Record<string, unknown>,
        },
      );
      if (!context) return;
      await resumeDagReview(
        reviewId,
        dag,
        dynamicReviewLevel(),
        true,
        dynamicHandlers(context.request),
        dynamicRunState,
        undefined,
        { conversation: context.request },
      );
    } catch (exc) {
      setDynamicStatusMessage(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setDynamicRunning(false);
    }
  };

  const ensureChatConversation = async (prompt: string): Promise<ApiConversation | null> => {
    if (selectedChatConversation) return selectedChatConversation;
    try {
      const conversation = chatSub === 'projects'
        ? selectedProjectId
          ? await createProjectConversation(selectedProjectId, {
            title: conversationTitleFromPrompt(prompt),
          })
          : null
        : await createConversation({
          title: conversationTitleFromPrompt(prompt),
        });
      if (!conversation) {
        setProjectError('请先新建或选择项目。');
        return null;
      }
      setConversations((items) => [conversation, ...items]);
      setSelectedConversationId(conversation.id);
      if (conversation.project_id) setSelectedProjectId(conversation.project_id);
      setProjectError(null);
      return conversation;
    } catch (exc) {
      setProjectError(exc instanceof Error ? exc.message : String(exc));
      return null;
    }
  };

  const runStream = async () => {
    if (!draft.trim() || streaming) return;
    const prompt = draft.trim();
    const conversation = await ensureChatConversation(prompt);
    if (!conversation) return;
    const conversationContext = { projectId: conversation.project_id, conversationId: conversation.id };
    const uploadsForRequest = pendingChatUploads;
    setDraft('');
    setError(null);
    setTrace([]);
    tokenQueueRef.current = [];
    contentStreamedRef.current = false;
    stopTokenTimer();
    setStreaming(true);
    setMessages((items) => [
      ...items,
      { role: 'user', kind: 'text', content: prompt },
      { role: 'assistant', kind: 'text', content: '' },
    ]);
    const capabilityScope = chatScopeMode === 'all' && chatAgentScope === 'none'
      ? undefined
      : {
        ...(chatScopeMode === 'custom' ? { capabilityIds: selectedChatCapabilityIds, skills: selectedChatSkillNames } : {}),
        agentScope: chatAgentScope,
        agentIds: selectedChatAgentIds,
      };
    appendTrace({
      type: 'model',
      label: 'runtime_started',
      detail: `Agent target=${target}; capabilities=${chatScopeLabel}.`,
      status: 'running',
    });
    const signal = beginStreamRequest();
    try {
      const streamOptions = { signal, uploads: uploadsForRequest, conversation: conversationContext };
      await streamTask(prompt, target, reviewLevel, {
        onStarted: () => {
          if (uploadsForRequest.length) {
            setPendingChatUploads((current) => current.slice(uploadsForRequest.length));
          }
        },
        onDag: (nextDag) => {
          flushQueuedTokensNow();
          closeAssistantReasoning();
          syncDag(nextDag);
          attachDagToLastAssistant(nextDag);
          if (shouldOpenDagReview(nextDag)) setReviewOpen(true);
        },
        onTrace: appendRuntimeTrace,
        onCapability: appendCapabilityMessage,
        onReasoning: (event) => enqueueReasoningToken(event.delta),
        onContent: (event) => enqueueContentToken(event.delta),
        onRetry: appendValidationFeedback,
        onValidating: appendValidating,
        onReview: (review) => {
          if (review.kind === 'capability_review') {
            setCapabilityReviewFeedback('');
            setCapabilityReview(review);
          } else {
            setReviewOpen(true);
          }
        },
        onDone: (payload) => {
          const result = payload.result;
          const resultDag = result.state?.dag ?? null;
          const resultReview = result.state?.pending_review ?? null;
          setRunState(result.state ?? null);
          if (result.state?.run_id) {
            setConversations((items) => items.map((conversation) => (
              conversation.id === conversationContext.conversationId
                ? { ...conversation, last_run_id: result.state?.run_id ?? conversation.last_run_id }
                : conversation
            )));
          }
          flushQueuedTokensNow();
          closeAssistantReasoning();
          if (resultDag) {
            syncDag(resultDag);
            attachDagToLastAssistant(resultDag);
            if (shouldOpenDagReview(resultDag, resultReview)) setReviewOpen(true);
            appendTrace({ type: 'dag', label: 'dag_generated', detail: `Generated ${resultDag.nodes.length} node(s).`, status: 'completed' });
          }
          handlePendingReview(resultReview);
          enqueueFinalAnswerIfMissing(result.output_text);
          appendTrace({
            type: 'model',
            label: 'runtime_completed',
            detail: resultDag ? 'DAG loop completed the request.' : 'Capability loop completed the request.',
            status: result.state?.status === 'failed' ? 'failed' : 'completed',
          });
        },
        onError: (message) => {
          setError(message);
          appendTrace({ type: 'model', label: 'dag_agent_failed', detail: message, status: 'failed' });
        },
      }, capabilityScope, null, undefined, streamOptions);
    } catch (exc) {
      if (isAbortError(exc) || signal.aborted) return;
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      appendTrace({ type: 'model', label: 'dag_agent_failed', detail: message, status: 'failed' });
    } finally {
      clearStreamRequest(signal);
      await waitForTokenQueue();
      setStreaming(false);
    }
  };

  const stopStream = () => {
    streamAbortRef.current?.abort();
    tokenQueueRef.current = [];
    contentStreamedRef.current = false;
    stopTokenTimer();
    resolveTokenDrain();
    setStreaming(false);
    appendTrace({ type: 'model', label: 'interrupted', detail: 'The current UI stream was interrupted.', status: 'failed' });
  };

  const resumeDag = async (approved: boolean) => {
    if (!dagReview || streaming) return;
    const previousDagReview = dagReview;
    const previousDagReviewFeedback = dagReviewFeedback;
    const previousDag = dag;
    const previousMessages = messages;
    setError(null);
    setReviewOpen(false);
    tokenQueueRef.current = [];
    contentStreamedRef.current = false;
    stopTokenTimer();
    setStreaming(true);
    const reviewId = dagReview.review_id;
    const feedback = approved ? '' : dagReviewFeedback.trim();
    setDagReview(null);
    setDagReviewFeedback('');
    appendTrace({
      type: 'dag',
      label: approved ? 'dag_confirmed' : 'dag_rejected',
      detail: `${approved ? 'Approving' : 'Rejecting'} review ${reviewId}.`,
      status: approved ? 'running' : 'rejected',
    });
    if (!approved) {
      const rejectedDag = { ...dag, status: 'rejected' as const };
      syncDag(rejectedDag);
      attachDagToLastAssistant(rejectedDag);
    }

    const signal = beginStreamRequest();
    try {
      await resumeDagReview(reviewId, approved ? dag : null, reviewLevel, approved, {
        onDag: (nextDag) => {
          flushQueuedTokensNow();
          closeAssistantReasoning();
          syncDag(nextDag);
          attachDagToLastAssistant(nextDag);
        },
        onTrace: appendRuntimeTrace,
        onCapability: appendCapabilityMessage,
        onReasoning: (event) => enqueueReasoningToken(event.delta),
        onContent: (event) => enqueueContentToken(event.delta),
        onRetry: appendValidationFeedback,
        onValidating: appendValidating,
        onReview: (review) => {
          if (review.kind === 'capability_review') {
            setCapabilityReviewFeedback('');
            setCapabilityReview(review);
          } else {
            setReviewOpen(true);
          }
        },
        onDone: (payload) => {
          const result = payload.result;
          const resultDag = result.state?.dag ?? null;
          const resultReview = result.state?.pending_review ?? null;
          setRunState(result.state ?? null);
          flushQueuedTokensNow();
          closeAssistantReasoning();
          if (resultDag) {
            syncDag(resultDag);
            attachDagToLastAssistant(resultDag);
            if (shouldOpenDagReview(resultDag, resultReview)) setReviewOpen(true);
          }
          handlePendingReview(resultReview);
          enqueueFinalAnswerIfMissing(result.output_text);
          appendTrace({ type: 'model', label: 'runtime_completed', detail: 'DAG loop completed the request.', status: 'completed' });
        },
        onError: (message) => {
          setError(message);
          appendTrace({ type: 'model', label: 'resume_failed', detail: message, status: 'failed' });
        },
      }, activeConversationContext ? null : runState, feedback, {
        signal,
        ...(activeConversationContext ? {
          conversation: activeConversationContext,
        } : {}),
      });
    } catch (exc) {
      if (isAbortError(exc) || signal.aborted) {
        restoreDagReviewAfterAbort(previousDagReview, previousDagReviewFeedback, previousDag, previousMessages);
        return;
      }
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      appendTrace({ type: 'model', label: 'resume_failed', detail: message, status: 'failed' });
    } finally {
      clearStreamRequest(signal);
      await waitForTokenQueue();
      setStreaming(false);
    }
  };

  const confirmDag = () => {
    void resumeDag(true);
  };

  const rejectDag = () => {
    void resumeDag(false);
  };

  const confirmCapabilityReview = async (approved: boolean) => {
    if (!capabilityReview || streaming) return;
    const previousCapabilityReview = capabilityReview;
    const previousCapabilityReviewFeedback = capabilityReviewFeedback;
    const previousMessages = messages;
    const feedback = capabilityReviewFeedback.trim();
    setCapabilityReview(null);
    setCapabilityReviewFeedback('');
    setError(null);
    tokenQueueRef.current = [];
    contentStreamedRef.current = false;
    stopTokenTimer();
    setStreaming(true);
    appendTrace({
      type: 'model',
      label: 'capability_review_resumed',
      detail: `Capability review ${approved ? 'approved' : 'rejected'}.`,
      status: approved ? 'running' : 'rejected',
    });
    if (!approved) {
      flushQueuedTokensNow();
      closeAssistantReasoning();
      updateLastAssistantText((message) => ({
        ...message,
        timeline: appendCapabilityReviewDecisionTimeline(message.timeline, capabilityReview, approved, feedback),
      }));
    }

    const signal = beginStreamRequest();
    try {
      await resumeCapabilityReview(capabilityReview.review_id, approved, {
        onTrace: appendRuntimeTrace,
        onCapability: appendCapabilityMessage,
        onReasoning: (event) => enqueueReasoningToken(event.delta),
        onContent: (event) => enqueueContentToken(event.delta),
        onRetry: appendValidationFeedback,
        onValidating: appendValidating,
        onReview: handlePendingReview,
        onDone: (payload) => {
          const resultReview = payload.result.state?.pending_review ?? null;
          setRunState(payload.result.state ?? null);
          flushQueuedTokensNow();
          closeAssistantReasoning();
          handlePendingReview(resultReview);
          enqueueFinalAnswerIfMissing(payload.result.output_text);
          appendTrace({ type: 'model', label: 'runtime_completed', detail: 'Capability loop completed the request.', status: 'completed' });
        },
        onError: (message) => {
          setError(message);
          appendTrace({ type: 'model', label: 'capability_review_failed', detail: message, status: 'failed' });
        },
      }, activeConversationContext ? null : runState, feedback, {
        signal,
        ...(activeConversationContext ? {
          conversation: activeConversationContext,
        } : {}),
      });
    } catch (exc) {
      if (isAbortError(exc) || signal.aborted) {
        restoreCapabilityReviewAfterAbort(previousCapabilityReview, previousCapabilityReviewFeedback, previousMessages);
        return;
      }
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      appendTrace({ type: 'model', label: 'capability_review_failed', detail: message, status: 'failed' });
    } finally {
      clearStreamRequest(signal);
      await waitForTokenQueue();
      setStreaming(false);
    }
  };

  const createProjectConversationFromProject = async (projectId: string) => {
    if (streaming) return null;
    const projectConversationCount = conversations.filter((conversation) => (
      conversation.kind === 'chat' && conversation.project_id === projectId
    )).length;
    try {
      const conversation = await createProjectConversation(projectId, {
        title: `会话 ${projectConversationCount + 1}`,
      });
      setConversations((items) => [conversation, ...items]);
      setSelectedProjectId(projectId);
      setSelectedConversationId(conversation.id);
      setChatSub('projects');
      setProjectError(null);
      clearChatSurface();
      return conversation;
    } catch (exc) {
      setProjectError(exc instanceof Error ? exc.message : String(exc));
      return null;
    }
  };

  const newChat = async () => {
    if (streaming) return;
    if (chatSub === 'projects') {
      if (!selectedProjectId) {
        setProjectError('请先新建或选择项目。');
        return;
      }
      await createProjectConversationFromProject(selectedProjectId);
    } else {
      const standaloneConversationCount = conversations.filter((conversation) => (
        conversation.kind === 'chat' && !conversation.project_id
      )).length;
      try {
        const conversation = await createConversation({
          title: `会话 ${standaloneConversationCount + 1}`,
        });
        setConversations((items) => [conversation, ...items]);
        setSelectedConversationId(conversation.id);
        setProjectError(null);
      } catch (exc) {
        setProjectError(exc instanceof Error ? exc.message : String(exc));
        return;
      }
      setDagReview(null);
      setDagReviewFeedback('');
      setCapabilityReview(null);
      setCapabilityReviewFeedback('');
      setRunState(null);
      setMessages([]);
      setDraft('');
      setPendingChatUploads([]);
      syncDag(emptyDag);
      setTrace([]);
      setError(null);
      setReviewOpen(false);
      tokenQueueRef.current = [];
      contentStreamedRef.current = false;
      stopTokenTimer();
    }
  };

  const clearChatSurface = () => {
    conversationHydrationRequestRef.current += 1;
    setMessages([]);
    setDraft('');
    setPendingChatUploads([]);
    syncDag(emptyDag);
    setTrace([]);
    setError(null);
    setReviewOpen(false);
    setDagReview(null);
    setDagReviewFeedback('');
    setCapabilityReview(null);
    setCapabilityReviewFeedback('');
    setRunState(null);
    setRunArtifactFiles([]);
    setSelectedArtifactId('');
    tokenQueueRef.current = [];
    contentStreamedRef.current = false;
    stopTokenTimer();
  };

  const selectChatSub = (sub: ChatWorkspaceSub) => {
    if (streaming) return;
    if (
      sub === chatSub
      && (
        (sub === 'projects' && !selectedConversationId)
        || (sub === 'conversations' && (!selectedChatConversation || !selectedChatConversation.project_id))
      )
    ) {
      return;
    }
    setChatSub(sub);
    if (sub === 'projects') {
      if (!selectedProjectId && projects[0]) setSelectedProjectId(projects[0].id);
      setSelectedConversationId('');
      clearChatSurface();
      return;
    }
    setSelectedConversationId('');
    clearChatSurface();
  };

  const selectProject = (projectId: string) => {
    if (streaming) return;
    if (projectId === selectedProjectId && !selectedConversationId) return;
    setSelectedProjectId(projectId);
    setSelectedConversationId('');
    setChatSub('projects');
    clearChatSurface();
  };

  const selectConversation = (conversationId: string) => {
    if (streaming || conversationId === selectedConversationId) return;
    const conversation = conversations.find((item) => item.id === conversationId);
    if (conversation?.project_id) setSelectedProjectId(conversation.project_id);
    setSelectedConversationId(conversationId);
    clearChatSurface();
  };

  const selectWorkspace = (workspace: WorkspaceKey) => {
    setActiveWorkspace(workspace);
    if (!streaming && workspace === 'chat' && chatSub === 'conversations') {
      setSelectedConversationId('');
      clearChatSurface();
    }
  };

  const deleteConversationFromSidebar = async (conversationId: string) => {
    if (streaming) return;
    setConversationDeleteTargetId(conversationId);
  };

  const confirmConversationDelete = async () => {
    if (streaming || !conversationDeleteTarget) return;
    const conversation = conversationDeleteTarget;
    const remaining = conversations.filter((item) => item.id !== conversation.id);
    const deletingSelected = conversation.id === selectedConversationId;
    try {
      if (conversation.project_id) {
        await deleteProjectConversation(conversation.project_id, conversation.id);
      } else {
        await deleteConversation(conversation.id);
      }
      setConversations(remaining);
      if (deletingSelected) {
        const nextConversation = chatSub === 'projects'
          ? remaining.find((item) => item.kind === 'chat' && item.project_id === conversation.project_id) ?? null
          : remaining.find((item) => item.kind === 'chat' && !item.project_id) ?? null;
        setSelectedConversationId(nextConversation?.id ?? '');
        if (nextConversation?.project_id) setSelectedProjectId(nextConversation.project_id);
        clearChatSurface();
      }
      setProjectError(null);
      setConversationDeleteTargetId('');
    } catch (exc) {
      setProjectError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const createProjectFromSidebar = async () => {
    if (streaming) return;
    setProjectDraft({ name: '', slug: '', description: '' });
    setProjectCreateOpen(true);
  };

  const submitProjectCreation = async () => {
    const cleanName = projectDraft.name.trim();
    if (!cleanName || streaming) return;
    try {
      const project = await createProject({
        name: cleanName,
        ...(projectDraft.slug.trim() ? { slug: projectDraft.slug.trim() } : {}),
        ...(projectDraft.description.trim() ? { description: projectDraft.description.trim() } : {}),
      });
      setProjects((items) => [project, ...items]);
      setSelectedProjectId(project.id);
      setSelectedConversationId('');
      setChatSub('projects');
      setProjectCreateOpen(false);
      setProjectError(null);
      clearChatSurface();
    } catch (exc) {
      setProjectError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const openProjectEditDialog = () => {
    if (!selectedProject) return;
    setProjectDraft({
      name: selectedProject.name,
      slug: selectedProject.slug,
      description: selectedProject.description ?? '',
    });
    setProjectEditOpen(true);
  };

  const submitProjectEdit = async () => {
    if (!selectedProject || streaming) return;
    const cleanName = projectDraft.name.trim();
    if (!cleanName) return;
    try {
      const project = await updateProject(selectedProject.id, {
        name: cleanName,
        slug: projectDraft.slug.trim() || selectedProject.slug,
        description: projectDraft.description.trim() || null,
      });
      setProjects((items) => items.map((item) => (item.id === project.id ? project : item)));
      setProjectEditOpen(false);
      setProjectError(null);
    } catch (exc) {
      setProjectError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const confirmProjectDelete = async () => {
    if (!selectedProject || streaming) return;
    const projectId = selectedProject.id;
    try {
      await deleteProject(projectId);
      setProjects((items) => items.filter((item) => item.id !== projectId));
      setConversations((items) => items.filter((conversation) => conversation.project_id !== projectId));
      setSelectedProjectId('');
      setSelectedConversationId('');
      setProjectDeleteOpen(false);
      setProjectFiles([]);
      setProjectFilePreview(null);
      setProjectError(null);
      clearChatSurface();
    } catch (exc) {
      setProjectError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const openProjectFile = async (file: ProjectFileItem) => {
    const requestId = projectFilePreviewRequestRef.current + 1;
    projectFilePreviewRequestRef.current = requestId;
    setSelectedProjectFilePath(file.path);
    setProjectFilePreview(null);
    setProjectFilePreviewError(null);
    if (file.kind === 'directory') {
      setProjectFilePath(file.path);
      setProjectFilePreviewLoading(false);
      return;
    }
    if (!selectedProject || !file.preview_url) {
      setProjectFilePreviewLoading(false);
      return;
    }
    const projectId = selectedProject.id;
    setProjectFilePreviewLoading(true);
    try {
      const preview = await previewProjectFile(projectId, file.path);
      if (projectFilePreviewRequestRef.current !== requestId) return;
      setProjectFilePreview(preview);
    } catch (exc) {
      if (projectFilePreviewRequestRef.current !== requestId) return;
      setProjectFilePreviewError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      if (projectFilePreviewRequestRef.current === requestId) setProjectFilePreviewLoading(false);
    }
  };

  const navigateProjectFilesUp = () => {
    projectFilePreviewRequestRef.current += 1;
    setProjectFilePath((current) => parentProjectPath(current));
    setSelectedProjectFilePath('');
    setProjectFilePreview(null);
    setProjectFilePreviewLoading(false);
    setProjectFilePreviewError(null);
  };

  const uploadSelectedProjectFiles = async (files: FileList | null) => {
    if (!selectedProject || !files?.length) return;
    try {
      await uploadProjectFiles(selectedProject.id, projectFilePath, Array.from(files));
      await refreshProjectFiles();
      setProjectError(null);
    } catch (exc) {
      setProjectFilesError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const openProjectFileDialog = (kind: ProjectFileDialogKind, file?: ProjectFileItem) => {
    setProjectFileDialog({ kind, file });
    if (kind === 'folder') {
      setProjectFileDraft('');
    } else {
      setProjectFileDraft(file?.path ?? '');
    }
  };

  const confirmProjectFileDialog = async () => {
    if (!selectedProject || !projectFileDialog) return;
    try {
      if (projectFileDialog.kind === 'folder') {
        const folderPath = joinProjectPath(projectFilePath, projectFileDraft);
        if (!folderPath) return;
        await createProjectFolder(selectedProject.id, folderPath);
      } else if (projectFileDialog.kind === 'rename' && projectFileDialog.file) {
        const nextPath = projectFileDraft.trim();
        if (!nextPath) return;
        await renameProjectFile(selectedProject.id, projectFileDialog.file.path, nextPath);
        if (selectedProjectFilePath === projectFileDialog.file.path) {
          projectFilePreviewRequestRef.current += 1;
          setSelectedProjectFilePath(nextPath);
          setProjectFilePreview(null);
          setProjectFilePreviewLoading(false);
        }
      } else if (projectFileDialog.kind === 'delete' && projectFileDialog.file) {
        await deleteProjectFile(selectedProject.id, projectFileDialog.file.path);
        if (selectedProjectFilePath === projectFileDialog.file.path) {
          projectFilePreviewRequestRef.current += 1;
          setSelectedProjectFilePath('');
          setProjectFilePreview(null);
          setProjectFilePreviewLoading(false);
          setProjectFilePreviewError(null);
        }
      }
      setProjectFileDialog(null);
      setProjectFileDraft('');
      await refreshProjectFiles();
    } catch (exc) {
      setProjectFilesError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  return (
    <div className={`app-shell ${navCollapsed ? 'nav-collapsed' : ''}`}>
      <WorkspaceSidebar
        activeWorkspace={activeWorkspace}
        agentsSub={agentManagementSub}
        agentPresetCount={agentPresets.length}
        agentPresets={agentPresets}
        artifacts={editorArtifacts}
        chatSub={chatSub}
        collapsed={navCollapsed}
        capabilities={capabilities}
        capabilityCount={visibleToolManagementCapabilities(capabilities, '').length}
        conversations={conversations}
        creatingAgentPreset={creatingAgentPreset}
        creatingModel={creatingModel}
        systemSub={systemManagementSub}
        models={models}
        onlyOfficeEnabled={onlyOfficeSettings.enabled}
        mcpCount={mcpServers.length}
        mcpServers={mcpServers}
        projectError={projectError}
        projects={projects}
        pythonTools={pythonTools}
        profiles={profiles}
        orchestrationMode={orchestrationMode}
        savedDags={visibleSavedDags}
        selectedDagId={editorSavedDagId ?? ''}
        selectedAgentPresetId={selectedAgentPresetId}
        selectedConversationId={selectedConversationId}
        selectedModelId={selectedModelId}
        selectedProjectId={selectedProjectId}
        selectedProfileId={selectedProfileId}
        selectedToolCapabilityId={selectedToolCapabilityId}
        selectedToolMcpName={selectedToolMcpName}
        selectedToolMcpToolId={selectedToolMcpToolId}
        selectedToolSkillName={selectedToolSkillName}
        selectedSkillDetail={selectedSkillDetail}
        selectedSkillFilePath={selectedSkillFileDetail?.file_path ?? ''}
        skills={skills}
        skillCount={skills.length}
        toolsSub={toolsDirectoryTab}
        toolsQuery={toolsDirectoryQuery}
        onCreateArtifact={createEditorArtifact}
        onCreateAgentPreset={requestAgentPresetCreation}
        onChatSubChange={selectChatSub}
        onCreateMcp={() => requestCapabilityCreation('mcp')}
        onCreateModel={requestModelCreation}
        onCreateProject={() => void createProjectFromSidebar()}
        onCreateProfile={requestProfileCreation}
        onCreateTool={() => requestCapabilityCreation('tools')}
        onDeleteArtifact={deleteEditorArtifact}
        onEditArtifact={(artifactId) => setEditingArtifactId(artifactId)}
        onImportSkill={() => requestCapabilityCreation('skills')}
        onLoadDag={loadEditorUserDag}
        onNewChat={() => void newChat()}
        onNewProjectConversation={(projectId) => void createProjectConversationFromProject(projectId)}
        onNewDag={newEditorUserDag}
        onDeleteConversation={deleteConversationFromSidebar}
        onSelectProfile={setSelectedProfileId}
        onSelectAgentPreset={(id) => {
          setCreatingAgentPreset(false);
          setSelectedAgentPresetId(id);
        }}
        onSelectModel={(id) => {
          setCreatingModel(false);
          setSelectedModelId(id);
        }}
        onSelectConversation={selectConversation}
        onSelectProject={selectProject}
        onSelectSkillFile={(filePath) => void selectSkillFile(filePath)}
        onSelectToolCapability={setSelectedToolCapabilityId}
        onSelectToolMcp={selectToolMcpResource}
        onSelectToolSkill={selectToolSkill}
        onSelectWorkspace={selectWorkspace}
        onOrchestrationModeChange={setOrchestrationMode}
        onAgentsSubChange={selectAgentManagementSub}
        onSystemSubChange={setSystemManagementSub}
        onToolsSubChange={selectToolsDirectoryTab}
        onToggleCollapsed={() => setNavCollapsed((value) => !value)}
        onToolsQueryChange={setToolsDirectoryQuery}
        onUploadSkillFile={(file) => void loadSkillFile(file)}
        onUploadFiles={(files) => void uploadEditorFiles(files)}
      />
      <main className="workspace">
        {consoleError ? <div className="error-banner global-error">{consoleError}</div> : null}
        {activeWorkspace === 'chat' ? (
          selectedChatConversation || chatSub !== 'projects' ? (
            <ChatWorkspace
            artifactListError={runArtifactError}
            artifactListLoading={runArtifactLoading}
            artifactPanelOpen={artifactDrawerOpen}
            artifactPreview={selectedArtifactPreview}
            artifactPreviewError={selectedArtifactPreviewError}
            artifactPreviewLoading={selectedArtifactPreviewLoading}
            artifacts={chatArtifacts}
            chatScopeLabel={chatScopeLabel}
            currentDag={dag}
            draft={draft}
            error={error}
            loading={streaming}
            messageListRef={messageListRef}
            messages={messages}
            pendingUploads={pendingChatUploads}
            projectName={selectedConversationProject?.name ?? null}
            conversationTitle={selectedChatConversation?.title ?? null}
            reviewLevel={reviewLevel}
            selectedArtifact={selectedArtifact}
            selectedArtifactId={selectedArtifactId}
            target={target}
            validationEnabled={validationEnabled}
            validationError={validationError}
            validationPending={validationPending}
            onArtifactSelect={setSelectedArtifactId}
            onArtifactCopy={copySelectedArtifact}
            onArtifactRefresh={refreshRunArtifacts}
            onDraftChange={setDraft}
            onOpenDag={(snapshot, snapshotTrace) => {
              syncDag(snapshot);
              if (snapshotTrace) setTrace(snapshotTrace);
              setReviewOpen(true);
            }}
            onOpenScope={() => setCapabilityScopeOpen(true)}
            onReviewLevelChange={setReviewLevel}
            onRemoveUpload={removePendingChatUploads}
            onRun={() => void runStream()}
            onStop={stopStream}
            onTargetChange={setTarget}
            onToggleArtifacts={() => setArtifactPanelOpen((value) => !value)}
            onToggleValidation={() => void toggleValidation()}
            onUploadFiles={queueChatUploads}
            />
          ) : (
            <ProjectDetailWorkspace
              error={projectError}
              fileDialog={projectFileDialog}
              fileDraft={projectFileDraft}
              project={selectedProject}
              files={projectFiles}
              filesError={projectFilesError}
              filesLoading={projectFilesLoading}
              path={projectFilePath}
              preview={projectFilePreview}
              previewError={projectFilePreviewError}
              previewLoading={projectFilePreviewLoading}
              selectedFile={selectedProjectFile}
              onCreateFolder={() => openProjectFileDialog('folder')}
              onDeleteFile={(file) => openProjectFileDialog('delete', file)}
              onDeleteProject={() => setProjectDeleteOpen(true)}
              onDialogCancel={() => setProjectFileDialog(null)}
              onDialogConfirm={() => void confirmProjectFileDialog()}
              onDownloadFile={(file) => {
                if (selectedProject && file.download_url) window.open(projectFileDownloadUrl(selectedProject.id, file.path), '_blank', 'noopener');
              }}
              onEditProject={openProjectEditDialog}
              onFileDraftChange={setProjectFileDraft}
              onFileSelect={(file) => void openProjectFile(file)}
              onNavigateUp={navigateProjectFilesUp}
              onNewConversation={() => {
                if (selectedProject) void createProjectConversationFromProject(selectedProject.id);
              }}
              onRefresh={() => void refreshProjectFiles()}
              onRenameFile={(file) => openProjectFileDialog('rename', file)}
              onUploadFiles={(files) => void uploadSelectedProjectFiles(files)}
            />
          )
        ) : activeWorkspace === 'orchestration' && orchestrationMode === 'dynamic' ? (
          <DynamicOrchestrationWorkspace
            capabilities={capabilities}
            dag={dynamicDag}
            finalAnswer={dynamicFinalAnswer}
            finalAnswerOrder={dynamicFinalAnswerOrder}
            nodes={dynamicGraph.nodes}
            edges={dynamicGraph.edges}
            selectedId={dynamicSelectedId}
            prompt={dynamicPrompt}
            dynamicAdjust={dynamicAdjust}
            canRunDag={Boolean(dynamicRunState?.pending_review?.review_id && dynamicDag.nodes.length)}
            running={dynamicRunning}
            message={dynamicMessage}
            messageOrder={dynamicMessageOrder}
            messages={dynamicMessages}
            trace={dynamicTrace}
            onAddNode={onAddDynamicNode}
            onPatchNode={onPatchDynamicNode}
            onDeleteNode={onDeleteDynamicNode}
            onNodesChange={onDynamicNodesChange}
            onEdgesChange={onDynamicEdgesChange}
            onConnect={onDynamicConnect}
            onSelectNode={setDynamicSelectedId}
            onPromptChange={setDynamicPrompt}
            onDynamicAdjustChange={setDynamicAdjust}
            onGenerate={() => void generateDynamicDag()}
            onRun={() => void runDynamicDag()}
          />
        ) : activeWorkspace === 'orchestration' && orchestrationMode === 'static' ? (
          <OrchestrationWorkspace
            capabilities={capabilities}
            skills={skills}
            mcpServers={mcpServers}
            spec={editorUserDag}
            dag={editorDag}
            nodes={editorNodes}
            edges={editorEdges}
            selectedId={editorSelectedId}
            trace={editorTrace}
            run={editorRun}
            runTimeline={editorRunTimeline}
            message={editorMessage}
            running={editorRunning}
            runInputText={editorRunInputText}
            onPatchDag={patchEditorUserDag}
            onRunInputTextChange={setEditorRunInputText}
            onAddNode={addEditorNode}
            onPatchNode={patchEditorNode}
            onDeleteNode={deleteEditorNode}
            onSave={() => void persistEditorUserDag()}
            onRun={() => void runEditorSpec()}
            onNodesChange={onEditorNodesChange}
            onEdgesChange={onEditorEdgesChange}
            onConnect={onEditorConnect}
            onSelectNode={setEditorSelectedId}
          />
        ) : activeWorkspace === 'tools' ? (
          <CapabilityDirectory
            capabilities={capabilities}
            skills={skills}
            mcpServers={mcpServers}
            pythonTools={pythonTools}
            activeTab={toolsDirectoryTab}
            creationIntent={capabilityCreationIntent}
            query={toolsDirectoryQuery}
            selectedCapabilityId={selectedToolCapabilityId}
            selectedMcpName={selectedToolMcpName}
            selectedMcpToolId={selectedToolMcpToolId}
            selectedSkillDetail={selectedSkillDetail}
            selectedSkillFileDetail={selectedSkillFileDetail}
            selectedSkillName={selectedToolSkillName}
            skillImport={skillImport}
            skillMessage={skillMessage}
            onActiveTabChange={selectToolsDirectoryTab}
            onCreationIntentChange={setCapabilityCreationIntent}
            onInstallSkillDraft={() => void installSkillDraft()}
            onRemoveManagedSkill={() => void removeManagedSkill()}
            onSelectedCapabilityIdChange={setSelectedToolCapabilityId}
            onSelectedMcpNameChange={selectToolMcpResource}
            onSelectedSkillNameChange={selectToolSkill}
            onSkillImportChange={setSkillImport}
            onUploadSkillFile={(file) => void loadSkillFile(file)}
            onRefresh={refreshConsoleData}
          />
        ) : activeWorkspace === 'system' ? (
          <SystemManagementWorkspace
            activeSub={systemManagementSub}
            activeModelId={activeModelId}
            creating={creatingModel}
            models={models}
            onlyOfficeSettings={onlyOfficeSettings}
            selectedId={selectedModelId}
            onCreatingChange={setCreatingModel}
            onRefresh={refreshConsoleData}
            onSelect={setSelectedModelId}
          />
        ) : activeWorkspace === 'agents' ? (
          <AgentManagementWorkspace
            activeSub={agentManagementSub}
            agentPresetErrors={agentPresetErrors}
            agentPresets={agentPresets}
            capabilities={capabilities}
            creatingAgentPreset={creatingAgentPreset}
            creating={creatingProfile}
            profiles={profiles}
            selectedAgentPresetId={selectedAgentPresetId}
            selectedId={selectedProfileId}
            skills={skills}
            warnings={profileWarnings}
            onAgentPresetCreate={createAgentPreset}
            onAgentPresetCreatingChange={setCreatingAgentPreset}
            onAgentPresetDelete={removeAgentPreset}
            onAgentPresetSelect={setSelectedAgentPresetId}
            onAgentPresetUpdate={updateAgentPreset}
            onCreate={createManagedProfile}
            onCreatingChange={setCreatingProfile}
            onDelete={removeManagedProfile}
            onRefresh={refreshAgentData}
            onSelect={setSelectedProfileId}
            onUpdate={updateManagedProfile}
          />
        ) : (
          null
        )}
      </main>

      {projectCreateOpen ? (
        <ProjectCreateDialog
          draft={projectDraft}
          error={projectError}
          onCancel={() => setProjectCreateOpen(false)}
          onChange={setProjectDraft}
          onSubmit={() => void submitProjectCreation()}
        />
      ) : null}

      {projectEditOpen && selectedProject ? (
        <ProjectEditDialog
          draft={projectDraft}
          error={projectError}
          project={selectedProject}
          onCancel={() => setProjectEditOpen(false)}
          onChange={setProjectDraft}
          onSubmit={() => void submitProjectEdit()}
        />
      ) : null}

      {projectDeleteOpen && selectedProject ? (
        <ProjectDeleteDialog
          project={selectedProject}
          onCancel={() => setProjectDeleteOpen(false)}
          onConfirm={() => void confirmProjectDelete()}
        />
      ) : null}

      {conversationDeleteTarget ? (
        <ConversationDeleteDialog
          conversation={conversationDeleteTarget}
          project={projects.find((project) => project.id === conversationDeleteTarget.project_id) ?? null}
          onCancel={() => setConversationDeleteTargetId('')}
          onConfirm={() => void confirmConversationDelete()}
        />
      ) : null}

      {capabilityScopeOpen ? (
        <ChatCapabilityScopeDialog
          agentPresets={agentPresets}
          agentScope={chatAgentScope}
          capabilities={capabilities}
          skills={skills}
          mcpServers={mcpServers}
          mode={chatScopeMode}
          selectedAgentIds={selectedChatAgentIds}
          selectedCapabilityIds={selectedChatCapabilityIds}
          selectedSkillNames={selectedChatSkillNames}
          onAgentIdsChange={setSelectedChatAgentIds}
          onAgentScopeChange={setChatAgentScope}
          onModeChange={setChatScopeMode}
          onCapabilityIdsChange={setSelectedChatCapabilityIds}
          onSkillNamesChange={setSelectedChatSkillNames}
          onClose={() => setCapabilityScopeOpen(false)}
        />
      ) : null}

      {reviewOpen && dag.nodes.length ? (
        <DagReviewDialog
          dag={dag}
          nodes={nodes}
          edges={edges}
          trace={trace}
          selectedNode={selectedNode}
          feedback={dagReviewFeedback}
          onFeedbackChange={setDagReviewFeedback}
          onClose={() => setReviewOpen(false)}
          onConfirm={confirmDag}
          onReject={rejectDag}
          onPatchNode={patchSelected}
          onAddNode={addNode}
          onDeleteNode={deleteSelected}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onSelectNode={setSelectedId}
        />
      ) : null}

      {editingArtifact ? (
        <ArtifactEditDialog
          artifact={editingArtifact}
          artifacts={editorUserDag.artifacts ?? {}}
          onClose={() => setEditingArtifactId('')}
          onSave={saveEditorArtifact}
        />
      ) : null}

      {capabilityReview ? (
        <CapabilityReviewDialog
          review={capabilityReview}
          feedback={capabilityReviewFeedback}
          onFeedbackChange={setCapabilityReviewFeedback}
          onApprove={() => confirmCapabilityReview(true)}
          onReject={() => confirmCapabilityReview(false)}
          onClose={() => {
            setCapabilityReview(null);
            setCapabilityReviewFeedback('');
          }}
        />
      ) : null}
    </div>
  );
}

function WorkspaceSidebar({
  activeWorkspace,
  agentsSub,
  agentPresetCount,
  agentPresets,
  artifacts,
  chatSub,
  collapsed,
  capabilities,
  capabilityCount,
  conversations,
  creatingAgentPreset,
  creatingModel,
  systemSub,
  models,
  onlyOfficeEnabled,
  mcpCount,
  mcpServers,
  projectError,
  projects,
  orchestrationMode,
  pythonTools,
  profiles,
  savedDags,
  selectedDagId,
  selectedAgentPresetId,
  selectedConversationId,
  selectedModelId,
  selectedProjectId,
  selectedProfileId,
  selectedToolCapabilityId,
  selectedToolMcpName,
  selectedToolMcpToolId,
  selectedToolSkillName,
  selectedSkillDetail,
  selectedSkillFilePath,
  skills,
  skillCount,
  toolsSub,
  toolsQuery,
  onCreateArtifact,
  onCreateAgentPreset,
  onChatSubChange,
  onCreateMcp,
  onCreateModel,
  onCreateProject,
  onCreateProfile,
  onCreateTool,
  onDeleteArtifact,
  onEditArtifact,
  onImportSkill,
  onLoadDag,
  onNewChat,
  onNewProjectConversation,
  onNewDag,
  onDeleteConversation,
  onSelectAgentPreset,
  onSelectConversation,
  onSelectProfile,
  onSelectProject,
  onSelectModel,
  onSelectSkillFile,
  onSelectToolCapability,
  onSelectToolMcp,
  onSelectToolSkill,
  onSelectWorkspace,
  onOrchestrationModeChange,
  onAgentsSubChange,
  onSystemSubChange,
  onToolsSubChange,
  onToggleCollapsed,
  onToolsQueryChange,
  onUploadSkillFile,
  onUploadFiles,
}: {
  activeWorkspace: WorkspaceKey;
  agentsSub: AgentManagementSub;
  agentPresetCount: number;
  agentPresets: AgentPreset[];
  artifacts: Artifact[];
  chatSub: ChatWorkspaceSub;
  collapsed: boolean;
  capabilities: CapabilityDefinition[];
  capabilityCount: number;
  conversations: ApiConversation[];
  creatingAgentPreset: boolean;
  creatingModel: boolean;
  systemSub: SystemManagementSub;
  models: ModelProvider[];
  onlyOfficeEnabled: boolean;
  mcpCount: number;
  mcpServers: MCPServer[];
  projectError: string | null;
  projects: ApiProject[];
  orchestrationMode: OrchestrationMode;
  pythonTools: PythonToolEntry[];
  profiles: AgentProfile[];
  savedDags: SavedDagView[];
  selectedDagId: string;
  selectedAgentPresetId: string;
  selectedConversationId: string;
  selectedModelId: string;
  selectedProjectId: string;
  selectedProfileId: string;
  selectedToolCapabilityId: string;
  selectedToolMcpName: string;
  selectedToolMcpToolId: string;
  selectedToolSkillName: string;
  selectedSkillDetail: SkillDetail | null;
  selectedSkillFilePath: string;
  skills: SkillSummary[];
  skillCount: number;
  toolsSub: ToolDirectoryTab;
  toolsQuery: string;
  onCreateArtifact: () => void;
  onCreateAgentPreset: () => void;
  onChatSubChange: (sub: ChatWorkspaceSub) => void;
  onCreateMcp: () => void;
  onCreateModel: () => void;
  onCreateProject: () => void;
  onCreateProfile: () => void;
  onCreateTool: () => void;
  onDeleteArtifact: (artifactId: string) => void;
  onEditArtifact: (artifactId: string) => void;
  onImportSkill: () => void;
  onLoadDag: (saved: SavedDagView) => void;
  onNewChat: () => void;
  onNewProjectConversation: (projectId: string) => void;
  onNewDag: () => void;
  onDeleteConversation: (id: string) => void;
  onSelectAgentPreset: (id: string) => void;
  onSelectConversation: (id: string) => void;
  onSelectProfile: (id: string) => void;
  onSelectProject: (id: string) => void;
  onSelectModel: (id: string) => void;
  onSelectSkillFile: (filePath: string | null) => void;
  onSelectToolCapability: (id: string) => void;
  onSelectToolMcp: (name: string, toolId?: string | null) => void;
  onSelectToolSkill: (name: string) => void;
  onSelectWorkspace: (workspace: WorkspaceKey) => void;
  onOrchestrationModeChange: (mode: OrchestrationMode) => void;
  onAgentsSubChange: (sub: AgentManagementSub) => void;
  onSystemSubChange: (sub: SystemManagementSub) => void;
  onToolsSubChange: (tab: ToolDirectoryTab) => void;
  onToggleCollapsed: () => void;
  onToolsQueryChange: (query: string) => void;
  onUploadSkillFile: (file: File | undefined) => void;
  onUploadFiles: (files: FileList | null) => void;
}) {
  const standaloneConversationCount = conversations.filter((conversation) => (
    conversation.kind === 'chat' && !conversation.project_id
  )).length;
  const orchestrationSubnav = [
    { key: 'dynamic' as const, label: '动态编排', icon: <Play size={16} />, count: 'DAG' },
    { key: 'static' as const, label: '静态编排', icon: <GitBranch size={16} />, count: savedDags.length },
  ];
  const chatSubnav = [
    { key: 'conversations' as const, label: '会话', icon: <MessageSquare size={16} />, count: standaloneConversationCount },
    { key: 'projects' as const, label: '项目', icon: <Folder size={16} />, count: projects.length },
  ];
  const toolSubnav = [
    { key: 'tools' as const, label: '工具', icon: <Wrench size={16} />, count: capabilityCount },
    { key: 'skills' as const, label: '技能', icon: <FileText size={16} />, count: skillCount },
    { key: 'mcp' as const, label: 'MCP 服务', icon: <Database size={16} />, count: mcpCount },
  ];
  const agentSubnav = [
    { key: 'profiles' as const, label: '角色设定', icon: <UserCog size={16} />, count: profiles.length },
    { key: 'presets' as const, label: '智能体预设', icon: <Bot size={16} />, count: agentPresetCount },
  ];
  const systemSubnav = [
    { key: 'models' as const, label: '模型管理', icon: <SlidersHorizontal size={16} />, count: models.length },
    { key: 'onlyoffice' as const, label: '文档预览配置', icon: <Settings size={16} />, count: onlyOfficeEnabled ? 'ON' : 'OFF' },
  ];
  const normalizedToolsQuery = normalizeSearchQuery(toolsQuery);
  const sidebarToolTree = buildToolManagementTree(capabilities, pythonTools, normalizedToolsQuery);
  const sidebarCapabilities = [
    ...sidebarToolTree.builtin.items.map((item) => item.capability),
    ...sidebarToolTree.pythonSources.flatMap((source) => source.items.map((item) => item.capability)),
    ...sidebarToolTree.manual.items.map((item) => item.capability),
  ];
  const sidebarCustomToolCount = sidebarToolTree.pythonSources.reduce((total, source) => total + source.items.length, 0)
    + sidebarToolTree.manual.items.length;
  const sidebarSkills = skills.filter((skill) => matchesSkillQuery(skill, normalizedToolsQuery));
  const sidebarMcpTree = buildMcpManagementTree(mcpServers, normalizedToolsQuery);
  const activeToolSubnav = toolSubnav.find((item) => item.key === toolsSub) ?? toolSubnav[0];
  const activeAgentSubnav = agentSubnav.find((item) => item.key === agentsSub) ?? agentSubnav[0];
  const skillFileGroups = Object.entries(selectedSkillDetail?.linked_files ?? {})
    .filter(([, files]) => files.length);
  const [historyQuery, setHistoryQuery] = useState('');
  const [dagListQuery, setDagListQuery] = useState('');
  const [modelQuery, setModelQuery] = useState('');
  const [agentQuery, setAgentQuery] = useState('');
  const [expandedProjectIds, setExpandedProjectIds] = useState<Set<string>>(() => new Set());
  const [expandedSkillNames, setExpandedSkillNames] = useState<Set<string>>(() => new Set());
  const [expandedSkillFolders, setExpandedSkillFolders] = useState<Set<string>>(() => new Set());
  const [collapsedResourceTreeKeys, setCollapsedResourceTreeKeys] = useState<Set<string>>(() => new Set());
  // 手风琴式子菜单：同一时间最多展开一个，展开新的会收起其它。
  const [expandedMenu, setExpandedMenu] = useState<WorkspaceKey | null>(activeWorkspace);
  const normalizedHistoryQuery = normalizeSearchQuery(historyQuery);
  const normalizedDagListQuery = normalizeSearchQuery(dagListQuery);
  const normalizedModelQuery = normalizeSearchQuery(modelQuery);
  const normalizedAgentQuery = normalizeSearchQuery(agentQuery);
  const visibleConversations = conversations.filter((conversation) => conversation.kind === 'chat' && !conversation.project_id && matchesSearchQuery(
    [
      conversation.id,
      conversation.title,
      conversation.status,
    ],
    normalizedHistoryQuery,
  ));
  const projectConversationsByProjectId = new Map<string, ApiConversation[]>();
  conversations.forEach((conversation) => {
    if (conversation.kind !== 'chat') return;
    if (!conversation.project_id) return;
    const items = projectConversationsByProjectId.get(conversation.project_id) ?? [];
    items.push(conversation);
    projectConversationsByProjectId.set(conversation.project_id, items);
  });
  function projectConversationMatchesSearch(conversation: ApiConversation, query: string) {
    return matchesSearchQuery(
      [conversation.id, conversation.title, conversation.status],
      query,
    );
  }
  function projectMatchesSearch(project: ApiProject, query: string) {
    return matchesSearchQuery(
      [project.id, project.name, project.slug, project.description],
      query,
    );
  }
  const visibleProjects = projects.filter((project) => {
    const projectConversations = projectConversationsByProjectId.get(project.id) ?? [];
    return projectMatchesSearch(project, normalizedHistoryQuery) || projectConversations.some((conversation) =>
      projectConversationMatchesSearch(conversation, normalizedHistoryQuery)
    );
  });
  const selectedSidebarConversation = conversations.find((conversation) => (
    conversation.id === selectedConversationId && conversation.kind === 'chat'
  )) ?? null;
  const workspaceRootLabel = selectedSidebarConversation
    ? (selectedSidebarConversation.project_id ? '.dagent/projects' : '.dagent/projects/_conversations')
    : chatSub === 'projects' && selectedProjectId ? '.dagent/projects' : '.dagent/runs';
  const visibleSavedDags = savedDags.filter((dag) => matchesSearchQuery(
    [
      dag.savedDagId,
      dag.name,
      dag.description,
      dag.spec.id,
      dag.spec.name,
      dag.spec.description,
      dag.revision,
      dag.spec.nodes.length,
    ],
    normalizedDagListQuery,
  ));
  const visibleModels = models.filter((model) => matchesSearchQuery(
    [model.id, model.name, model.source, model.base_url, model.model],
    normalizedModelQuery,
  ));
  const visibleProfiles = profiles.filter((profile) => matchesSearchQuery(
    [profile.id, profile.name, profile.description, profile.source, profileSourceLabel(profile)],
    normalizedAgentQuery,
  ));
  const visibleAgentPresets = agentPresets.filter((preset) => matchesAgentPresetQuery(preset, normalizedAgentQuery));
  const onCapabilityNavClick = (key: WorkspaceKey) => {
    if (activeWorkspace === key) {
      // 已在该工作区：切换其子菜单展开/收起。
      setExpandedMenu((current) => (current === key ? null : key));
      return;
    }
    // 切到其它工作区：展开它、收起其它。
    onSelectWorkspace(key);
    setExpandedMenu(key);
  };
  const toggleProjectExpansion = (projectId: string) => {
    setExpandedProjectIds((current) => {
      const next = new Set(current);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  };
  const expandProject = (projectId: string) => {
    setExpandedProjectIds((current) => {
      if (current.has(projectId)) return current;
      const next = new Set(current);
      next.add(projectId);
      return next;
    });
  };
  // 通过非侧栏入口（如“新建工具/预设”）切换工作区时，展开目标的子菜单并收起其它。
  useEffect(() => {
    setExpandedMenu(activeWorkspace);
  }, [activeWorkspace]);
  const createCapabilityResource = () => {
    if (toolsSub === 'tools') {
      onCreateTool();
    } else if (toolsSub === 'skills') {
      onImportSkill();
    } else {
      onCreateMcp();
    }
  };
  const capabilityCreateTitle = toolsSub === 'tools'
    ? '导入 Python 工具'
    : toolsSub === 'skills'
      ? '导入技能'
      : '新建 MCP';
  const createAgentResource = () => {
    if (agentsSub === 'profiles') {
      onCreateProfile();
    } else {
      onCreateAgentPreset();
    }
  };
  const agentCreateTitle = agentsSub === 'profiles' ? '新建角色设定' : '新建智能体预设';
  const toggleSkillTree = (name: string) => {
    onSelectToolSkill(name);
    setExpandedSkillNames((current) => nextExpandedSkillNames(current, name, selectedToolSkillName === name));
  };
  const toggleSkillFolder = (name: string, folder: string) => {
    const folderKey = `${name}:${folder}`;
    setExpandedSkillFolders((current) => {
      const next = new Set(current);
      if (next.has(folderKey)) {
        next.delete(folderKey);
      } else {
        next.add(folderKey);
      }
      return next;
    });
  };
  const isResourceTreeOpen = (treeKey: string) => !collapsedResourceTreeKeys.has(treeKey);
  const toggleResourceTreeKey = (treeKey: string) => {
    setCollapsedResourceTreeKeys((current) => {
      const next = new Set(current);
      if (next.has(treeKey)) {
        next.delete(treeKey);
      } else {
        next.add(treeKey);
      }
      return next;
    });
  };
  const toolSourceDisplayName = (label: string): string => label.split(/[\\/]/).filter(Boolean).pop() ?? label;
  const renderToolCapabilityRow = (capability: CapabilityDefinition) => (
    <button
      className={selectedToolCapabilityId === capability.id ? 'active sidebar-skill-file-row sidebar-tool-leaf-row' : 'sidebar-skill-file-row sidebar-tool-leaf-row'}
      key={capability.id}
      onClick={() => onSelectToolCapability(capability.id)}
      type="button"
    >
      <Wrench size={14} />
      <span>{capabilityDisplayName(capability)}</span>
      <em data-enabled={capability.enabled} />
    </button>
  );
  const renderMcpToolRow = (server: MCPServer, capability: CapabilityDefinition) => (
    <button
      className={selectedToolMcpToolId === capability.id ? 'active sidebar-skill-file-row sidebar-tool-leaf-row' : 'sidebar-skill-file-row sidebar-tool-leaf-row'}
      key={capability.id}
      onClick={() => onSelectToolMcp(server.name, capability.id)}
      type="button"
    >
      <Wrench size={14} />
      <span>{capabilityDisplayName(capability)}</span>
      <em data-enabled={server.status === 'connected' && capability.enabled} />
    </button>
  );
  const renderProfileRow = (profile: AgentProfile) => (
    <button
      className={selectedProfileId === profile.id ? 'active sidebar-skill-file-row sidebar-tool-leaf-row' : 'sidebar-skill-file-row sidebar-tool-leaf-row'}
      key={profile.id}
      onClick={() => {
        onAgentsSubChange('profiles');
        onSelectProfile(profile.id);
      }}
      title={profile.description || profileSourceLabel(profile)}
      type="button"
    >
      <UserCog size={14} />
      <span>{profile.name}</span>
      <em data-enabled={profile.editable} />
    </button>
  );
  const renderAgentPresetRow = (preset: AgentPreset) => (
    <button
      className={!creatingAgentPreset && selectedAgentPresetId === preset.id ? 'active sidebar-skill-file-row sidebar-tool-leaf-row' : 'sidebar-skill-file-row sidebar-tool-leaf-row'}
      key={preset.id}
      onClick={() => {
        onAgentsSubChange('presets');
        onSelectAgentPreset(preset.id);
      }}
      title={`${preset.profile} · ${preset.capabilities?.length ?? 0} 能力 · ${preset.skills?.length ?? 0} 技能`}
      type="button"
    >
      <Bot size={14} />
      <span>{preset.name}</span>
      <em data-enabled="true" />
    </button>
  );
  const renderResourceTreeBranch = ({
    treeKey,
    icon,
    label,
    count,
    children,
    title,
    treeClassName,
    active,
    onSelect,
  }: {
    treeKey: string;
    icon: React.ReactNode;
    label: string;
    count: number;
    children: React.ReactNode;
    title?: string;
    treeClassName: string;
    active?: boolean;
    onSelect?: () => void;
  }) => (
    <div className="sidebar-skill-row sidebar-resource-tree-row" key={treeKey}>
      <div className="sidebar-skill-row-main">
        <button
          className={active ? 'active sidebar-resource-tree-select' : 'sidebar-resource-tree-select'}
          onClick={() => {
            onSelect?.();
            toggleResourceTreeKey(treeKey);
          }}
          title={title ?? label}
          type="button"
        >
          {icon}
          <span>{label}</span>
          <code>{count}</code>
        </button>
        <button
          className="sidebar-skill-toggle"
          data-open={isResourceTreeOpen(treeKey)}
          onClick={() => toggleResourceTreeKey(treeKey)}
          title={isResourceTreeOpen(treeKey) ? '收起分类' : '展开分类'}
          type="button"
        >
          <ChevronRight size={13} />
        </button>
      </div>
      {isResourceTreeOpen(treeKey) ? (
        <div className={`sidebar-skill-file-tree ${treeClassName}`}>
          {children}
        </div>
      ) : null}
    </div>
  );
  const builtinToolBranch = sidebarToolTree.builtin.items.length
    ? renderResourceTreeBranch({
        treeKey: 'tool:builtin',
        icon: <Folder size={13} />,
        label: sidebarToolTree.builtin.label,
        count: sidebarToolTree.builtin.items.length,
        treeClassName: 'sidebar-resource-file-tree',
        children: sidebarToolTree.builtin.items.map((item) => renderToolCapabilityRow(item.capability)),
      })
    : null;
  const pythonToolSourceBranches = sidebarToolTree.pythonSources.map((sourceGroup) => (
    renderResourceTreeBranch({
      treeKey: `tool:python:${sourceGroup.id}`,
      icon: <File size={13} />,
      label: toolSourceDisplayName(sourceGroup.label),
      title: sourceGroup.label,
      count: sourceGroup.items.length,
      treeClassName: 'sidebar-resource-file-tree',
      children: sourceGroup.items.map((item) => renderToolCapabilityRow(item.capability)),
    })
  ));
  const pythonToolBranch = sidebarToolTree.pythonSources.length
    ? renderResourceTreeBranch({
        treeKey: 'tool:python',
        icon: <FileText size={13} />,
        label: 'Python 脚本',
        count: sidebarToolTree.pythonSources.reduce((total, source) => total + source.items.length, 0),
        treeClassName: 'sidebar-resource-file-tree',
        children: <>{pythonToolSourceBranches}</>,
      })
    : null;
  const manualToolBranch = sidebarToolTree.manual.items.length
    ? renderResourceTreeBranch({
        treeKey: 'tool:manual',
        icon: <FileText size={13} />,
        label: sidebarToolTree.manual.label,
        count: sidebarToolTree.manual.items.length,
        treeClassName: 'sidebar-resource-file-tree',
        children: sidebarToolTree.manual.items.map((item) => renderToolCapabilityRow(item.capability)),
      })
    : null;
  const customToolBranch = sidebarCustomToolCount
    ? renderResourceTreeBranch({
        treeKey: 'tool:custom',
        icon: <Folder size={13} />,
        label: '自定义工具',
        count: sidebarCustomToolCount,
        treeClassName: 'sidebar-resource-file-tree',
        children: (
          <>
            {pythonToolBranch}
            {manualToolBranch}
          </>
        ),
      })
    : null;
  const renderToolTree = () => (
    sidebarCapabilities.length ? (
      <>
        {builtinToolBranch}
        {customToolBranch}
      </>
    ) : <div className="sidebar-empty-row">没有匹配的工具</div>
  );
  const renderMcpTree = () => (
    sidebarMcpTree.length ? (
      <>
        {sidebarMcpTree.map(({ server, tools }) => (
          renderResourceTreeBranch({
            treeKey: `mcp:${server.name}`,
            icon: <Database size={13} />,
            label: server.name,
            title: server.name,
            count: tools.length,
            treeClassName: 'sidebar-resource-file-tree',
            active: selectedToolMcpName === server.name && !selectedToolMcpToolId,
            onSelect: () => onSelectToolMcp(server.name, null),
            children: tools.length
              ? tools.map((capability) => renderMcpToolRow(server, capability))
              : <div className="sidebar-empty-row">暂无工具</div>,
          })
        ))}
      </>
    ) : <div className="sidebar-empty-row">暂无 MCP 服务</div>
  );
  const builtinProfiles = visibleProfiles.filter((profile) => profile.source === 'builtin');
  const customProfiles = visibleProfiles.filter((profile) => profile.source !== 'builtin');
  const builtinProfileBranch = renderResourceTreeBranch({
    treeKey: 'agent:profiles:builtin',
    icon: <UserCog size={13} />,
    label: '内置',
    count: builtinProfiles.length,
    treeClassName: 'sidebar-resource-file-tree',
    children: builtinProfiles.length
      ? builtinProfiles.map((profile) => renderProfileRow(profile))
      : <div className="sidebar-empty-row">{normalizedAgentQuery ? '没有匹配的内置角色设定' : '暂无内置角色设定'}</div>,
  });
  const customProfileBranch = renderResourceTreeBranch({
    treeKey: 'agent:profiles:custom',
    icon: <Folder size={13} />,
    label: '自定义',
    count: customProfiles.length,
    treeClassName: 'sidebar-resource-file-tree',
    children: customProfiles.length
      ? customProfiles.map((profile) => renderProfileRow(profile))
      : <div className="sidebar-empty-row">{normalizedAgentQuery ? '没有匹配的自定义角色设定' : '暂无自定义角色设定'}</div>,
  });
  const renderProfileTree = () => (
    <>
      {builtinProfileBranch}
      {customProfileBranch}
    </>
  );
  const renderAgentPresetList = () => (
    visibleAgentPresets.length
      ? visibleAgentPresets.map((preset) => renderAgentPresetRow(preset))
      : <div className="sidebar-empty-row">{normalizedAgentQuery ? '没有匹配的智能体预设' : '暂无智能体预设'}</div>
  );

  return (
    <aside className="workspace-sidebar" data-collapsed={collapsed}>
      <div className="sidebar-brand-row">
        <button className="brand-mark" onClick={collapsed ? onToggleCollapsed : undefined} title={collapsed ? '展开侧栏' : 'dagent'} type="button">
          <img className="brand-logo-glyph" src={dagentMark} alt="dagent" />
          <ChevronRight className="brand-logo-expand" size={19} />
        </button>
        <div className="sidebar-brand-copy">
          <strong>dagent</strong>
          <span>Agent DAG Harness</span>
        </div>
        <button className="sidebar-collapse-button" onClick={onToggleCollapsed} title="收起 / 展开" type="button">
          {collapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
        </button>
      </div>

      <div className="sidebar-label">工作区</div>
      <nav className="sidebar-nav" aria-label="Workspace navigation">
        {workspaceItems.map((item) => {
          if (item.key === 'chat') {
            return (
              <div className="sidebar-capability-nav" key={item.key}>
                <button
                  className={activeWorkspace === item.key ? 'active sidebar-capability-button' : 'sidebar-capability-button'}
                  onClick={() => onCapabilityNavClick(item.key)}
                  title={item.label}
                  type="button"
                >
                  {item.icon}
                  <span>{item.label}</span>
                  <span className="sidebar-capability-chevron" data-open={expandedMenu === item.key}>
                    <ChevronRight size={14} />
                  </span>
                </button>
                {expandedMenu === item.key ? (
                  <div className="sidebar-subnav nested">
                    {chatSubnav.map((subitem) => (
                      <button
                        className={chatSub === subitem.key ? 'active' : ''}
                        key={subitem.key}
                        onClick={() => {
                          onSelectWorkspace('chat');
                          onChatSubChange(subitem.key);
                        }}
                        title={subitem.label}
                        type="button"
                      >
                        {subitem.icon}
                        <span>{subitem.label}</span>
                        <em>{subitem.count}</em>
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          }
          if (item.key === 'orchestration') {
            return (
              <div className="sidebar-capability-nav" key={item.key}>
                <button
                  className={activeWorkspace === item.key ? 'active sidebar-capability-button' : 'sidebar-capability-button'}
                  onClick={() => onCapabilityNavClick(item.key)}
                  title={item.label}
                  type="button"
                >
                  {item.icon}
                  <span>{item.label}</span>
                  <span className="sidebar-capability-chevron" data-open={expandedMenu === item.key}>
                    <ChevronRight size={14} />
                  </span>
                </button>
                {expandedMenu === item.key ? (
                  <div className="sidebar-subnav nested">
                    {orchestrationSubnav.map((subitem) => (
                      <button
                        className={orchestrationMode === subitem.key ? 'active' : ''}
                        key={subitem.key}
                        onClick={() => {
                          onSelectWorkspace('orchestration');
                          onOrchestrationModeChange(subitem.key);
                        }}
                        title={subitem.label}
                        type="button"
                      >
                        {subitem.icon}
                        <span>{subitem.label}</span>
                        <em>{subitem.count}</em>
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          }
          if (item.key === 'tools') {
            return (
              <div className="sidebar-capability-nav" key={item.key}>
                <button
                  className={activeWorkspace === item.key ? 'active sidebar-capability-button' : 'sidebar-capability-button'}
                  onClick={() => onCapabilityNavClick(item.key)}
                  title={item.label}
                  type="button"
                >
                  {item.icon}
                  <span>{item.label}</span>
                  <span className="sidebar-capability-chevron" data-open={expandedMenu === item.key}>
                    <ChevronRight size={14} />
                  </span>
                </button>
                {expandedMenu === item.key ? (
                  <div className="sidebar-subnav nested">
                    {toolSubnav.map((subitem) => (
                      <button
                        className={toolsSub === subitem.key ? 'active' : ''}
                        key={subitem.key}
                        onClick={() => {
                          onSelectWorkspace('tools');
                          onToolsSubChange(subitem.key);
                        }}
                        title={subitem.label}
                        type="button"
                      >
                        {subitem.icon}
                        <span>{subitem.label}</span>
                        <em>{subitem.count}</em>
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          }
          if (item.key === 'agents') {
            return (
              <div className="sidebar-capability-nav" key={item.key}>
                <button
                  className={activeWorkspace === item.key ? 'active sidebar-capability-button' : 'sidebar-capability-button'}
                  onClick={() => onCapabilityNavClick(item.key)}
                  title={item.label}
                  type="button"
                >
                  {item.icon}
                  <span>{item.label}</span>
                  <span className="sidebar-capability-chevron" data-open={expandedMenu === item.key}>
                    <ChevronRight size={14} />
                  </span>
                </button>
                {expandedMenu === item.key ? (
                  <div className="sidebar-subnav nested">
                    {agentSubnav.map((subitem) => (
                      <button
                        className={agentsSub === subitem.key ? 'active' : ''}
                        key={subitem.key}
                        onClick={() => {
                          onSelectWorkspace('agents');
                          onAgentsSubChange(subitem.key);
                        }}
                        title={subitem.label}
                        type="button"
                      >
                        {subitem.icon}
                        <span>{subitem.label}</span>
                        <em>{subitem.count}</em>
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          }
          if (item.key === 'system') {
            return (
              <div className="sidebar-capability-nav" key={item.key}>
                <button
                  className={activeWorkspace === item.key ? 'active sidebar-capability-button' : 'sidebar-capability-button'}
                  onClick={() => onCapabilityNavClick(item.key)}
                  title={item.label}
                  type="button"
                >
                  {item.icon}
                  <span>{item.label}</span>
                  <span className="sidebar-capability-chevron" data-open={expandedMenu === item.key}>
                    <ChevronRight size={14} />
                  </span>
                </button>
                {expandedMenu === item.key ? (
                  <div className="sidebar-subnav nested">
                    {systemSubnav.map((subitem) => (
                      <button
                        className={systemSub === subitem.key ? 'active' : ''}
                        key={subitem.key}
                        onClick={() => {
                          onSelectWorkspace('system');
                          onSystemSubChange(subitem.key);
                        }}
                        title={subitem.label}
                        type="button"
                      >
                        {subitem.icon}
                        <span>{subitem.label}</span>
                        <em>{subitem.count}</em>
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          }
          return (
            <button
              key={item.key}
              className={activeWorkspace === item.key ? 'active' : ''}
              onClick={() => onSelectWorkspace(item.key)}
              title={item.label}
              type="button"
            >
              {item.icon}
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>

      {activeWorkspace === 'chat' && chatSub === 'conversations' ? (
        <section className="sidebar-history">
          <div className="sidebar-history-head">
            <span>会话</span>
            <button onClick={onNewChat} title="新建会话" type="button">
              <Plus size={14} />
            </button>
          </div>
          <SidebarSearchField
            value={historyQuery}
            onChange={setHistoryQuery}
          />
          {projectError ? <div className="sidebar-error-row">{projectError}</div> : null}
          <div className="sidebar-history-list">
            {visibleConversations.length ? visibleConversations.map((conversation) => (
              <div
                className="sidebar-conversation-row"
                key={conversation.id}
              >
                <button
                  className={conversation.id === selectedConversationId ? 'active' : ''}
                  onClick={() => onSelectConversation(conversation.id)}
                  type="button"
                >
                  <span>
                    <MessageSquare size={13} />
                    <strong>{conversation.title}</strong>
                  </span>
                  <em>{conversation.status}</em>
                </button>
                <button
                  className="sidebar-conversation-delete"
                  onClick={() => onDeleteConversation(conversation.id)}
                  title="删除会话"
                  type="button"
                >
                  <Trash2 size={12} />
                </button>
              </div>
            )) : (
              <div className="sidebar-empty-row">
                {normalizedHistoryQuery ? '没有匹配的会话' : '暂无会话'}
              </div>
            )}
          </div>
        </section>
      ) : null}

      {activeWorkspace === 'chat' && chatSub === 'projects' ? (
        <section className="sidebar-history">
          <div className="sidebar-history-head">
            <span>项目</span>
            <button onClick={onCreateProject} title="新建项目" type="button">
              <Plus size={14} />
            </button>
          </div>
          <SidebarSearchField
            value={historyQuery}
            onChange={setHistoryQuery}
          />
          {projectError ? <div className="sidebar-error-row">{projectError}</div> : null}
          <div className="sidebar-history-list">
            {visibleProjects.length ? visibleProjects.map((project) => {
              const projectConversations = projectConversationsByProjectId.get(project.id) ?? [];
              const displayedProjectConversations = normalizedHistoryQuery && !projectMatchesSearch(project, normalizedHistoryQuery)
                ? projectConversations.filter((conversation) => projectConversationMatchesSearch(conversation, normalizedHistoryQuery))
                : projectConversations;
              const expanded = expandedProjectIds.has(project.id);
              return (
                <div
                  className="sidebar-project-tree-row"
                  key={project.id}
                >
                  <div className="sidebar-project-tree-main">
                    <button
                      className={project.id === selectedProjectId && !selectedConversationId ? 'active sidebar-project-select' : 'sidebar-project-select'}
                      onClick={() => {
                        onSelectProject(project.id);
                        toggleProjectExpansion(project.id);
                      }}
                      title={project.workspace_uri}
                      type="button"
                    >
                      <span>
                        <Folder size={13} />
                        <strong>{project.name}</strong>
                      </span>
                      <em>{projectConversations.length ? `${projectConversations.length} 会话` : project.slug}</em>
                    </button>
                    <button
                      className="sidebar-project-create-conversation"
                      onClick={() => {
                        expandProject(project.id);
                        onNewProjectConversation(project.id);
                      }}
                      title="新建会话"
                      type="button"
                    >
                      <Plus size={13} />
                    </button>
                    <button
                      aria-expanded={expanded}
                      className="sidebar-project-toggle"
                      data-open={expanded}
                      onClick={() => toggleProjectExpansion(project.id)}
                      title={expanded ? '收起会话' : '展开会话'}
                      type="button"
                    >
                      <ChevronRight size={13} />
                    </button>
                  </div>
                  {expanded ? (
                    <div className="sidebar-project-conversation-tree">
                      {displayedProjectConversations.length ? displayedProjectConversations.map((conversation) => (
                        <div className="sidebar-project-conversation-row" key={conversation.id}>
                          <button
                            className={conversation.id === selectedConversationId ? 'active' : ''}
                            onClick={() => onSelectConversation(conversation.id)}
                            type="button"
                          >
                            <span>
                              <MessageSquare size={12} />
                              <strong>{conversation.title}</strong>
                            </span>
                            <em>{conversation.status}</em>
                          </button>
                          <button
                            className="sidebar-conversation-delete"
                            onClick={() => onDeleteConversation(conversation.id)}
                            title="删除会话"
                            type="button"
                          >
                            <Trash2 size={12} />
                          </button>
                        </div>
                      )) : (
                        <div className="sidebar-empty-row">{normalizedHistoryQuery ? '没有匹配的会话' : '暂无会话'}</div>
                      )}
                    </div>
                  ) : null}
                </div>
              );
            }) : (
              <div className="sidebar-empty-row">{normalizedHistoryQuery ? '没有匹配的项目' : '暂无项目'}</div>
            )}
          </div>
        </section>
      ) : null}

      {activeWorkspace === 'orchestration' && orchestrationMode === 'static' ? (
        <section className="sidebar-context-section">
          <div className="sidebar-history-head">
            <span>编排列表</span>
            <button onClick={onNewDag} title="新建编排" type="button">
              <Plus size={14} />
            </button>
          </div>
          <SidebarSearchField
            value={dagListQuery}
            onChange={setDagListQuery}
          />
          <div className="sidebar-context-list">
            {visibleSavedDags.length ? visibleSavedDags.map((item) => (
              <button
                className={item.savedDagId === selectedDagId ? 'active' : ''}
                key={item.savedDagId}
                onClick={() => onLoadDag(item)}
                title={item.name || item.spec.name || item.spec.id}
                type="button"
              >
                <span>
                  <GitBranch size={13} />
                  <strong>{item.name || item.spec.name || item.spec.id}</strong>
                  <code>v{item.revision}</code>
                </span>
                <em>{item.description || item.spec.description || `${item.spec.nodes.length} 节点`}</em>
              </button>
            )) : (
              <div className="sidebar-empty-row">{normalizedDagListQuery ? '没有匹配的编排' : '暂无编排'}</div>
            )}
          </div>
        </section>
      ) : null}

      {activeWorkspace === 'orchestration' && orchestrationMode === 'static' ? (
        <section className="sidebar-artifact-section">
          <div className="sidebar-artifact-head">
            <span>Artifacts</span>
            <UploadPicker variant="sidebar" onUploadFiles={onUploadFiles} />
            <button className="sidebar-artifact-icon" onClick={onCreateArtifact} title="添加路径" type="button">
              <Plus size={13} />
            </button>
          </div>
          <div className="sidebar-artifact-list">
            {artifacts.length ? artifacts.map((artifact) => (
              <div className="sidebar-artifact-row" key={artifact.id}>
                <button
                  className="sidebar-artifact-main"
                  onClick={() => onEditArtifact(artifact.id)}
                  title={`编辑 ${artifactDisplayPath(artifact)}`}
                  type="button"
                >
                  <span>{artifactKindLabel(artifact)}</span>
                  <strong>{artifactDisplayName(artifact)}</strong>
                </button>
                <button className="sidebar-artifact-delete" onClick={() => onDeleteArtifact(artifact.id)} title="删除 artifact" type="button">
                  <X size={11} />
                </button>
              </div>
            )) : (
              <div className="sidebar-empty-row">暂无 artifacts</div>
            )}
          </div>
        </section>
      ) : null}

      {activeWorkspace === 'tools' ? (
        <section className="sidebar-context-section capability-resource-section">
          <div className="sidebar-tool-list-head">
            <span>{activeToolSubnav.label}</span>
            <button onClick={createCapabilityResource} title={capabilityCreateTitle} type="button">
              {toolsSub === 'skills' || toolsSub === 'tools' ? <Upload size={14} /> : <Plus size={14} />}
            </button>
          </div>
          <SidebarSearchField
            value={toolsQuery}
            onChange={onToolsQueryChange}
          />
          <div className="sidebar-tool-list">
            {toolsSub === 'tools' ? (
              renderToolTree()
            ) : toolsSub === 'skills' ? (
              sidebarSkills.length ? sidebarSkills.map((skill) => {
                const name = skillLookupName(skill);
                const isSelectedSkill = selectedToolSkillName === name;
                const isSkillTreeOpen = expandedSkillNames.has(name);
                return (
                  <div className="sidebar-skill-row" key={skill.path}>
                    <div className="sidebar-skill-row-main">
                      <button
                        className={isSelectedSkill ? 'active sidebar-skill-select' : 'sidebar-skill-select'}
                        onClick={() => toggleSkillTree(name)}
                        type="button"
                      >
                        <FileText size={14} />
                        <span>{name}</span>
                        <em data-enabled={skill.managed} />
                      </button>
                      <button
                        className="sidebar-skill-toggle"
                        data-open={isSkillTreeOpen}
                        onClick={() => toggleSkillTree(name)}
                        title={isSkillTreeOpen ? '收起文件树' : '展开文件树'}
                        type="button"
                      >
                        <ChevronRight size={13} />
                      </button>
                    </div>
                    {isSelectedSkill && isSkillTreeOpen ? (
                      <div className="sidebar-skill-file-tree">
                        {selectedSkillDetail ? (
                          <>
                            <button
                              className={!selectedSkillFilePath ? 'active sidebar-skill-file-row' : 'sidebar-skill-file-row'}
                              onClick={() => onSelectSkillFile(null)}
                              type="button"
                            >
                              <FileText size={13} />
                              <span>SKILL.md</span>
                            </button>
                            {skillFileGroups.map(([folder, files]) => (
                              <div className="sidebar-skill-file-group" key={folder}>
                                <button
                                  className="sidebar-skill-folder-toggle"
                                  data-open={expandedSkillFolders.has(`${name}:${folder}`)}
                                  onClick={() => toggleSkillFolder(name, folder)}
                                  type="button"
                                >
                                  <ChevronRight size={12} />
                                  <Folder size={13} />
                                  <span>{folder}</span>
                                </button>
                                {expandedSkillFolders.has(`${name}:${folder}`) ? files.map((filePath) => (
                                  <button
                                    className={selectedSkillFilePath === filePath ? 'active sidebar-skill-file-row' : 'sidebar-skill-file-row'}
                                    key={filePath}
                                    onClick={() => onSelectSkillFile(filePath)}
                                    type="button"
                                  >
                                    <FileText size={13} />
                                    <span>{filePath.split('/').pop() ?? filePath}</span>
                                  </button>
                                )) : null}
                              </div>
                            ))}
                          </>
                        ) : (
                          <div className="sidebar-empty-row">正在加载技能文件</div>
                        )}
                        <label className="sidebar-skill-upload">
                          <Plus size={12} />
                          添加文件
                          <input
                            type="file"
                            accept=".md,text/markdown,text/plain,.zip,application/zip"
                            onChange={(event) => {
                              onUploadSkillFile(event.target.files?.[0]);
                              event.currentTarget.value = '';
                            }}
                          />
                        </label>
                      </div>
                    ) : null}
                  </div>
                );
              }) : <div className="sidebar-empty-row">没有匹配的技能</div>
            ) : (
              renderMcpTree()
            )}
          </div>
        </section>
      ) : null}

      {activeWorkspace === 'system' && systemSub === 'models' ? (
        <section className="sidebar-context-section">
          <div className="sidebar-history-head">
            <span>模型列表</span>
            <button onClick={onCreateModel} title="新建模型" type="button">
              <Plus size={14} />
            </button>
          </div>
          <SidebarSearchField
            value={modelQuery}
            onChange={setModelQuery}
          />
          <div className="sidebar-context-list sidebar-model-list">
            {visibleModels.length ? visibleModels.map((model) => (
              <button
                className={!creatingModel && selectedModelId === model.id ? 'active' : ''}
                key={model.id}
                onClick={() => onSelectModel(model.id)}
                type="button"
              >
                <span>
                  <SlidersHorizontal size={13} />
                  <strong>{model.name || model.model}</strong>
                  <code>{model.active ? '当前' : model.source}</code>
                </span>
                <em>{model.model}</em>
              </button>
            )) : <div className="sidebar-empty-row">{normalizedModelQuery ? '没有匹配的模型' : '暂无模型'}</div>}
          </div>
        </section>
      ) : null}

      {activeWorkspace === 'agents' ? (
        <section className="sidebar-context-section agent-config-list">
          <div className="sidebar-history-head">
            <span>{activeAgentSubnav.label}</span>
            <button onClick={createAgentResource} title={agentCreateTitle} type="button">
              <Plus size={14} />
            </button>
          </div>
          <SidebarSearchField
            value={agentQuery}
            onChange={setAgentQuery}
          />
          <div className="sidebar-tool-list sidebar-agent-resource-list">
            {agentsSub === 'profiles' ? renderProfileTree() : renderAgentPresetList()}
          </div>
        </section>
      ) : null}

      <div className="sidebar-foot">
        <div className="workspace-root-chip">
          <span />
          <code>{workspaceRootLabel}</code>
        </div>
        <div className="sidebar-user">
          <div>RX</div>
          <span>
            <strong>RobotSe7en</strong>
            <em>本地运行</em>
          </span>
        </div>
      </div>
    </aside>
  );
}

function DesignWorkspacePlaceholder({
  workspace,
  onBackToChat,
}: {
  workspace: WorkspaceKey;
  onBackToChat: () => void;
}) {
  const label = workspace === 'chat' ? '智能工作台' : workspacePlaceholderLabels[workspace];
  return (
    <section className="design-workspace-placeholder">
      <div>
        <div className="design-workspace-placeholder-icon">
          <GitBranch size={26} />
        </div>
        <strong>{label}</strong>
        <p>先评审「智能工作台」这一版的视觉方案。确认方向后,我会把同一套设计语言铺到这个工作区。</p>
        <button onClick={onBackToChat} type="button">
          ← 返回智能工作台
        </button>
      </div>
    </section>
  );
}

function ChatWorkspace({
  artifactListError,
  artifactListLoading,
  artifactPanelOpen,
  artifactPreview,
  artifactPreviewError,
  artifactPreviewLoading,
  artifacts,
  chatScopeLabel,
  currentDag,
  draft,
  error,
  loading,
  messageListRef,
  messages,
  pendingUploads,
  projectName,
  conversationTitle,
  reviewLevel,
  selectedArtifact,
  selectedArtifactId,
  target,
  validationEnabled,
  validationError,
  validationPending,
  onArtifactCopy,
  onArtifactRefresh,
  onArtifactSelect,
  onDraftChange,
  onOpenDag,
  onOpenScope,
  onReviewLevelChange,
  onRemoveUpload,
  onRun,
  onStop,
  onTargetChange,
  onToggleArtifacts,
  onToggleValidation,
  onUploadFiles,
}: {
  artifactListError: string | null;
  artifactListLoading: boolean;
  artifactPanelOpen: boolean;
  artifactPreview: RunArtifactPreview | null;
  artifactPreviewError: string | null;
  artifactPreviewLoading: boolean;
  artifacts: WorkbenchArtifactItem[];
  chatScopeLabel: string;
  currentDag: Dag;
  draft: string;
  error: string | null;
  loading: boolean;
  messageListRef: React.RefObject<HTMLDivElement | null>;
  messages: ChatMessage[];
  pendingUploads: File[];
  projectName: string | null;
  conversationTitle: string | null;
  reviewLevel: ReviewLevel;
  selectedArtifact: WorkbenchArtifactItem | null;
  selectedArtifactId: string;
  target: ChatTarget;
  validationEnabled: boolean;
  validationError: string | null;
  validationPending: boolean;
  onArtifactCopy: () => void;
  onArtifactRefresh: () => void;
  onArtifactSelect: (id: string) => void;
  onDraftChange: (value: string) => void;
  onOpenDag: (dag: Dag, trace?: TraceLogEvent[]) => void;
  onOpenScope: () => void;
  onReviewLevelChange: (value: ReviewLevel) => void;
  onRemoveUpload: (indexes: number[]) => void;
  onRun: () => void;
  onStop: () => void;
  onTargetChange: (value: ChatTarget) => void;
  onToggleArtifacts: () => void;
  onToggleValidation: () => void;
  onUploadFiles: (files: FileList | null) => void;
}) {
  const title = currentChatTitle(messages);
  const sessionLabel = projectName && conversationTitle
    ? `${projectName} / ${conversationTitle}`
    : conversationTitle
      ? conversationTitle
      : 'local session';
  const [pendingUploadsExpanded, setPendingUploadsExpanded] = useState(false);
  const pendingUploadGroups = useMemo(() => buildPendingUploadGroups(pendingUploads), [pendingUploads]);
  const visiblePendingUploads = useMemo(
    () => visiblePendingUploadGroups(pendingUploadGroups, pendingUploadsExpanded, 4),
    [pendingUploadGroups, pendingUploadsExpanded],
  );

  useEffect(() => {
    if (!pendingUploads.length) setPendingUploadsExpanded(false);
  }, [pendingUploads.length]);

  return (
    <section className={`chat-workspace ${artifactPanelOpen ? 'with-artifacts' : 'without-artifacts'}`}>
      <div className="chat-main">
        <div className="chat-scroll" ref={messageListRef}>
          <div className="conversation-frame">
            <div className="conversation-meta">
              <strong>{title}</strong>
              <span />
              <code>{sessionLabel} · {messages.length} turns</code>
            </div>
            {error ? <div className="error-banner">{error}</div> : null}
            {messages.length === 0 ? (
              <DesignEmptyConversation />
            ) : messages.map((message, index) => (
                <ChatMessageRow
                  key={`${message.role}-${index}`}
                  loading={loading && index === messages.length - 1}
                  message={message}
                  onOpenDag={onOpenDag}
                />
              ))}
          </div>
        </div>

        <div className="composer-shell">
          <div className="composer-card">
            <textarea
              value={draft}
              onChange={(event) => onDraftChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) onRun();
              }}
              placeholder="描述一个任务,或请求规划、审查、执行结果…"
            />
            {pendingUploadGroups.length ? (
              <div className="pending-upload-list">
                {visiblePendingUploads.groups.map((group) => {
                  const detail = group.kind === 'folder'
                    ? `${group.fileCount} 个文件`
                    : formatFileSize(group.size);
                  return (
                    <div className="pending-upload-row" key={group.key}>
                      {group.kind === 'folder' ? <Folder size={14} /> : <File size={14} />}
                      <span title={group.paths.join('\n')}>{group.label}</span>
                      <em>{group.kind === 'folder' ? `${detail} · ${formatFileSize(group.size)}` : detail}</em>
                      <button
                        className="icon-button"
                        disabled={loading}
                        onClick={() => onRemoveUpload(group.indexes)}
                        title="移除"
                        type="button"
                      >
                        <X size={13} />
                      </button>
                    </div>
                  );
                })}
                {visiblePendingUploads.hiddenCount ? (
                  <button
                    className="pending-upload-more"
                    disabled={loading}
                    onClick={() => setPendingUploadsExpanded(true)}
                    type="button"
                  >
                    另有 {visiblePendingUploads.hiddenCount} 项
                  </button>
                ) : pendingUploadsExpanded && pendingUploadGroups.length > 4 ? (
                  <button
                    className="pending-upload-more"
                    disabled={loading}
                    onClick={() => setPendingUploadsExpanded(false)}
                    type="button"
                  >
                    收起
                  </button>
                ) : null}
              </div>
            ) : null}
            <div className="composer-toolbar">
              <UploadPicker disabled={loading} variant="composer" onUploadFiles={onUploadFiles} />
              <div className="mode-switch" aria-label="Agent target">
                {(['auto', 'dag', 'tool'] as ChatTarget[]).map((item) => (
                  <button
                    key={item}
                    className={target === item ? 'active' : ''}
                    onClick={() => onTargetChange(item)}
                    type="button"
                  >
                    {item}
                  </button>
                ))}
              </div>
              <select
                className="review-select"
                value={reviewLevel}
                onChange={(event) => onReviewLevelChange(event.target.value as ReviewLevel)}
                aria-label="Review level"
              >
                {reviewLevels.map((level) => (
                  <option key={level} value={level}>
                    {level} review
                  </option>
                ))}
              </select>
              <button
                className={`validation-toggle ${validationEnabled ? 'active' : ''} ${validationError ? 'error' : ''}`}
                type="button"
                onClick={onToggleValidation}
                disabled={validationPending}
                title={validationError ?? 'Validate final answers against the user request'}
                aria-pressed={validationEnabled}
              >
                <span />
                {validationPending ? 'Validation saving' : validationEnabled ? 'Validation on' : validationError ? 'Validation error' : 'Validation off'}
              </button>
              <button
                className="secondary-button compact-button scope-button"
                onClick={onOpenScope}
                title="选择能力"
                type="button"
              >
                <SlidersHorizontal size={15} />
                {chatScopeLabel}
              </button>
              {currentDag.nodes.length ? <StatusBadge status={currentDag.status} /> : null}
              <button className="primary-button chat-send-button" onClick={loading ? onStop : onRun} type="button">
                {loading ? '停止' : '发送'}
                {loading ? <CircleStop size={16} /> : <Send size={16} />}
              </button>
            </div>
          </div>
        </div>
      </div>

      <ArtifactPanel
        error={artifactListError}
        loading={artifactListLoading}
        preview={artifactPreview}
        previewError={artifactPreviewError}
        previewLoading={artifactPreviewLoading}
        artifacts={artifacts}
        open={artifactPanelOpen}
        selectedArtifact={selectedArtifact}
        selectedArtifactId={selectedArtifactId}
        onCopy={onArtifactCopy}
        onRefresh={onArtifactRefresh}
        onSelect={onArtifactSelect}
        onToggle={onToggleArtifacts}
      />
    </section>
  );
}

function ProjectDetailWorkspace({
  error,
  fileDialog,
  fileDraft,
  files,
  filesError,
  filesLoading,
  path,
  preview,
  previewError,
  previewLoading,
  project,
  selectedFile,
  onCreateFolder,
  onDeleteFile,
  onDeleteProject,
  onDialogCancel,
  onDialogConfirm,
  onDownloadFile,
  onEditProject,
  onFileDraftChange,
  onFileSelect,
  onNavigateUp,
  onNewConversation,
  onRefresh,
  onRenameFile,
  onUploadFiles,
}: {
  error: string | null;
  fileDialog: { kind: ProjectFileDialogKind; file?: ProjectFileItem } | null;
  fileDraft: string;
  files: ProjectFileItem[];
  filesError: string | null;
  filesLoading: boolean;
  path: string;
  preview: ProjectFilePreview | null;
  previewError: string | null;
  previewLoading: boolean;
  project: ApiProject | null;
  selectedFile: ProjectFileItem | null;
  onCreateFolder: () => void;
  onDeleteFile: (file: ProjectFileItem) => void;
  onDeleteProject: () => void;
  onDialogCancel: () => void;
  onDialogConfirm: () => void;
  onDownloadFile: (file: ProjectFileItem) => void;
  onEditProject: () => void;
  onFileDraftChange: (value: string) => void;
  onFileSelect: (file: ProjectFileItem) => void;
  onNavigateUp: () => void;
  onNewConversation: () => void;
  onRefresh: () => void;
  onRenameFile: (file: ProjectFileItem) => void;
  onUploadFiles: (files: FileList | null) => void;
}) {
  if (!project) {
    return (
      <section className="project-detail-workspace">
        <div className="project-empty-state">
          <Folder size={24} />
          <strong>选择项目</strong>
          <p>在左侧项目列表中选择一个项目后，这里会显示工作目录、文件和项目会话。</p>
        </div>
      </section>
    );
  }

  const updatedAt = new Date(project.updated_at * 1000).toLocaleString();

  return (
    <section className="project-detail-workspace">
      <header className="project-detail-header">
        <div className="project-detail-title">
          <Folder size={19} />
          <div>
            <strong>{project.name}</strong>
            <span>{project.slug}</span>
          </div>
        </div>
        <div className="project-detail-actions">
          <button className="secondary-button compact-button" onClick={onNewConversation} type="button">
            <Plus size={14} />
            新建会话
          </button>
          <button className="secondary-button compact-button" onClick={onEditProject} type="button">
            <FileText size={14} />
            编辑
          </button>
          <button className="secondary-button danger-button compact-button" onClick={onDeleteProject} type="button">
            <Trash2 size={14} />
            删除
          </button>
        </div>
      </header>

      <div className="project-detail-body">
        <section className="project-detail-summary">
          <div className="project-summary-main">
            <span>项目目录</span>
            <code>{project.workspace_uri}</code>
          </div>
          <div className="project-summary-grid">
            <div>
              <span>组织</span>
              <strong>{project.org_id}</strong>
            </div>
            <div>
              <span>所有者</span>
              <strong>{project.owner_user_id}</strong>
            </div>
            <div>
              <span>更新</span>
              <strong>{updatedAt}</strong>
            </div>
          </div>
          {project.description ? <p>{project.description}</p> : null}
          {error ? <div className="project-detail-error">{error}</div> : null}
        </section>

        <ProjectFileManager
          dialog={fileDialog}
          draft={fileDraft}
          error={filesError}
          files={files}
          loading={filesLoading}
          path={path}
          preview={preview}
          previewError={previewError}
          previewLoading={previewLoading}
          project={project}
          selectedFile={selectedFile}
          onCreateFolder={onCreateFolder}
          onDeleteFile={onDeleteFile}
          onDialogCancel={onDialogCancel}
          onDialogConfirm={onDialogConfirm}
          onDownloadFile={onDownloadFile}
          onDraftChange={onFileDraftChange}
          onFileSelect={onFileSelect}
          onNavigateUp={onNavigateUp}
          onRefresh={onRefresh}
          onRenameFile={onRenameFile}
          onUploadFiles={onUploadFiles}
        />
      </div>
    </section>
  );
}

function ProjectFileManager({
  dialog,
  draft,
  error,
  files,
  loading,
  path,
  preview,
  previewError,
  previewLoading,
  selectedFile,
  onCreateFolder,
  onDeleteFile,
  onDialogCancel,
  onDialogConfirm,
  onDownloadFile,
  onDraftChange,
  onFileSelect,
  onNavigateUp,
  onRefresh,
  onRenameFile,
  onUploadFiles,
}: {
  dialog: { kind: ProjectFileDialogKind; file?: ProjectFileItem } | null;
  draft: string;
  error: string | null;
  files: ProjectFileItem[];
  loading: boolean;
  path: string;
  preview: ProjectFilePreview | null;
  previewError: string | null;
  previewLoading: boolean;
  project: ApiProject;
  selectedFile: ProjectFileItem | null;
  onCreateFolder: () => void;
  onDeleteFile: (file: ProjectFileItem) => void;
  onDialogCancel: () => void;
  onDialogConfirm: () => void;
  onDownloadFile: (file: ProjectFileItem) => void;
  onDraftChange: (value: string) => void;
  onFileSelect: (file: ProjectFileItem) => void;
  onNavigateUp: () => void;
  onRefresh: () => void;
  onRenameFile: (file: ProjectFileItem) => void;
  onUploadFiles: (files: FileList | null) => void;
}) {
  const directoryInputProps = {
    directory: '',
    webkitdirectory: '',
  } as React.InputHTMLAttributes<HTMLInputElement> & { directory: string; webkitdirectory: string };
  const onUploadChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    onUploadFiles(event.currentTarget.files);
    event.currentTarget.value = '';
  };

  return (
    <section className="project-file-manager">
      <div className="project-file-toolbar">
        <button className="icon-button" disabled={!path || loading} onClick={onNavigateUp} title="上一级" type="button">
          <ChevronLeft size={15} />
        </button>
        <div className="project-file-path">
          <Folder size={14} />
          <code>{path || '/'}</code>
        </div>
        <button className="icon-button" disabled={loading} onClick={onRefresh} title="刷新" type="button">
          <RefreshCw className={loading ? 'spin' : ''} size={15} />
        </button>
        <label className="secondary-button compact-button project-file-upload">
          <Upload size={14} />
          上传文件
          <input type="file" multiple onChange={onUploadChange} />
        </label>
        <label className="secondary-button compact-button project-file-upload">
          <Folder size={14} />
          上传目录
          <input type="file" multiple {...directoryInputProps} onChange={onUploadChange} />
        </label>
        <button className="secondary-button compact-button" onClick={onCreateFolder} type="button">
          <Plus size={14} />
          新建文件夹
        </button>
      </div>

      <div className="project-file-browser">
        <div className="project-file-tree">
          {error ? <div className="project-file-error">{error}</div> : null}
          {loading ? (
            <div className="project-file-empty">
              <Loader className="spin" size={14} />
              <span>正在加载目录...</span>
            </div>
          ) : files.length ? (
            files.map((file) => (
              <div
                className={file.path === selectedFile?.path ? 'active project-file-row' : 'project-file-row'}
                key={file.path}
              >
                <button
                  className="project-file-main"
                  onClick={() => onFileSelect(file)}
                  title={file.path}
                  type="button"
                >
                  {file.kind === 'directory' ? <Folder size={14} /> : <File size={14} />}
                  <span>{file.name}</span>
                  <em>{projectFileMeta(file)}</em>
                </button>
                <div className="project-file-actions">
                  <button className="icon-button" onClick={() => onRenameFile(file)} title="重命名" type="button">
                    <FileText size={12} />
                  </button>
                  {file.kind === 'file' && file.download_url ? (
                    <button className="icon-button" onClick={() => onDownloadFile(file)} title="下载" type="button">
                      <Download size={12} />
                    </button>
                  ) : null}
                  <button className="icon-button danger-icon-button" onClick={() => onDeleteFile(file)} title="删除" type="button">
                    <Trash2 size={12} />
                  </button>
                </div>
              </div>
            ))
          ) : (
            <div className="project-file-empty">当前目录为空。</div>
          )}
        </div>

        <ProjectFilePreviewPane
          preview={preview}
          previewError={previewError}
          previewLoading={previewLoading}
          selectedFile={selectedFile}
        />
      </div>

      {dialog ? (
        <ProjectFileActionDialog
          dialog={dialog}
          draft={draft}
          onCancel={onDialogCancel}
          onConfirm={onDialogConfirm}
          onDraftChange={onDraftChange}
        />
      ) : null}
    </section>
  );
}

function ProjectFilePreviewPane({
  preview,
  previewError,
  previewLoading,
  selectedFile,
}: {
  preview: ProjectFilePreview | null;
  previewError: string | null;
  previewLoading: boolean;
  selectedFile: ProjectFileItem | null;
}) {
  if (!selectedFile) {
    return (
      <section className="project-file-preview">
        <div className="project-file-preview-empty">选择项目文件后在这里预览。</div>
      </section>
    );
  }
  if (selectedFile.kind === 'directory') {
    return (
      <section className="project-file-preview">
        <div className="project-file-preview-empty">目录已打开。选择一个文件查看预览。</div>
      </section>
    );
  }

  const selectedArtifact = projectFilePreviewArtifactItem(selectedFile);
  const copyProjectPreview = () => {
    if (preview?.content) void navigator.clipboard.writeText(preview.content);
  };

  return (
    <section className="project-file-preview">
      <ArtifactPreview
        error={previewError}
        loading={previewLoading}
        preview={preview}
        selectedArtifact={selectedArtifact}
        onCopy={copyProjectPreview}
      />
    </section>
  );
}

function ProjectFileActionDialog({
  dialog,
  draft,
  onCancel,
  onConfirm,
  onDraftChange,
}: {
  dialog: { kind: ProjectFileDialogKind; file?: ProjectFileItem };
  draft: string;
  onCancel: () => void;
  onConfirm: () => void;
  onDraftChange: (value: string) => void;
}) {
  const title = dialog.kind === 'folder'
    ? '新建文件夹'
    : dialog.kind === 'rename'
      ? '重命名'
      : '删除文件';
  const isDelete = dialog.kind === 'delete';
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={title}>
      <div className="project-dialog compact-project-dialog">
        <header className="project-dialog-head">
          <div>
            <span>项目文件</span>
            <strong>{title}</strong>
          </div>
          <button className="icon-button" onClick={onCancel} title="关闭" type="button">
            <X size={14} />
          </button>
        </header>
        <div className="project-dialog-body">
          {isDelete ? (
            <p>删除 <code>{dialog.file?.path}</code> 后无法从项目目录中恢复。</p>
          ) : (
            <label>
              <span>{dialog.kind === 'folder' ? '文件夹路径' : '新路径'}</span>
              <input value={draft} onChange={(event) => onDraftChange(event.target.value)} autoFocus />
            </label>
          )}
        </div>
        <footer className="project-dialog-actions">
          <button className="secondary-button compact-button" onClick={onCancel} type="button">取消</button>
          <button
            className={isDelete ? 'primary-button danger-button compact-button' : 'primary-button compact-button'}
            disabled={!isDelete && !draft.trim()}
            onClick={onConfirm}
            type="button"
          >
            {isDelete ? '删除' : '确认'}
          </button>
        </footer>
      </div>
    </div>
  );
}

function ProjectCreateDialog({
  draft,
  error,
  onCancel,
  onChange,
  onSubmit,
}: {
  draft: ProjectDraft;
  error: string | null;
  onCancel: () => void;
  onChange: (draft: ProjectDraft) => void;
  onSubmit: () => void;
}) {
  return (
    <ProjectFormDialog
      draft={draft}
      error={error}
      eyebrow="新建项目"
      submitLabel="创建项目"
      title="创建项目"
      onCancel={onCancel}
      onChange={onChange}
      onSubmit={onSubmit}
    />
  );
}

function ProjectEditDialog({
  draft,
  error,
  project,
  onCancel,
  onChange,
  onSubmit,
}: {
  draft: ProjectDraft;
  error: string | null;
  project: ApiProject;
  onCancel: () => void;
  onChange: (draft: ProjectDraft) => void;
  onSubmit: () => void;
}) {
  return (
    <ProjectFormDialog
      draft={draft}
      error={error}
      eyebrow={project.slug}
      submitLabel="保存"
      title="编辑项目"
      onCancel={onCancel}
      onChange={onChange}
      onSubmit={onSubmit}
    />
  );
}

function ProjectDeleteDialog({
  project,
  onCancel,
  onConfirm,
}: {
  project: ApiProject;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="删除项目">
      <div className="project-dialog compact-project-dialog">
        <header className="project-dialog-head">
          <div>
            <span>删除项目</span>
            <strong>{project.name}</strong>
          </div>
          <button className="icon-button" onClick={onCancel} title="关闭" type="button">
            <X size={14} />
          </button>
        </header>
        <div className="project-dialog-body danger-dialog-body">
          <AlertTriangle size={18} />
          <p>项目记录、项目会话以及项目工作目录都会被删除。</p>
          <code>{project.workspace_uri}</code>
        </div>
        <footer className="project-dialog-actions">
          <button className="secondary-button compact-button" onClick={onCancel} type="button">取消</button>
          <button className="primary-button danger-button compact-button" onClick={onConfirm} type="button">删除项目</button>
        </footer>
      </div>
    </div>
  );
}

function ConversationDeleteDialog({
  conversation,
  project,
  onCancel,
  onConfirm,
}: {
  conversation: ApiConversation;
  project: ApiProject | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const deleteMessage = conversation.project_id
    ? '项目会话只会删除会话记录和运行历史，项目目录会保留。'
    : '会话记录和该会话工作目录会同步删除。';
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="删除会话">
      <div className="project-dialog compact-project-dialog">
        <header className="project-dialog-head">
          <div>
            <span>删除会话</span>
            <strong>{conversation.title}</strong>
          </div>
          <button className="icon-button" onClick={onCancel} title="关闭" type="button">
            <X size={14} />
          </button>
        </header>
        <div className="project-dialog-body danger-dialog-body">
          <AlertTriangle size={18} />
          <p>{deleteMessage}</p>
          <code>{project ? `${project.name} / ${conversation.id}` : conversation.id}</code>
        </div>
        <footer className="project-dialog-actions">
          <button className="secondary-button compact-button" onClick={onCancel} type="button">取消</button>
          <button className="primary-button danger-button compact-button" onClick={onConfirm} type="button">删除会话</button>
        </footer>
      </div>
    </div>
  );
}

function ProjectFormDialog({
  draft,
  error,
  eyebrow,
  submitLabel,
  title,
  onCancel,
  onChange,
  onSubmit,
}: {
  draft: ProjectDraft;
  error: string | null;
  eyebrow: string;
  submitLabel: string;
  title: string;
  onCancel: () => void;
  onChange: (draft: ProjectDraft) => void;
  onSubmit: () => void;
}) {
  const updateDraft = (field: keyof ProjectDraft, value: string) => {
    onChange({ ...draft, [field]: value });
  };
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={title}>
      <form
        className="project-dialog"
        onSubmit={(event) => {
          event.preventDefault();
          if (draft.name.trim()) onSubmit();
        }}
      >
        <header className="project-dialog-head">
          <div>
            <span>{eyebrow}</span>
            <strong>{title}</strong>
          </div>
          <button className="icon-button" onClick={onCancel} title="关闭" type="button">
            <X size={14} />
          </button>
        </header>
        <div className="project-dialog-body project-dialog-form">
          <label>
            <span>名称</span>
            <input value={draft.name} onChange={(event) => updateDraft('name', event.target.value)} autoFocus />
          </label>
          <label>
            <span>Slug</span>
            <input value={draft.slug} onChange={(event) => updateDraft('slug', event.target.value)} />
          </label>
          <label>
            <span>描述</span>
            <textarea value={draft.description} onChange={(event) => updateDraft('description', event.target.value)} />
          </label>
          {error ? <div className="project-dialog-error">{error}</div> : null}
        </div>
        <footer className="project-dialog-actions">
          <button className="secondary-button compact-button" onClick={onCancel} type="button">取消</button>
          <button className="primary-button compact-button" disabled={!draft.name.trim()} type="submit">
            <Check size={14} />
            {submitLabel}
          </button>
        </footer>
      </form>
    </div>
  );
}

function DesignEmptyConversation() {
  return (
    <div className="design-empty-conversation">
      <div className="design-empty-icon">
        <Bot size={18} />
      </div>
      <strong>新对话</strong>
      <p>输入任务后，这里会显示来自后端运行流的真实推理、DAG、工具调用和结果。</p>
    </div>
  );
}

function ChatMessageRow({
  loading,
  message,
  onOpenDag,
}: {
  loading: boolean;
  message: ChatMessage;
  onOpenDag: (dag: Dag, trace?: TraceLogEvent[]) => void;
}) {
  if (message.role === 'user') {
    return (
      <div className="chat-row user-row">
        <div className="user-bubble">{message.content}</div>
        <div className="user-avatar">
          <UserCog size={15} />
        </div>
      </div>
    );
  }
  return (
    <div className="chat-row assistant-row">
      <div className="assistant-avatar">
        <Bot size={16} />
      </div>
      <div className="assistant-turn-frame">
        <MessageTimeline
          message={message}
          loading={loading}
          onOpenDag={(snapshot, snapshotTrace) => onOpenDag(snapshot, snapshotTrace)}
        />
      </div>
    </div>
  );
}

// 可复用的面板宽度：localStorage 持久化 + 夹取，供右侧可拖拽面板使用。
function usePanelWidth(storageKey: string, fallback: number, min: number, max: number) {
  const [width, setWidth] = useState<number>(() => {
    const saved = Number(window.localStorage.getItem(storageKey));
    return Number.isFinite(saved) && saved >= min && saved <= max ? saved : fallback;
  });
  const resize = useCallback((next: number) => {
    const clamped = Math.min(max, Math.max(min, next));
    setWidth(clamped);
    window.localStorage.setItem(storageKey, String(Math.round(clamped)));
  }, [storageKey, min, max]);
  return [width, resize] as const;
}

function UploadPicker({
  disabled = false,
  onUploadFiles,
  variant,
}: {
  disabled?: boolean;
  onUploadFiles: (files: FileList | null) => void;
  variant: 'composer' | 'sidebar';
}) {
  const detailsRef = useRef<HTMLDetailsElement | null>(null);
  const directoryInputProps = {
    directory: '',
    webkitdirectory: '',
  } as React.InputHTMLAttributes<HTMLInputElement> & { directory: string; webkitdirectory: string };
  const iconSize = variant === 'composer' ? 17 : 13;
  const summaryClass = variant === 'composer'
    ? 'icon-button attachment-button'
    : 'sidebar-artifact-icon';
  const onChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    onUploadFiles(event.target.files);
    event.currentTarget.value = '';
    event.currentTarget.closest('details')?.removeAttribute('open');
  };
  useEffect(() => {
    if (disabled) {
      detailsRef.current?.removeAttribute('open');
    }
  }, [disabled]);
  const onSummaryClick = (event: React.MouseEvent<HTMLElement>) => {
    if (disabled) {
      event.preventDefault();
    }
  };

  return (
    <details
      ref={detailsRef}
      className={`upload-picker ${variant === 'composer' ? 'composer-upload-picker' : 'sidebar-upload-picker'}`}
    >
      <summary
        className={summaryClass}
        title="上传附件"
        aria-disabled={disabled}
        aria-label="上传附件"
        onClick={onSummaryClick}
      >
        <Upload size={iconSize} />
      </summary>
      <div className="upload-picker-menu">
        <label>
          <File size={13} />
          <span>上传文件</span>
          <input disabled={disabled} type="file" multiple onChange={onChange} />
        </label>
        <label>
          <Folder size={13} />
          <span>上传文件夹</span>
          <input disabled={disabled} type="file" multiple {...directoryInputProps} onChange={onChange} />
        </label>
      </div>
    </details>
  );
}

// 右侧面板的拖拽手柄：放在面板左缘，向左拖变宽、向右拖变窄。
function PanelResizeHandle({ width, onResize }: { width: number; onResize: (next: number) => void }) {
  const [dragging, setDragging] = useState(false);
  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = width;
    setDragging(true);
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';
    const onMove = (moveEvent: PointerEvent) => onResize(startWidth + (startX - moveEvent.clientX));
    const onUp = () => {
      setDragging(false);
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  };
  return (
    <div
      className="panel-resize-handle"
      data-dragging={dragging}
      onPointerDown={onPointerDown}
      role="separator"
      aria-orientation="vertical"
      title="拖动调整宽度"
    />
  );
}

function ArtifactPanel({
  error,
  loading,
  preview,
  previewError,
  previewLoading,
  artifacts,
  open,
  selectedArtifact,
  selectedArtifactId,
  onCopy,
  onRefresh,
  onSelect,
  onToggle,
}: {
  error: string | null;
  loading: boolean;
  preview: RunArtifactPreview | null;
  previewError: string | null;
  previewLoading: boolean;
  artifacts: WorkbenchArtifactItem[];
  open: boolean;
  selectedArtifact: WorkbenchArtifactItem | null;
  selectedArtifactId: string;
  onCopy: () => void;
  onRefresh: () => void;
  onSelect: (id: string) => void;
  onToggle: () => void;
}) {
  const [artifactWidth, setArtifactWidth] = usePanelWidth('dagent.artifact-width', 540, 360, 980);
  const [artifactFilesExpanded, setArtifactFilesExpanded] = useState(true);
  const [expandedArtifactFolders, setExpandedArtifactFolders] = useState<Set<string>>(() => new Set());
  const artifactTree = useMemo(() => buildWorkbenchArtifactTree(artifacts), [artifacts]);

  useEffect(() => {
    setExpandedArtifactFolders((current) => {
      const next = new Set(current);
      artifactTree.forEach((node) => {
        if (node.kind === 'folder') next.add(node.id);
      });
      artifactFolderIdsForPath(selectedArtifact?.path).forEach((id) => next.add(id));
      return sameStringSet(current, next) ? current : next;
    });
  }, [artifactTree, selectedArtifact?.path]);

  const toggleArtifactFolder = (id: string) => {
    setExpandedArtifactFolders((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (!open) {
    return (
      <aside className="artifact-rail">
        <button
          className="icon-button"
          onClick={onToggle}
          title="展开产物"
          type="button"
        >
          <ChevronLeft size={16} />
        </button>
        <div>
          <span>产物</span>
          <em>{artifacts.length}</em>
        </div>
      </aside>
    );
  }

  return (
    <aside className="artifact-drawer" style={{ width: artifactWidth }}>
      <PanelResizeHandle width={artifactWidth} onResize={setArtifactWidth} />
      <div className="artifact-drawer-head">
        <div className="artifact-drawer-title">
          <Folder className="artifact-drawer-title-folder" size={17} />
          <strong>产物</strong>
          <span>{artifacts.length}</span>
        </div>
        <div className="artifact-drawer-actions">
          <button className="icon-button" disabled={loading} onClick={onRefresh} title="刷新" type="button">
            <RefreshCw className={loading ? 'spin' : ''} size={15} />
          </button>
          <button className="icon-button" onClick={onToggle} title="收起面板" type="button">
            <ChevronRight size={16} />
          </button>
        </div>
      </div>

      <div className="artifact-drawer-body" data-tree-expanded={artifactFilesExpanded}>
        <div className="artifact-tree-pane" data-expanded={artifactFilesExpanded}>
          {artifactFilesExpanded ? (
            <>
              <div className="artifact-tree-list">
                {error ? <div className="artifact-empty">{error}</div> : null}
                {artifactTree.length ? (
                  <ArtifactTree
                    depth={0}
                    expandedFolders={expandedArtifactFolders}
                    nodes={artifactTree}
                    onSelect={onSelect}
                    onToggleFolder={toggleArtifactFolder}
                    selectedArtifactId={selectedArtifactId}
                  />
                ) : (
                  <div className="artifact-empty">当前运行还没有产物。</div>
                )}
              </div>
            </>
          ) : (
            <button
              className="artifact-tree-rail-toggle"
              onClick={() => setArtifactFilesExpanded(true)}
              title="展开目录树"
              type="button"
            >
              <Folder size={14} />
              <ChevronRight size={12} />
            </button>
          )}
        </div>

        {artifactFilesExpanded ? (
          <button
            className="artifact-tree-divider-toggle"
            onClick={() => setArtifactFilesExpanded(false)}
            title="收起目录树"
            type="button"
          >
            <ChevronLeft size={14} />
          </button>
        ) : null}

        <ArtifactPreview
          error={previewError}
          loading={previewLoading}
          preview={preview}
          selectedArtifact={selectedArtifact}
          onCopy={onCopy}
        />
      </div>
    </aside>
  );
}

function ArtifactTree({
  depth,
  expandedFolders,
  nodes,
  onSelect,
  onToggleFolder,
  selectedArtifactId,
}: {
  depth: number;
  expandedFolders: Set<string>;
  nodes: WorkbenchArtifactTreeNode[];
  onSelect: (id: string) => void;
  onToggleFolder: (id: string) => void;
  selectedArtifactId: string;
}) {
  return (
    <div className="artifact-tree-level">
      {nodes.map((node) => {
        const treeDepthStyle = { '--artifact-tree-depth': depth } as React.CSSProperties;
        if (node.kind === 'folder') {
          const open = expandedFolders.has(node.id);
          return (
            <div className="artifact-tree-folder-group" key={node.id}>
              <button
                aria-expanded={open}
                className="artifact-tree-folder"
                data-open={open}
                onClick={() => onToggleFolder(node.id)}
                style={treeDepthStyle}
                title={node.path}
                type="button"
              >
                <ChevronRight size={12} />
                <Folder size={13} />
                <span>{node.name}</span>
                <em>{node.fileCount}</em>
              </button>
              {open ? (
                <ArtifactTree
                  depth={depth + 1}
                  expandedFolders={expandedFolders}
                  nodes={node.children}
                  onSelect={onSelect}
                  onToggleFolder={onToggleFolder}
                  selectedArtifactId={selectedArtifactId}
                />
              ) : null}
            </div>
          );
        }
        return (
          <button
            className={node.item.id === selectedArtifactId ? 'active artifact-tree-file' : 'artifact-tree-file'}
            key={node.id}
            onClick={() => onSelect(node.item.id)}
            style={treeDepthStyle}
            title={node.path}
            type="button"
          >
            <span className="artifact-tree-file-name">{node.name}</span>
          </button>
        );
      })}
    </div>
  );
}

function ArtifactPreview({
  error,
  loading,
  preview,
  selectedArtifact,
  onCopy,
}: {
  error: string | null;
  loading: boolean;
  preview: TextFilePreview | null;
  selectedArtifact: WorkbenchArtifactItem | null;
  onCopy: () => void;
}) {
  const [previewFullscreen, setPreviewFullscreen] = useState(false);

  useEffect(() => {
    setPreviewFullscreen(false);
  }, [selectedArtifact?.id]);

  useEffect(() => {
    if (!previewFullscreen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPreviewFullscreen(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [previewFullscreen]);

  if (!selectedArtifact) {
    return (
      <div className="artifact-preview">
        <div className="artifact-preview-empty">选择一次运行产物后在这里预览。</div>
      </div>
    );
  }

  const canCopy = Boolean(preview?.content);
  const downloadUrl = artifactPreviewDownloadUrl(selectedArtifact);
  const canFullscreen = selectedArtifact.previewable !== false && !selectedArtifact.error;
  const fileName = artifactListFileName(selectedArtifact);

  return (
    <div
      className="artifact-preview"
      data-fullscreen={previewFullscreen}
      role={previewFullscreen ? 'dialog' : undefined}
      aria-modal={previewFullscreen ? true : undefined}
      aria-label={previewFullscreen ? `${fileName} 预览` : undefined}
    >
      <div className="artifact-preview-fullscreen-shell">
        <div className="artifact-preview-head">
          <File size={14} />
          <strong className="artifact-preview-title">{fileName}</strong>
          <button className="icon-button" disabled={!canCopy} onClick={onCopy} title="复制" type="button">
            <Copy size={13} />
          </button>
          <a
            className="icon-button"
            href={downloadUrl ?? undefined}
            download={selectedArtifact.name}
            aria-disabled={!downloadUrl}
            title="下载"
          >
            <Download size={13} />
          </a>
          <button
            className="icon-button"
            disabled={!canFullscreen}
            onClick={() => setPreviewFullscreen((value) => !value)}
            title={previewFullscreen ? '关闭全屏' : '全屏预览'}
            type="button"
          >
            {previewFullscreen ? <X size={14} /> : <Maximize2 size={13} />}
          </button>
        </div>
        <ArtifactPreviewBody
          error={error}
          loading={loading}
          preview={preview}
          selectedArtifact={selectedArtifact}
        />
      </div>
    </div>
  );
}

function ArtifactPreviewBody({
  error,
  loading,
  preview,
  selectedArtifact,
}: {
  error: string | null;
  loading: boolean;
  preview: TextFilePreview | null;
  selectedArtifact: WorkbenchArtifactItem;
}) {
  const mode = artifactPreviewMode(selectedArtifact.previewKind);
  if (selectedArtifact.error) {
    return <div className="artifact-preview-empty">{selectedArtifact.error}</div>;
  }
  if (selectedArtifact.previewable === false) {
    return <div className="artifact-preview-empty">此文件暂不支持预览。</div>;
  }
  if (mode === 'browser') {
    return <ArtifactBrowserPreview selectedArtifact={selectedArtifact} />;
  }
  if (mode === 'unsupported') {
    return <div className="artifact-preview-empty">此文件暂不支持预览。</div>;
  }
  if (loading) {
    return (
      <div className="artifact-preview-empty">
        <Loader className="spin" size={14} />
        <span>正在加载预览...</span>
      </div>
    );
  }
  if (error) {
    return <div className="artifact-preview-empty">{error}</div>;
  }
  if (preview && selectedArtifact.previewKind === 'markdown') {
    return (
      <div className="artifact-markdown markdown-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{preview.content}</ReactMarkdown>
        {preview.truncated ? <div className="artifact-preview-note">内容已截断到 {preview.truncated_at} 字节。</div> : null}
      </div>
    );
  }
  if (preview) {
    return (
      <>
        <pre>{preview.content}</pre>
        {preview.truncated ? <div className="artifact-preview-note">内容已截断到 {preview.truncated_at} 字节。</div> : null}
      </>
    );
  }
  return <div className="artifact-preview-empty">选择文件后加载预览。</div>;
}

function ArtifactBrowserPreview({ selectedArtifact }: { selectedArtifact: WorkbenchArtifactItem }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const downloadUrl = artifactPreviewDownloadUrl(selectedArtifact);
  const onlyOfficeConfigUrl = selectedArtifact.onlyOfficeConfigUrl ?? null;

  useEffect(() => {
    const container = containerRef.current;
    const previewKind = selectedArtifact.previewKind;
    if (!container) return;
    if ((!downloadUrl && !onlyOfficeConfigUrl) || !isBrowserArtifactPreviewKind(previewKind)) {
      setLoading(false);
      setError('此文件缺少可用的预览下载地址。');
      return;
    }

    let cancelled = false;
    let handle: ArtifactPreviewRenderHandle | null = null;
    const controller = new AbortController();
    container.replaceChildren();
    setLoading(true);
    setError(null);

    const renderBuiltInBrowserArtifactPreview = async () => {
      const response = await fetch(downloadUrl!, { signal: controller.signal });
      if (!response.ok) throw new Error(await artifactResponseError(response));
      const blob = await response.blob();
      handle = await renderBrowserArtifactPreview(container, {
        kind: previewKind,
        source: blob,
        fileName: selectedArtifact.name,
        signal: controller.signal,
      });
    };

    const render = async () => {
      if (onlyOfficeConfigUrl) {
        try {
          handle = await renderBrowserArtifactPreview(container, {
            kind: previewKind,
            onlyOfficeConfigUrl,
            fileName: selectedArtifact.name,
            signal: controller.signal,
          });
          return;
        } catch (exc) {
          if (isAbortError(exc) || !downloadUrl) throw exc;
          container.replaceChildren();
        }
      }
      await renderBuiltInBrowserArtifactPreview();
    };

    void render()
      .then(() => {
        if (cancelled) {
          handle?.destroy();
          return;
        }
        setLoading(false);
      })
      .catch((exc) => {
        if (cancelled || isAbortError(exc)) return;
        setError(exc instanceof Error ? exc.message : String(exc));
        setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
      handle?.destroy();
      container.replaceChildren();
    };
  }, [downloadUrl, onlyOfficeConfigUrl, selectedArtifact.name, selectedArtifact.previewKind]);

  return (
    <div className="artifact-browser-preview-shell">
      {loading ? (
        <div className="artifact-preview-empty">
          <Loader className="spin" size={14} />
          <span>正在加载预览...</span>
        </div>
      ) : null}
      {error ? <div className="artifact-preview-empty">{error}</div> : null}
      <div className="artifact-browser-preview-host" ref={containerRef} />
    </div>
  );
}

function sameStringSet(left: Set<string>, right: Set<string>): boolean {
  if (left.size !== right.size) return false;
  for (const value of left) {
    if (!right.has(value)) return false;
  }
  return true;
}

function artifactListFileName(artifact: WorkbenchArtifactItem): string {
  const value = artifact.path || artifact.name;
  return value.replace(/\\/g, '/').split('/').filter(Boolean).pop() || artifact.name;
}

async function artifactResponseError(response: Response): Promise<string> {
  try {
    const payload = await response.clone().json();
    if (typeof payload.detail === 'string') return payload.detail;
    if (payload.detail) return JSON.stringify(payload.detail);
    return response.statusText;
  } catch {
    // Fall through to plain-text response bodies.
  }
  const text = await response.text().catch(() => '');
  return text || `加载预览失败（HTTP ${response.status}）。`;
}

function PaneTitle({ icon, title }: { icon: React.ReactNode; title: string }) {
  return (
    <div className="pane-title">
      {icon}
      <span>{title}</span>
    </div>
  );
}

function MessageTimeline({
  message,
  loading,
  onOpenDag,
}: {
  message: ChatMessage;
  loading: boolean;
  onOpenDag: (dag: Dag, trace?: TraceLogEvent[]) => void;
}) {
  if (!message.timeline?.length) {
    return <MessageContent content={message.content || (loading ? '...' : '')} />;
  }

  return (
    <div className="message-timeline">
      {message.timeline.map((item, index) =>
        item.type === 'capability' ? (
          <CapabilityEventCard key={`${item.event.invocation_id}-${index}`} event={item.event} result={item.result} />
        ) : item.type === 'dag' ? (
          <DagSummaryCard
            key={`${item.dag.task_id || item.dag.dag_id}-${index}`}
            dag={item.dag}
            onOpen={() => onOpenDag(item.dag, message.traceSnapshot)}
          />
        ) : item.type === 'reasoning' ? (
          <ReasoningBlock key={`reasoning-${index}`} content={item.content} closed={item.closed} />
        ) : item.type === 'validation' ? (
          <ValidationCard key={`validation-${index}`} event={item.event} />
        ) : item.type === 'validating' ? (
          <ValidationCard key={`validating-${index}`} />
        ) : item.content ? (
          <MessageContent key={`text-${index}`} content={item.content} />
        ) : null,
      )}
      {!timelineHasVisibleContent(message.timeline) && !message.content && loading ? <MessageContent content="..." /> : null}
    </div>
  );
}

function timelineHasVisibleContent(timeline: MessageTimelineItem[] | undefined): boolean {
  return Boolean(timeline?.some((item) => {
    if (item.type === 'text' || item.type === 'reasoning') return Boolean(item.content);
    return true;
  }));
}

const DSL_NODE_RE = /^[A-Za-z][A-Za-z0-9_-]*\s*=\s*[A-Za-z_][A-Za-z0-9_]*\(.*\)/;

function looksLikeDsl(text: string): boolean {
  const lines = text.trim().split('\n').filter((l) => l.trim());
  if (lines.length === 0) return false;
  const dslLines = lines.filter(
    (l) => DSL_NODE_RE.test(l.trim()) || /^task:/i.test(l.trim()),
  );
  return dslLines.length >= lines.length * 0.5;
}

function MessageContent({ content }: { content: string }) {
  const parts = useMemo(() => splitThinking(content), [content]);
  return (
    <div className="markdown-body">
      {parts.map((part, index) =>
        part.type === 'think' ? (
          <ReasoningBlock key={`${part.type}-${index}`} content={part.content} closed={Boolean(part.closed)} />
        ) : looksLikeDsl(part.content) ? null : (
          <ReactMarkdown key={`${part.type}-${index}`} remarkPlugins={[remarkGfm]}>{part.content}</ReactMarkdown>
        ),
      )}
    </div>
  );
}

function ReasoningBlock({ content, closed }: { content: string; closed: boolean }) {
  return (
    <details className="think-block" open={!closed}>
      <summary className="reasoning-summary">
        <Bot size={14} />
        <strong>推理过程</strong>
        <ChevronRight className="timeline-chevron" size={15} />
      </summary>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content || '...'}</ReactMarkdown>
    </details>
  );
}

function splitThinking(content: string): Array<{ type: 'answer' | 'think'; content: string; closed?: boolean }> {
  const parts: Array<{ type: 'answer' | 'think'; content: string; closed?: boolean }> = [];
  let cursor = 0;
  while (cursor < content.length) {
    const openIndex = content.indexOf('<think>', cursor);
    if (openIndex === -1) {
      const answer = content.slice(cursor);
      if (answer) parts.push({ type: 'answer', content: answer });
      break;
    }
    const answer = content.slice(cursor, openIndex);
    if (answer) parts.push({ type: 'answer', content: answer });
    const thinkStart = openIndex + '<think>'.length;
    const closeIndex = content.indexOf('</think>', thinkStart);
    if (closeIndex === -1) {
      parts.push({ type: 'think', content: content.slice(thinkStart), closed: false });
      break;
    }
    parts.push({ type: 'think', content: content.slice(thinkStart, closeIndex), closed: true });
    cursor = closeIndex + '</think>'.length;
  }
  return parts.length ? parts : [{ type: 'answer', content }];
}

function dagRunStatus(status: string): 'planned' | 'running' | 'completed' | 'failed' {
  if (status === 'failed') return 'failed';
  if (status === 'running') return 'running';
  if (status === 'planned') return 'planned';
  return 'completed';
}

const dagStatusLabels: Record<Dag['status'], string> = {
  draft: '草稿',
  review_required: '待审核',
  approved: '已通过',
  running: '运行中',
  awaiting_review: '待审核',
  completed: '已完成',
  failed: '失败',
  rejected: '已拒绝',
  aborted: '已终止',
};

function StatusBadge({ status }: { status: Dag['status'] }) {
  return <span className="status-badge" data-status={status}>{dagStatusLabels[status] ?? status}</span>;
}

function DagSummaryCard({
  dag,
  onOpen,
}: {
  dag: Dag;
  onOpen: () => void;
}) {
  const riskyNodes = dag.nodes.filter((node) => nodeReviewInfo(node).reviewAttention).length;
  const actionLabel = isDagConfirmable(dag) ? '打开审查' : '查看流程';
  return (
    <button className="dag-summary-card" onClick={onOpen} type="button">
      <div className="dag-summary-head">
        <GitBranch size={17} />
        <strong>{dag.task_id || dag.dag_id}</strong>
        <StatusBadge status={dag.status} />
      </div>
      <div className="dag-summary-stats">
        <span><strong>{dag.nodes.length}</strong> nodes</span>
        <span><strong>{dag.edges.length}</strong> edges</span>
        <span><strong>{riskyNodes}</strong> review</span>
        <em>{actionLabel} <ChevronRight size={13} /></em>
      </div>
    </button>
  );
}

function ValidationCard({ event }: { event?: ValidationFeedbackEvent }) {
  const validating = !event;
  const passed = event ? event.type === 'validation.passed' || event.passed === true : false;
  const statusLabel = validating ? '校验中' : passed ? '通过' : '需要重试';

  return (
    <details className={`timeline-card validation-card ${validating ? 'validation-running' : passed ? 'validation-passed' : 'validation-feedback'}`}>
      <summary className="timeline-card-head">
        {validating ? <Loader size={14} /> : passed ? <Check size={14} /> : <AlertTriangle size={14} />}
        <strong>结果校验</strong>
        <span>{statusLabel}</span>
        <ChevronRight className="timeline-chevron" size={15} />
      </summary>
      {validating ? (
        <div className="timeline-section">
          <div className="timeline-section-label">状态</div>
          <p>正在校验最终回复...</p>
        </div>
      ) : event?.summary ? (
        <div className="timeline-section">
          <div className="timeline-section-label">摘要</div>
          <p>{event.summary}</p>
        </div>
      ) : null}
      {!validating && event && !passed && event.issues.length ? (
        <div className="timeline-section">
          <div className="timeline-section-label">问题</div>
          <ul className="validation-issues">
            {event.issues.map((issue, index) => (
              <li key={index}>
                {issue.node_id ? <em>[{issue.node_id}]</em> : null}
                <span>{issue.message}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {!validating && event && !passed && event.reason ? (
        <div className="timeline-section">
          <div className="timeline-section-label">反馈</div>
          <p>{event.reason}</p>
        </div>
      ) : null}
    </details>
  );
}

function hasNonZeroExitCode(content?: string): boolean {
  if (!content) return false;
  const match = content.match(/exit_code=(\d+)/);
  return match !== null && match[1] !== '0';
}

function CapabilityEventCard({ event, result }: { event: CapabilityStreamEvent; result?: CapabilityStreamEvent }) {
  const resultContent = result?.content || (event.type !== 'capability.call.started' ? event.content || '' : '');
  const isError = result?.type === 'capability.call.failed' || event.type === 'capability.call.failed';
  const rejectedByReview = Boolean(result?.content?.startsWith('人工审核已拒绝'));
  const isExitError = !isError && hasNonZeroExitCode(resultContent);
  const showError = isError || isExitError;
  const statusLabel = result
    ? (rejectedByReview ? '已拒绝' : isError ? 'failed' : isExitError ? 'error' : 'done')
    : (event.type === 'capability.call.started' ? 'running' : event.type === 'capability.call.failed' ? 'failed' : 'done');
  const argsText = formatCapabilityArguments(event.arguments);
  const eventClass = rejectedByReview ? 'capability-event-rejected' : showError ? 'capability-event-error' : `capability-event-${statusLabel}`;
  return (
    <details className={`capability-event-card ${eventClass}`}>
      <summary className="capability-event-head">
        <Wrench size={14} />
        <strong>{event.capability_id}</strong>
        <span>{statusLabel}</span>
        <ChevronRight className="timeline-chevron" size={15} />
      </summary>
      {argsText ? (
        <div className="capability-section">
          <div className="capability-section-label">Args</div>
          <CapabilityCodeBlock value={argsText} />
        </div>
      ) : null}
      {resultContent ? (
        <div className="capability-section">
          <div className="capability-section-label">{showError ? 'Error' : 'Result'}</div>
          <CapabilityCodeBlock value={resultContent} />
        </div>
      ) : null}
    </details>
  );
}

function CapabilityCodeBlock({ value }: { value: string }) {
  return (
    <textarea
      className="capability-code-block"
      readOnly
      rows={capabilityCodeRows(value)}
      spellCheck={false}
      value={value}
    />
  );
}

function capabilityCodeRows(value: string): number {
  const lines = Math.max(1, value.split('\n').length);
  return Math.max(6, Math.min(16, lines));
}

function findMatchingCapabilityCall(timeline: MessageTimelineItem[], invocationId: string): number {
  for (let i = timeline.length - 1; i >= 0; i--) {
    const item = timeline[i];
    if (item.type === 'capability' && item.event.invocation_id === invocationId && item.event.type === 'capability.call.started') {
      return i;
    }
  }
  return -1;
}

function formatCapabilityArguments(value?: Record<string, unknown>) {
  if (!value) return '';
  if (!Object.keys(value).length) return '';
  return JSON.stringify(value, null, 2);
}

function clipText(value: string, maxLength: number) {
  return value.length > maxLength ? `${value.slice(0, maxLength - 3)}...` : value;
}

function currentChatTitle(messages: ChatMessage[]): string {
  const latestUser = [...messages].reverse().find((message) => message.role === 'user' && message.content.trim());
  return latestUser ? clipText(latestUser.content.trim(), 40) : '新对话';
}

function conversationTitleFromPrompt(prompt: string): string {
  return clipText(prompt.trim(), 40) || '新对话';
}

function uniqueNodeId(dag: Dag) {
  let index = dag.nodes.length + 1;
  let id = `node_${index}`;
  const existing = new Set(dag.nodes.map((node) => node.id));
  while (existing.has(id)) {
    index += 1;
    id = `node_${index}`;
  }
  return id;
}

function dagNameInputCh(value: string) {
  return Math.max(12, Math.min(32, value.length + 2));
}

function uniqueDraftArtifactId(artifacts: Record<string, Artifact>, prefix: string) {
  let index = Object.keys(artifacts).length + 1;
  let id = `${prefix}_${index}`;
  while (Object.prototype.hasOwnProperty.call(artifacts, id)) {
    index += 1;
    id = `${prefix}_${index}`;
  }
  return id;
}

function filesFromList(fileList: FileList | null): File[] {
  return Array.from(fileList ?? []);
}

function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function projectFileMeta(file: ProjectFileItem): string {
  if (file.kind === 'directory') return '目录';
  if (typeof file.size === 'number') return formatFileSize(file.size);
  return file.media_type ?? '文件';
}

function projectFilePreviewArtifactItem(file: ProjectFileItem): WorkbenchArtifactItem {
  return {
    id: `project:${file.path}`,
    name: file.name,
    extension: projectFileExtension(file.name),
    meta: projectFileMeta(file),
    source: 'run',
    path: file.path,
    previewKind: file.preview_kind ?? undefined,
    previewable: file.previewable,
    previewUrl: file.preview_url ?? null,
    downloadUrl: file.download_url ?? null,
    onlyOfficeConfigUrl: file.onlyoffice_config_url ?? null,
    size: file.size ?? null,
  };
}

function projectFileExtension(name: string): string {
  const suffix = name.split('.').filter(Boolean).pop();
  if (!suffix || suffix === name) return 'FILE';
  return suffix.slice(0, 5).toUpperCase();
}

function artifactDisplayPath(artifact: Artifact) {
  return artifact.paths?.[0] || artifact.id;
}

function artifactDisplayName(artifact: Artifact) {
  const displayName = artifact.metadata?.display_name;
  if (typeof displayName === 'string' && displayName.trim()) return displayName;
  return artifact.description || artifact.id;
}

function artifactKindLabel(artifact: Artifact) {
  const kind = artifact.metadata?.kind;
  if (typeof kind === 'string' && kind.trim()) return kind;
  return isUploadedFileArtifact(artifact) ? 'file' : 'path';
}

function compareArtifactsByPath(left: Artifact, right: Artifact) {
  return artifactDisplayPath(left).localeCompare(artifactDisplayPath(right));
}

function validateArtifactDraft(
  artifact: Artifact,
  artifacts: Record<string, Artifact>,
  previousId: string,
): string | null {
  const id = artifact.id.trim();
  if (!id) return 'Artifact id is required.';
  if (!/^[A-Za-z][A-Za-z0-9_-]*$/.test(id)) return 'Artifact id must start with a letter and use letters, numbers, _ or -.';
  if (id !== previousId && Object.prototype.hasOwnProperty.call(artifacts, id)) return `Artifact '${id}' already exists.`;
  if (!artifact.paths.length) return 'Artifact path is required.';
  const invalidPath = artifact.paths.find((path) => !isSafeArtifactPath(path));
  if (invalidPath) return `Artifact path '${invalidPath}' must be a relative path without .. segments.`;
  return null;
}

function isSafeArtifactPath(path: string): boolean {
  const clean = path.trim().replace(/\\/g, '/');
  if (!clean || clean.startsWith('/')) return false;
  return clean.split('/').every((part) => part && part !== '.' && part !== '..');
}

function ArtifactEditDialog({
  artifact,
  artifacts,
  onClose,
  onSave,
}: {
  artifact: Artifact;
  artifacts: Record<string, Artifact>;
  onClose: () => void;
  onSave: (previousId: string, artifact: Artifact) => boolean;
}) {
  const [artifactId, setArtifactId] = useState(artifact.id);
  const [pathsText, setPathsText] = useState((artifact.paths ?? []).join('\n'));
  const [description, setDescription] = useState(artifact.description ?? '');
  const [required, setRequired] = useState(artifact.required ?? true);

  useEffect(() => {
    setArtifactId(artifact.id);
    setPathsText((artifact.paths ?? []).join('\n'));
    setDescription(artifact.description ?? '');
    setRequired(artifact.required ?? true);
  }, [artifact]);

  const draft: Artifact = {
    id: artifactId.trim(),
    paths: pathsText.split('\n').map((path) => path.trim()).filter(Boolean),
    description,
    required,
    metadata: artifact.metadata ?? {},
  };
  const uploadedFile = isUploadedFileArtifact(artifact);
  const validation = validateArtifactDraft(draft, artifacts, artifact.id);

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="编辑 artifact">
      <form
        className="artifact-edit-dialog"
        onSubmit={(event) => {
          event.preventDefault();
          if (!validation) onSave(artifact.id, draft);
        }}
      >
        <header className="artifact-edit-head">
          <div>
            <span>Artifact</span>
            <strong>{artifactDisplayName(artifact)}</strong>
          </div>
          <button className="icon-button" onClick={onClose} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        <div className="artifact-edit-body">
          <label>
            ID
            <input
              value={artifactId}
              disabled={uploadedFile}
              onChange={(event) => setArtifactId(event.target.value)}
              spellCheck={false}
              title={uploadedFile ? '上传文件的 id 关联当前会话内的上传内容' : undefined}
            />
          </label>
          <label>
            Path
            <textarea
              value={pathsText}
              onChange={(event) => setPathsText(event.target.value)}
              spellCheck={false}
              rows={3}
            />
          </label>
          <label>
            Description
            <input value={description} onChange={(event) => setDescription(event.target.value)} spellCheck={false} />
          </label>
          <label className="checkbox-line artifact-edit-required">
            <input
              type="checkbox"
              checked={required}
              onChange={(event) => setRequired(event.target.checked)}
            />
            Required
          </label>
          {validation ? <p className="artifact-edit-error">{validation}</p> : null}
        </div>
        <footer className="artifact-edit-actions">
          <button className="secondary-button" onClick={onClose} type="button">取消</button>
          <button className="primary-button" disabled={Boolean(validation)} type="submit">
            <Save size={16} />
            保存
          </button>
        </footer>
      </form>
    </div>
  );
}

function CapabilityReviewDialog({
  review,
  feedback,
  onFeedbackChange,
  onApprove,
  onReject,
  onClose,
}: {
  review: ReviewEventPayload;
  feedback: string;
  onFeedbackChange: (value: string) => void;
  onApprove: () => void;
  onReject: () => void;
  onClose: () => void;
}) {
  const capabilityCall = review.capability_call;
  const argsText = capabilityCall ? JSON.stringify(capabilityCall.arguments, null, 2) : '';
  const payload = review.payload ?? {};
  const reason = payloadString(payload.reason);
  const error = payloadString(payload.error);
  const risk = riskFromPayload(payload.risk);
  const isBoundaryOverride = reason === 'boundary_violation';
  const title = isBoundaryOverride ? 'Boundary Override' : 'Capability Review';
  const detail = isBoundaryOverride
    ? 'This tool call needs approval to cross its configured boundary.'
    : review.message;
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Capability review">
      <div className="dag-modal capability-review-modal">
        <header className="modal-header">
          <div>
            <div className="modal-title">
              <AlertTriangle size={20} />
              <span>{title}</span>
              <span className={`risk-badge risk-${risk}`}>{risk.toUpperCase()}</span>
            </div>
            <p>{detail}</p>
          </div>
          <div className="modal-actions">
            <button className="secondary-button compact-button" onClick={onReject} type="button">
              <X size={16} />
              Reject
            </button>
            <button className="primary-button" onClick={onApprove} type="button">
              <Check size={17} />
              Approve
            </button>
            <button className="icon-button" onClick={onClose} title="Close" type="button">
              <X size={18} />
            </button>
          </div>
        </header>
        <div className="modal-body capability-review-body">
          {isBoundaryOverride && error ? (
            <div className="capability-review-warning">
              <strong>Boundary violation</strong>
              <span>{error}</span>
            </div>
          ) : null}
          {capabilityCall ? (
            <div className="capability-section">
              <div className="capability-section-label">Capability</div>
              <p><strong>{capabilityCall.capability_id}</strong></p>
            </div>
          ) : null}
          {reason ? (
            <div className="capability-section">
              <div className="capability-section-label">Reason</div>
              <p>{reason.replace(/_/g, ' ')}</p>
            </div>
          ) : null}
          {argsText ? (
            <div className="capability-section">
              <div className="capability-section-label">Arguments</div>
              <pre>{clipText(argsText, 1200)}</pre>
            </div>
          ) : null}
          <label className="review-feedback-field">
            <span>Reviewer feedback</span>
            <textarea
              value={feedback}
              onChange={(event) => onFeedbackChange(event.target.value)}
              placeholder="Reason or next instruction"
            />
          </label>
        </div>
      </div>
    </div>
  );
}

function payloadString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function riskFromPayload(value: unknown): RiskLevel {
  return riskLevels.includes(value as RiskLevel) ? value as RiskLevel : 'low';
}

function ChatCapabilityScopeDialog({
  agentPresets,
  agentScope,
  capabilities,
  skills,
  mcpServers,
  mode,
  selectedAgentIds,
  selectedCapabilityIds,
  selectedSkillNames,
  onAgentIdsChange,
  onAgentScopeChange,
  onModeChange,
  onCapabilityIdsChange,
  onSkillNamesChange,
  onClose,
}: {
  agentPresets: AgentPreset[];
  agentScope: AgentScopeMode;
  capabilities: CapabilityDefinition[];
  skills: SkillSummary[];
  mcpServers: MCPServer[];
  mode: ChatScopeMode;
  selectedAgentIds: string[];
  selectedCapabilityIds: string[];
  selectedSkillNames: string[];
  onAgentIdsChange: React.Dispatch<React.SetStateAction<string[]>>;
  onAgentScopeChange: React.Dispatch<React.SetStateAction<AgentScopeMode>>;
  onModeChange: React.Dispatch<React.SetStateAction<ChatScopeMode>>;
  onCapabilityIdsChange: React.Dispatch<React.SetStateAction<string[]>>;
  onSkillNamesChange: React.Dispatch<React.SetStateAction<string[]>>;
  onClose: () => void;
}) {
  const [query, setQuery] = useState('');
  const selectedCapabilities = new Set(selectedCapabilityIds);
  const selectedSkills = new Set(selectedSkillNames);
  const selectedAgents = new Set(selectedAgentIds);
  const normalizedQuery = normalizeSearchQuery(query);
  const enabledCapabilities = capabilities.filter((capability) => capability.enabled && capability.kind !== 'agent');
  const visibleCapabilities = enabledCapabilities.filter((capability) => matchesCapabilityQuery(capability, normalizedQuery));
  const visibleSkills = skills.filter((skill) => matchesSkillQuery(skill, normalizedQuery));
  const visibleAgents = agentPresets.filter((agent) => matchesAgentPresetQuery(agent, normalizedQuery));
  const groups = capabilityKinds
    .map((kind) => ({ kind, items: visibleCapabilities.filter((capability) => capability.kind === kind) }))
    .filter((group) => group.items.length);
  const mcpServerCounts = mcpServers
    .map((server) => ({
      name: server.name,
      ids: server.tools.filter((tool) => tool.enabled && matchesCapabilityQuery(tool, normalizedQuery)).map((tool) => tool.id),
    }))
    .filter((server) => server.ids.length);

  const selectVisible = () => {
    onModeChange('custom');
    onCapabilityIdsChange((current) => mergeValues(current, visibleCapabilities.map((capability) => capability.id)));
    onSkillNamesChange((current) => mergeValues(current, visibleSkills.map((skill) => skillLookupName(skill))));
  };

  const clearSelection = () => {
    onModeChange('custom');
    onCapabilityIdsChange([]);
    onSkillNamesChange([]);
    onAgentScopeChange('none');
    onAgentIdsChange([]);
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Chat capabilities">
      <div className="capability-scope-modal">
        <header className="modal-header">
          <div>
            <div className="modal-title">
              <SlidersHorizontal size={20} />
              <span>Chat Capabilities</span>
            </div>
            <p>{chatCapabilityScopeLabel(mode, selectedCapabilityIds.length, selectedSkillNames.length, agentScope, selectedAgentIds.length)}</p>
          </div>
          <div className="modal-actions">
            <button className="secondary-button compact-button" onClick={selectVisible} type="button">
              Select visible
            </button>
            <button className="secondary-button compact-button" onClick={clearSelection} type="button">
              Clear
            </button>
            <button className="icon-button" onClick={onClose} title="Close" type="button">
              <X size={18} />
            </button>
          </div>
        </header>
        <div className="capability-scope-body">
          <aside className="capability-scope-sidebar">
            <div className="scope-mode-switch" role="tablist" aria-label="Capability scope mode">
              <button className={mode === 'all' ? 'active' : ''} onClick={() => onModeChange('all')} type="button">
                All enabled
              </button>
              <button className={mode === 'custom' ? 'active' : ''} onClick={() => onModeChange('custom')} type="button">
                Custom
              </button>
            </div>
            <div className="search-field scope-search">
              <Search size={15} />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search capabilities" />
            </div>
            {mcpServerCounts.length ? (
              <div className="scope-server-list">
                <h3>MCP Servers</h3>
                {mcpServerCounts.map((server) => (
                  <button
                    key={server.name}
                    className="scope-server-button"
                    onClick={() => {
                      onModeChange('custom');
                      onCapabilityIdsChange((current) => mergeValues(current, server.ids));
                    }}
                    type="button"
                  >
                    <span>{server.name}</span>
                    <strong>{server.ids.length}</strong>
                  </button>
                ))}
              </div>
            ) : null}
            <div className="scope-server-list">
              <h3>Agent Delegation</h3>
              <div className="scope-mode-switch" role="tablist" aria-label="Agent delegation mode">
                <button className={agentScope === 'none' ? 'active' : ''} onClick={() => { onAgentScopeChange('none'); onAgentIdsChange([]); }} type="button">
                  不启用
                </button>
                <button className={agentScope === 'selected' ? 'active' : ''} onClick={() => onAgentScopeChange('selected')} type="button">
                  指定预设
                </button>
                <button className={agentScope === 'registered' ? 'active' : ''} onClick={() => { onAgentScopeChange('registered'); onAgentIdsChange([]); }} type="button">
                  全部预设
                </button>
              </div>
            </div>
          </aside>
          <div className="capability-scope-list">
            {agentScope === 'selected' ? (
              <section className="scope-group">
                <h3>智能体预设</h3>
                {visibleAgents.map((agent) => (
                  <label className="scope-row" key={agent.id}>
                    <input
                      type="checkbox"
                      checked={selectedAgents.has(agent.id)}
                      onChange={(event) => onAgentIdsChange((current) => toggleValue(current, agent.id, event.target.checked))}
                    />
                    <span>
                      <strong>{agent.id}</strong>
                      <span>{agent.profile}{agent.description ? ` · ${agent.description}` : ''}</span>
                    </span>
                  </label>
                ))}
                {!visibleAgents.length ? <div className="empty-state compact">没有匹配的智能体预设。</div> : null}
              </section>
            ) : null}
            {groups.map((group) => (
              <section className="scope-group" key={group.kind}>
                <h3>{group.kind}</h3>
                {group.items.map((capability) => (
                  <label className="scope-row" key={capability.id}>
                    <input
                      type="checkbox"
                      checked={mode === 'custom' && selectedCapabilities.has(capability.id)}
                      onChange={(event) => {
                        onModeChange('custom');
                        onCapabilityIdsChange((current) => toggleValue(current, capability.id, event.target.checked));
                      }}
                    />
                    <span>
                      <strong>{capabilityDisplayName(capability)}</strong>
                      <span>{capabilityScopeDetail(capability)}</span>
                    </span>
                  </label>
                ))}
              </section>
            ))}
            {visibleSkills.length ? (
              <section className="scope-group">
                <h3>skills</h3>
                {visibleSkills.map((skill) => {
                  const lookup = skillLookupName(skill);
                  return (
                    <label className="scope-row" key={skill.path}>
                      <input
                        type="checkbox"
                        checked={mode === 'custom' && selectedSkills.has(lookup)}
                        onChange={(event) => {
                          onModeChange('custom');
                          onSkillNamesChange((current) => toggleValue(current, lookup, event.target.checked));
                        }}
                      />
                      <span>
                        <strong>{skill.name}</strong>
                        <span>{skill.category ? `${skill.category} · ${skill.path}` : skill.path}</span>
                      </span>
                    </label>
                  );
                })}
              </section>
            ) : null}
            {!groups.length && !visibleSkills.length && !(agentScope === 'selected' && visibleAgents.length) ? <div className="empty-state compact">No matching capabilities.</div> : null}
          </div>
        </div>
      </div>
    </div>
  );
}

interface DynamicNodeExecutionRow {
  nodeId: string;
  title: string;
  detail: string;
  status: TraceLogEvent['status'];
  events: DynamicTraceLogEvent[];
  timelineOrder: number;
}

type DynamicTimelineItem =
  | { type: 'message'; id: string; order: number; message: DynamicChatMessage }
  | { type: 'status'; id: string; order: number; content: string }
  | { type: 'nodes'; id: string; order: number; rows: DynamicNodeExecutionRow[] }
  | { type: 'empty'; id: string; order: number; content: string }
  | { type: 'final'; id: string; order: number; content: string };

function executionOrderedNodes(dag: Dag): DagNode[] {
  const nodes = (dag.nodes ?? []).map(normalizeNode);
  const indexById = new Map(nodes.map((node, index) => [node.id, index]));
  const incoming = new Map(nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(nodes.map((node) => [node.id, [] as string[]]));

  for (const edge of dag.edges ?? []) {
    if (!incoming.has(edge.source) || !incoming.has(edge.target)) continue;
    incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1);
    outgoing.get(edge.source)?.push(edge.target);
  }

  const ready = nodes
    .filter((node) => (incoming.get(node.id) ?? 0) === 0)
    .sort((a, b) => (indexById.get(a.id) ?? 0) - (indexById.get(b.id) ?? 0));
  const ordered: DagNode[] = [];

  while (ready.length) {
    const node = ready.shift();
    if (!node) break;
    ordered.push(node);
    for (const targetId of outgoing.get(node.id) ?? []) {
      const nextIncoming = (incoming.get(targetId) ?? 0) - 1;
      incoming.set(targetId, nextIncoming);
      if (nextIncoming === 0) {
        const target = nodes.find((item) => item.id === targetId);
        if (target) {
          ready.push(target);
          ready.sort((a, b) => (indexById.get(a.id) ?? 0) - (indexById.get(b.id) ?? 0));
        }
      }
    }
  }

  return ordered.length === nodes.length ? ordered : nodes;
}

function dynamicNodeExecutionRows(dag: Dag, trace: DynamicTraceLogEvent[]): DynamicNodeExecutionRow[] {
  const eventsByNode = new Map<string, DynamicTraceLogEvent[]>();
  for (const event of trace) {
    if (!event.node_id) continue;
    const events = eventsByNode.get(event.node_id) ?? [];
    events.push(event);
    eventsByNode.set(event.node_id, events);
  }

  const rows: DynamicNodeExecutionRow[] = [];
  const rendered = new Set<string>();
  for (const node of executionOrderedNodes(dag)) {
    const events = eventsByNode.get(node.id) ?? [];
    if (!events.length) continue;
    const latest = events[events.length - 1];
    rows.push({
      nodeId: node.id,
      title: node.title || node.id,
      detail: nodeDisplayDetail(node),
      status: latest.status,
      events,
      timelineOrder: Math.min(...events.map((event) => event.timelineOrder)),
    });
    rendered.add(node.id);
  }

  for (const [nodeId, events] of eventsByNode) {
    if (rendered.has(nodeId) || !events.length) continue;
    const latest = events[events.length - 1];
    rows.push({
      nodeId,
      title: nodeId,
      detail: latest.label,
      status: latest.status,
      events,
      timelineOrder: Math.min(...events.map((event) => event.timelineOrder)),
    });
  }
  return rows;
}

function dynamicNodeStatusLabel(status: TraceLogEvent['status']): string {
  if (status === 'running') return '执行中';
  if (status === 'completed') return '已完成';
  if (status === 'failed') return '失败';
  if (status === 'awaiting_review') return '待审核';
  if (status === 'rejected') return '已拒绝';
  return '待执行';
}

function dynamicTimelineItems(
  messages: DynamicChatMessage[],
  statusMessage: string,
  statusMessageOrder: number,
  rows: DynamicNodeExecutionRow[],
  finalAnswer: string,
  finalAnswerOrder: number,
  running: boolean,
): DynamicTimelineItem[] {
  const items: DynamicTimelineItem[] = messages.map((message, index) => ({
    type: 'message',
    id: `message-${index}`,
    order: message.timelineOrder,
    message,
  }));

  if (statusMessage) {
    items.push({ type: 'status', id: 'status', order: statusMessageOrder, content: statusMessage });
  }
  if (rows.length) {
    items.push({ type: 'nodes', id: 'nodes', order: Math.min(...rows.map((row) => row.timelineOrder)), rows });
  } else if (!finalAnswer) {
    items.push({
      type: 'empty',
      id: 'empty',
      order: statusMessageOrder || Number.MAX_SAFE_INTEGER,
      content: running ? '等待节点执行事件...' : '暂无节点执行结果',
    });
  }
  if (finalAnswer) {
    items.push({ type: 'final', id: 'final', order: finalAnswerOrder, content: finalAnswer });
  }

  return items.sort((left, right) => left.order - right.order);
}

function DynamicMarkdown({ content, className = '' }: { content: string; className?: string }) {
  return (
    <div className={`dynamic-markdown markdown-body ${className}`.trim()}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content || '...'}</ReactMarkdown>
    </div>
  );
}

function DynamicChatEvents({
  dag,
  finalAnswer,
  finalAnswerOrder,
  message,
  messageOrder,
  messages,
  running,
  trace,
}: {
  dag: Dag;
  finalAnswer: string;
  finalAnswerOrder: number;
  message: string;
  messageOrder: number;
  messages: DynamicChatMessage[];
  running: boolean;
  trace: DynamicTraceLogEvent[];
}) {
  const rows = dynamicNodeExecutionRows(dag, trace);
  const finalText = finalAnswer.trim();
  const timelineItems = dynamicTimelineItems(messages, message, messageOrder, rows, finalText, finalAnswerOrder, running);

  return (
    <>
      {timelineItems.map((item) => {
        if (item.type === 'message') {
          return (
            <div className={`dynamic-chat-bubble ${item.message.role} dynamic-conversation-bubble`} key={item.id}>
              <span>{item.message.role === 'user' ? '你' : '助手'}</span>
              <DynamicMarkdown content={item.message.content} />
            </div>
          );
        }
        if (item.type === 'status') {
          return (
            <div className="dynamic-chat-bubble assistant dynamic-event-bubble" key={item.id}>
              <span>状态</span>
              <DynamicMarkdown content={item.content} />
            </div>
          );
        }
        if (item.type === 'nodes') {
          return (
            <div className="dynamic-node-result-list" key={item.id}>
              {item.rows.map((row, index) => (
                <details className={`dynamic-chat-bubble assistant dynamic-node-result-card ${row.status}`} key={row.nodeId}>
                  <summary className="dynamic-node-result-summary">
                    <span>{String(index + 1).padStart(2, '0')} · {dynamicNodeStatusLabel(row.status)}</span>
                    <strong title={row.nodeId}>{row.title}</strong>
                    <em title={row.detail}>{row.detail}</em>
                    <ChevronRight className="timeline-chevron" size={15} />
                  </summary>
                  {row.events.length ? (
                    <div className="dynamic-node-result-events">
                      {row.events.slice(-4).map((event) => (
                        <div className={`dynamic-node-result-event ${event.status}`} key={event.id}>
                          <span>{event.timestamp} · {event.type}</span>
                          <DynamicMarkdown content={event.detail || event.label} />
                        </div>
                      ))}
                    </div>
                  ) : null}
                </details>
              ))}
            </div>
          );
        }
        if (item.type === 'empty') {
          return (
            <div className="dynamic-chat-bubble assistant dynamic-event-bubble muted" key={item.id}>
              <span>事件</span>
              <p>{item.content}</p>
            </div>
          );
        }
        return (
          <div className="dynamic-chat-bubble assistant dynamic-final-result" key={item.id}>
            <span>最终结果</span>
            <DynamicMarkdown content={item.content} />
          </div>
        );
      })}
    </>
  );
}

function DynamicOrchestrationWorkspace({
  capabilities,
  dag,
  finalAnswer,
  finalAnswerOrder,
  nodes,
  edges,
  selectedId,
  prompt,
  dynamicAdjust,
  canRunDag,
  running,
  message,
  messageOrder,
  messages,
  trace,
  onAddNode,
  onPatchNode,
  onDeleteNode,
  onNodesChange,
  onEdgesChange,
  onConnect,
  onSelectNode,
  onPromptChange,
  onDynamicAdjustChange,
  onGenerate,
  onRun,
}: {
  capabilities: CapabilityDefinition[];
  dag: Dag;
  finalAnswer: string;
  finalAnswerOrder: number;
  nodes: Node[];
  edges: Edge[];
  selectedId: string;
  prompt: string;
  dynamicAdjust: boolean;
  canRunDag: boolean;
  running: boolean;
  message: string;
  messageOrder: number;
  messages: DynamicChatMessage[];
  trace: DynamicTraceLogEvent[];
  onAddNode: (capability?: CapabilityDefinition, position?: XYPosition) => void;
  onPatchNode: (nodeId: string, patch: Partial<DagNode>, edges?: DagEdge[]) => void;
  onDeleteNode: (nodeId?: string) => void;
  onNodesChange: (changes: NodeChange[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onConnect: (connection: Connection) => void;
  onSelectNode: (id: string) => void;
  onPromptChange: (value: string) => void;
  onDynamicAdjustChange: (value: boolean) => void;
  onGenerate: () => void;
  onRun: () => void;
}) {
  const badgeStatus: Dag['status'] = running ? 'running' : dag.status;
  const canGenerate = prompt.trim().length > 0 && !running;
  const canRun = canRunDag && !running;
  const selectedNode = dag.nodes.find((node) => node.id === selectedId) ?? null;
  const selectedNormalized = selectedNode ? normalizeNode(selectedNode) : null;
  const selectedInvocation = selectedNormalized && isCapabilityNode(selectedNormalized)
    ? selectedNormalized.payload.invocation
    : null;
  const selectedCapability = selectedInvocation
    ? capabilities.find((capability) => capability.id === selectedInvocation.capability_id)
    : null;
  const enabledCapabilities = visibleCapabilitiesForPicker(capabilities);
  const selectableCapabilities = selectedCapability && !enabledCapabilities.some((capability) => capability.id === selectedCapability.id)
    ? [selectedCapability, ...enabledCapabilities]
    : enabledCapabilities;
  const [flowInstance, setFlowInstance] = useState<ReactFlowInstance | null>(null);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const firstNodePosition = () => nodes.length ? undefined : canvasCenterNodePosition(flowInstance, canvasRef.current);
  const patchSelectedInvocation = (patch: Partial<CapabilityInvocation>) => {
    if (!selectedNode || !selectedInvocation) return;
    onPatchNode(selectedNode.id, {
      payload: {
        type: 'capability',
        invocation: { ...selectedInvocation, ...patch },
      },
    });
  };

  return (
    <section className="design-orchestration-workspace dynamic-orchestration-workspace">
      <aside className="dynamic-orchestration-chat">
        <div className="dynamic-chat-head">
          <strong>动态编排</strong>
        </div>
        <div className="dynamic-chat-feed">
          <div className="dynamic-chat-bubble assistant">
            <span>系统</span>
            <DynamicMarkdown content={dynamicAdjust ? '生成 DAG 后可根据反馈继续调整；运行中重规划会再次审核。' : '生成 DAG 后按固定图执行，不自动重规划。'} />
          </div>
          <DynamicChatEvents
            dag={dag}
            finalAnswer={finalAnswer}
            finalAnswerOrder={finalAnswerOrder}
            message={message}
            messageOrder={messageOrder}
            messages={messages}
            running={running}
            trace={trace}
          />
        </div>
        <div className="dynamic-chat-composer">
          <textarea
            value={prompt}
            onChange={(event) => onPromptChange(event.target.value)}
            placeholder={dag.nodes.length ? '输入新的要求来修改当前 DAG...' : '描述任务目标，或粘贴 SOP 来生成 DAG...'}
            spellCheck={false}
          />
          <div className="dynamic-chat-actions">
            <button
              className="secondary-button compact-button"
              disabled={!canGenerate}
              onClick={onGenerate}
              type="button"
            >
              {running ? <Loader size={14} className="spin" /> : <GitBranch size={14} />}
              生成 DAG
            </button>
            <button
              className="primary-button compact-button"
              disabled={!canRun}
              onClick={onRun}
              type="button"
            >
              <Play size={14} />
              运行
            </button>
          </div>
        </div>
      </aside>
      <div className="orchestration-main">
        <div className="orchestration-toolbar">
          <Play size={17} />
          <strong className="orchestration-title-text">动态编排</strong>
          <span className="orchestration-version">目标 / SOP → DAG</span>
          <StatusBadge status={badgeStatus} />
          <div className="orchestration-actions">
            <button
              className={`validation-toggle dynamic-adjust-toggle ${dynamicAdjust ? 'active' : ''}`}
              type="button"
              onClick={() => onDynamicAdjustChange(!dynamicAdjust)}
              title="控制生成初始 DAG 后是否允许根据执行结果继续调整"
              aria-pressed={dynamicAdjust}
            >
              <span />
              {dynamicAdjust ? '动态调整 开' : '动态调整 关'}
            </button>
            <button className="secondary-button compact-button" onClick={() => onAddNode(undefined, firstNodePosition())} type="button">
              <Plus size={14} />
              添加节点
            </button>
          </div>
        </div>

        <div className={`dynamic-orchestration-body ${selectedNormalized ? 'with-inspector' : ''}`}>
          <div className="orchestration-canvas dynamic-orchestration-canvas" ref={canvasRef}>
            <ReactFlow
              className="orchestration-flow"
              nodes={nodes}
              edges={edges}
              nodeTypes={designNodeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onNodeClick={(_, node) => onSelectNode(node.id)}
              onPaneClick={() => onSelectNode('')}
              nodesDraggable
              nodesConnectable
              elementsSelectable
              onInit={setFlowInstance}
              defaultViewport={{ x: 0, y: 0, zoom: 1 }}
              fitView={false}
              proOptions={{ hideAttribution: true }}
            >
              <Background color="#d8dade" gap={20} />
              <CanvasViewportControls hasNodes={nodes.length > 0} />
            </ReactFlow>
            {!nodes.length ? (
              <button className="orchestration-empty-canvas dynamic-orchestration-empty" onClick={() => onAddNode(undefined, firstNodePosition())} type="button">
                <Plus size={15} />
                添加第一个节点
              </button>
            ) : null}
          </div>
          {selectedNormalized ? (
            <aside className="node-inspector dynamic-node-inspector" aria-label="节点检查器">
              <div className="node-inspector-body">
                <div className="node-inspector-title">
                  <span>节点检查器</span>
                  <strong>{selectedNormalized.title || selectedNormalized.id}</strong>
                </div>
                <div className="inspector-field">
                  <label>节点 ID</label>
                  <input
                    value={selectedNormalized.id}
                    onChange={(event) => onPatchNode(selectedNormalized.id, { id: event.target.value })}
                  />
                </div>
                <div className="inspector-field">
                  <label>标题</label>
                  <input
                    value={selectedNormalized.title ?? ''}
                    onChange={(event) => onPatchNode(selectedNormalized.id, { title: event.target.value })}
                  />
                </div>
                {selectedInvocation ? (
                  <>
                    <div className="inspector-field">
                      <label>能力</label>
                      <select
                        value={selectedInvocation.capability_id}
                        onChange={(event) => {
                          const capability = capabilities.find((item) => item.id === event.target.value);
                          patchSelectedInvocation({
                            capability_id: event.target.value,
                            kind: capability?.kind ?? selectedInvocation.kind,
                            arguments: resetSchemaArguments(
                              selectedInvocation.arguments ?? {},
                              capability?.parameters,
                              selectedCapability?.parameters,
                            ),
                            risk: capabilityRisk(capability),
                          });
                        }}
                      >
                        <option value="">选择能力...</option>
                        {selectableCapabilities.map((capability) => (
                          <option key={capability.id} value={capability.id}>
                            {capabilityDisplayName(capability)}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="inspector-split">
                      <div className="inspector-field">
                        <label>类型</label>
                        <select
                          value={selectedInvocation.kind ?? 'tool'}
                          onChange={(event) => patchSelectedInvocation({ kind: event.target.value as CapabilityKind })}
                        >
                          {capabilityKinds.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
                        </select>
                      </div>
                      <div className="inspector-field">
                        <label>风险</label>
                        <select
                          value={selectedInvocation.risk ?? 'low'}
                          onChange={(event) => patchSelectedInvocation({ risk: event.target.value as RiskLevel })}
                        >
                          {riskLevels.map((risk) => <option key={risk} value={risk}>{risk}</option>)}
                        </select>
                      </div>
                    </div>
                    <InspectorArgumentEditor
                      value={selectedInvocation.arguments ?? {}}
                      parameters={selectedCapability?.parameters}
                      onChange={(argumentsValue) => patchSelectedInvocation({ arguments: argumentsValue })}
                    />
                  </>
                ) : (
                  <div className="empty-state compact">入口节点无需配置能力。</div>
                )}
                <button className="danger-line-button" onClick={() => onDeleteNode(selectedNormalized.id)} type="button">
                  <Trash2 size={14} />
                  删除节点
                </button>
              </div>
            </aside>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function CanvasViewportControls({
  hasNodes,
}: {
  hasNodes: boolean;
}) {
  const flowInstance = useReactFlow();
  const viewportReady = flowInstance.viewportInitialized;
  const stopCanvasEvent = (event: React.SyntheticEvent) => {
    event.preventDefault();
    event.stopPropagation();
  };
  const centerCanvas = () => {
    if (!viewportReady || !hasNodes) return;
    const visibleNodes = flowInstance.getNodes();
    if (!visibleNodes.length) return;
    const bounds = flowInstance.getNodesBounds(visibleNodes);
    void flowInstance.fitBounds(bounds, { padding: 0.25, duration: 220 });
  };
  const zoomInCanvas = () => {
    if (!viewportReady) return;
    void flowInstance.zoomIn({ duration: 160 });
  };
  const zoomOutCanvas = () => {
    if (!viewportReady) return;
    void flowInstance.zoomOut({ duration: 160 });
  };

  return (
    <div
      className="canvas-viewport-controls nopan nodrag"
      onPointerDown={stopCanvasEvent}
      onMouseDown={stopCanvasEvent}
      onClick={stopCanvasEvent}
      onDoubleClick={stopCanvasEvent}
      onContextMenu={stopCanvasEvent}
    >
      <button onClick={centerCanvas} disabled={!viewportReady || !hasNodes} title="居中显示" type="button">
        <Crosshair size={15} />
      </button>
      <button onClick={zoomInCanvas} disabled={!viewportReady} title="放大" type="button">
        <ZoomIn size={15} />
      </button>
      <button onClick={zoomOutCanvas} disabled={!viewportReady} title="缩小" type="button">
        <ZoomOut size={15} />
      </button>
    </div>
  );
}

function OrchestrationWorkspace({
  capabilities,
  skills,
  mcpServers,
  spec,
  dag,
  nodes,
  edges,
  selectedId,
  trace,
  run,
  runTimeline,
  message,
  running,
  runInputText,
  onPatchDag,
  onRunInputTextChange,
  onAddNode,
  onPatchNode,
  onDeleteNode,
  onSave,
  onRun,
  onNodesChange,
  onEdgesChange,
  onConnect,
  onSelectNode,
}: {
  capabilities: CapabilityDefinition[];
  skills: SkillSummary[];
  mcpServers: MCPServer[];
  spec: UserDag;
  dag: Dag;
  nodes: Node[];
  edges: Edge[];
  selectedId: string;
  trace: TraceLogEvent[];
  run: DagRun | null;
  runTimeline: RunTranscriptItem[];
  message: string;
  running: boolean;
  runInputText: string;
  onPatchDag: (patch: Partial<UserDag>) => void;
  onRunInputTextChange: (value: string) => void;
  onAddNode: (capability?: CapabilityDefinition, position?: XYPosition) => void;
  onPatchNode: (nodeId: string, patch: Partial<DagNode>, edges?: DagEdge[]) => void;
  onDeleteNode: (nodeId?: string) => void;
  onSave: () => void;
  onRun: () => void;
  onNodesChange: (changes: NodeChange[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onConnect: (connection: Connection) => void;
  onSelectNode: (id: string) => void;
}) {
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; flowPosition?: XYPosition; nodeId?: string } | null>(null);
  const [contextCapabilityId, setContextCapabilityId] = useState('');
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [flowInstance, setFlowInstance] = useState<ReactFlowInstance | null>(null);
  const selectedNode = dag.nodes.find((node) => node.id === selectedId) ?? null;
  const selectedUserNode = selectedNode
    ? spec.nodes.find((node) => node.id === selectedNode.id) ?? null
    : null;
  const runSummary = buildRunDialogSummary(userDagFromRuntimeDag(spec, dag));
  const enabledCapabilities = visibleCapabilitiesForPicker(capabilities);
  const contextCapability = enabledCapabilities.find((capability) => capability.id === contextCapabilityId) ?? enabledCapabilities[0];
  const selectedNormalized = selectedNode ? normalizeNode(selectedNode) : null;
  const selectedInvocation = selectedNormalized && isCapabilityNode(selectedNormalized)
    ? selectedNormalized.payload.invocation
    : null;
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const selectedCapability = selectedInvocation
    ? capabilities.find((capability) => capability.id === selectedInvocation.capability_id)
    : null;
  const staticNodeTitle = (node: DagNode): string => {
    const title = node.title?.trim();
    if (title) return title;
    if (isCapabilityNode(node)) {
      const capability = capabilities.find((item) => item.id === node.payload.invocation.capability_id);
      return capability ? capabilityDisplayName(capability) : node.payload.invocation.capability_id || '未命名节点';
    }
    return nodeDisplayTitle(node);
  };
  const selectedDisplayTitle = selectedNormalized ? staticNodeTitle(selectedNormalized) : '';
  const selectableCapabilities = selectedCapability && !enabledCapabilities.some((capability) => capability.id === selectedCapability.id)
    ? [selectedCapability, ...enabledCapabilities]
    : enabledCapabilities;
  const contextCapabilityGroups = capabilityOptionGroups(enabledCapabilities);
  const selectableCapabilityGroups = capabilityOptionGroups(selectableCapabilities);
  const artifactItems = Object.values(spec.artifacts ?? {}).sort(compareArtifactsByPath);
  const contextNode = contextMenu?.nodeId ? dag.nodes.find((node) => node.id === contextMenu.nodeId) : null;
  const contextMenuTitle = contextNode ? `节点：${staticNodeTitle(contextNode)}` : '画布';
  const flowPositionFromEvent = (event: MouseEvent | React.MouseEvent<Element>) =>
    flowInstance?.screenToFlowPosition({ x: event.clientX, y: event.clientY });
  const firstNodePosition = () => nodes.length ? undefined : canvasCenterNodePosition(flowInstance, canvasRef.current);

  const openCanvasMenu = (event: MouseEvent | React.MouseEvent<Element>) => {
    event.preventDefault();
    setContextMenu({ x: event.clientX, y: event.clientY, flowPosition: flowPositionFromEvent(event) });
    setContextCapabilityId((current) => current || enabledCapabilities[0]?.id || '');
  };
  const openNodeMenu = (event: React.MouseEvent, nodeId: string) => {
    event.preventDefault();
    event.stopPropagation();
    onSelectNode(nodeId);
    setContextMenu({ x: event.clientX, y: event.clientY, flowPosition: flowPositionFromEvent(event), nodeId });
    setContextCapabilityId((current) => current || enabledCapabilities[0]?.id || '');
  };
  const handlePaneClick = () => {
    setContextMenu(null);
    onSelectNode('');
  };
  const addFromContext = () => {
    if (!contextMenu) return;
    if (contextCapability) {
      onAddNode(contextCapability, contextMenu.flowPosition);
    }
    setContextMenu(null);
  };
  const deleteFromContext = () => {
    if (contextMenu?.nodeId) onDeleteNode(contextMenu.nodeId);
    setContextMenu(null);
  };
  const patchSelectedInvocation = (patch: Partial<CapabilityInvocation>, nextEdges?: DagEdge[]) => {
    if (!selectedNode || !selectedInvocation) return;
    onPatchNode(selectedNode.id, {
      payload: {
        type: 'capability',
        invocation: { ...selectedInvocation, ...patch },
      },
    }, nextEdges);
  };
  const patchSelectedAgentConfig = (agent: UserDagAgentConfig | undefined) => {
    if (!selectedNode) return;
    onPatchDag({
      nodes: spec.nodes.map((node) =>
        node.id === selectedNode.id
          ? normalizeUserDagNode({ ...node, agent })
          : node,
      ),
    });
  };
  function ensureBindingDependency(argumentsValue: Record<string, unknown>): DagEdge[] | undefined {
    if (!selectedNode) return undefined;
    const refs = collectNodeOutputRefs(argumentsValue);
    if (!refs.length) return undefined;
    let nextEdges = dag.edges ?? [];
    let changed = false;
    for (const ref of refs) {
      if (ref.nodeId === selectedNode.id) continue;
      const exists = nextEdges.some((edge) => edge.source === ref.nodeId && edge.target === selectedNode.id);
      if (exists || wouldCreateCycle(nextEdges, ref.nodeId, selectedNode.id)) continue;
      nextEdges = [
        ...nextEdges,
        {
          source: ref.nodeId,
          target: selectedNode.id,
          reason: 'Parameter binding.',
        },
      ];
      changed = true;
    }
    return changed ? nextEdges : undefined;
  }
  const patchArtifactList = (field: 'inputs' | 'outputs', artifactId: string, checked: boolean) => {
    if (!selectedNode || !selectedInvocation) return;
    const current = selectedNode[field] ?? [];
    const next = checked
      ? [...current.filter((id) => id !== artifactId), artifactId].sort()
      : current.filter((id) => id !== artifactId);
    if (field === 'inputs' && checked) {
      const boundary = selectedInvocation.boundary ?? {
        allowed_paths: ['.'],
      };
      onPatchNode(selectedNode.id, {
        [field]: next,
        payload: {
          type: 'capability',
          invocation: {
            ...selectedInvocation,
            boundary: {
              ...boundary,
              allowed_paths: addUniqueBoundaryValue(boundary.allowed_paths ?? [], artifactPathExpr(artifactId)),
            },
          },
        },
      });
      return;
    }
    onPatchNode(selectedNode.id, { [field]: next });
  };

  return (
    <section className="design-orchestration-workspace">
      <div className="orchestration-main">
        <div className="orchestration-toolbar">
          <GitBranch size={17} />
          <input
            className="orchestration-name-input"
            style={{ width: `${dagNameInputCh(spec.name || 'untitled_dag')}ch` }}
            value={spec.name || ''}
            onChange={(event) => onPatchDag({ name: event.target.value })}
            placeholder="untitled_dag"
          />
          <span className="orchestration-version">v{spec.version ?? 1}</span>
          <StatusBadge status={dag.status} />
          <div className="orchestration-actions">
            <button className="secondary-button compact-button" onClick={onSave} type="button">
              <Save size={15} />
              保存
            </button>
            <button className="primary-button compact-button" onClick={() => setRunDialogOpen(true)} type="button">
              <Play size={14} />
              运行
            </button>
          </div>
        </div>

        <div className="orchestration-canvas" ref={canvasRef}>
          <ReactFlow
            key={spec.id}
            className="orchestration-flow"
            nodes={nodes}
            edges={edges}
            nodeTypes={designNodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={(_, node) => onSelectNode(node.id)}
            onNodeContextMenu={(event, node) => openNodeMenu(event, node.id)}
            onPaneClick={handlePaneClick}
            onPaneContextMenu={openCanvasMenu}
            onInit={setFlowInstance}
            defaultViewport={{ x: 0, y: 0, zoom: 1 }}
            fitView={false}
            proOptions={{ hideAttribution: true }}
          >
            <Background color="#d8dade" gap={20} />
            <CanvasViewportControls hasNodes={nodes.length > 0} />
          </ReactFlow>
          {!nodes.length ? (
            <button className="orchestration-empty-canvas" onClick={() => onAddNode(undefined, firstNodePosition())} type="button">
              <Plus size={15} />
              添加第一个节点
            </button>
          ) : null}
        </div>
        {contextMenu ? (
          <div
            className="orchestration-context-menu"
            style={{ left: contextMenu.x, top: contextMenu.y }}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="context-menu-title">{contextMenuTitle}</div>
            <label className="context-select">
              能力
              <select value={contextCapability?.id ?? ''} onChange={(event) => setContextCapabilityId(event.target.value)}>
                {contextCapabilityGroups.map((group) => (
                  <optgroup key={group.kind} label={group.label}>
                    {group.items.map((capability) => (
                      <option key={capability.id} value={capability.id}>
                        {capabilityDisplayName(capability)}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
            <button className="context-menu-item" onClick={addFromContext} disabled={!contextCapability} type="button">
              <Plus size={15} />
              添加节点
            </button>
            {contextMenu.nodeId ? (
              <button className="context-menu-item danger" onClick={deleteFromContext} type="button">
                <Trash2 size={15} />
                删除节点
              </button>
            ) : null}
          </div>
        ) : null}
      </div>

      {selectedNormalized ? (
        <aside className="node-inspector static-node-inspector" aria-label="节点检查器">
          <div className="node-inspector-body">
            <div className="node-inspector-title">
              <span>节点检查器</span>
              <strong>{selectedDisplayTitle}</strong>
            </div>
            <div className="inspector-field">
              <label>标题</label>
              <input
                value={selectedNormalized.title ?? ''}
                onChange={(event) => onPatchNode(selectedNormalized.id, { title: event.target.value })}
                placeholder={selectedDisplayTitle}
              />
            </div>
            {selectedInvocation ? (
              <>
                <div className="inspector-field">
                  <label>能力</label>
                  <select
                    value={selectedInvocation.capability_id}
                    onChange={(event) => {
                      const capability = capabilities.find((item) => item.id === event.target.value);
                      patchSelectedInvocation({
                        capability_id: event.target.value,
                        kind: capability?.kind ?? selectedInvocation.kind,
                        arguments: resetSchemaArguments(
                          selectedInvocation.arguments ?? {},
                          capability?.parameters,
                          selectedCapability?.parameters,
                        ),
                        risk: capabilityRisk(capability),
                      });
                    }}
                  >
                    <option value="">选择能力...</option>
                    {selectableCapabilityGroups.map((group) => (
                      <optgroup key={group.kind} label={group.label}>
                        {group.items.map((capability) => (
                          <option key={capability.id} value={capability.id}>
                            {capabilityDisplayName(capability)}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                </div>
                <div className="inspector-split">
                  <div className="inspector-field">
                    <label>类型</label>
                    <select
                      value={selectedInvocation.kind ?? 'tool'}
                      onChange={(event) => patchSelectedInvocation({ kind: event.target.value as CapabilityKind })}
                    >
                      {capabilityKinds.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
                    </select>
                  </div>
                  <div className="inspector-field">
                    <label>风险</label>
                    <select
                      value={selectedInvocation.risk ?? 'low'}
                      onChange={(event) => patchSelectedInvocation({ risk: event.target.value as RiskLevel })}
                    >
                      {riskLevels.map((risk) => <option key={risk} value={risk}>{risk}</option>)}
                    </select>
                  </div>
                </div>
                <div className="inspector-field">
                  <InspectorArgumentEditor
                    value={selectedInvocation.arguments ?? {}}
                    parameters={selectedCapability?.parameters}
                    dag={dag}
                    nodeId={selectedNormalized.id}
                    inputSchema={spec.input_schema ?? {}}
                    artifacts={spec.artifacts ?? {}}
                    capabilities={capabilities}
                    onEnsureDependency={ensureBindingDependency}
                    onChange={(argumentsValue, nextEdges) => patchSelectedInvocation({ arguments: argumentsValue }, nextEdges)}
                  />
                </div>
                {isAgentTarget(selectedInvocation.capability_id) ? (
                  <AgentNodeScopeEditor
                    capabilities={capabilities}
                    skills={skills}
                    mcpServers={mcpServers}
                    config={selectedUserNode?.agent}
                    onChange={patchSelectedAgentConfig}
                  />
                ) : null}
              </>
            ) : (
              <div className="empty-state compact">入口节点无需配置能力。</div>
            )}

            <div className="inspector-section-head">
              <span>Artifact 绑定</span>
            </div>
            <div className="inspector-artifact-list">
              {artifactItems.length ? artifactItems.map((artifact) => (
                <div className="inspector-artifact-row" key={artifact.id}>
                  <code>{artifactKindLabel(artifact)}</code>
                  <span title={artifactDisplayPath(artifact)}>
                    {artifactDisplayName(artifact)}
                  </span>
                  <label>
                    <input
                      type="checkbox"
                      checked={(selectedNormalized.inputs ?? []).includes(artifact.id)}
                      onChange={(event) => patchArtifactList('inputs', artifact.id, event.target.checked)}
                    />
                    输入
                  </label>
                  <label>
                    <input
                      type="checkbox"
                      checked={(selectedNormalized.outputs ?? []).includes(artifact.id)}
                      onChange={(event) => patchArtifactList('outputs', artifact.id, event.target.checked)}
                    />
                    输出
                  </label>
                </div>
              )) : <div className="empty-state compact">暂无 artifacts。</div>}
            </div>

            <button className="danger-line-button" onClick={() => onDeleteNode(selectedNormalized.id)} type="button">
              <Trash2 size={14} />
              删除节点
            </button>

            {trace.filter((event) => event.node_id === selectedNormalized.id).length ? (
              <NodeExecutionLog logs={trace.filter((event) => event.node_id === selectedNormalized.id)} />
            ) : null}
          </div>
        </aside>
      ) : null}
      {runDialogOpen ? (
        <RunDagDialog
          specName={spec.name}
          summary={runSummary}
          run={run}
          timeline={runTimeline}
          running={running}
          message={message}
          inputText={runInputText}
          onInputTextChange={onRunInputTextChange}
          onStart={onRun}
          onClose={() => setRunDialogOpen(false)}
        />
      ) : null}
    </section>
  );
}

function AgentNodeScopeEditor({
  capabilities,
  skills,
  mcpServers,
  config,
  onChange,
}: {
  capabilities: CapabilityDefinition[];
  skills: SkillSummary[];
  mcpServers: MCPServer[];
  config?: UserDagAgentConfig | null;
  onChange: (config: UserDagAgentConfig | undefined) => void;
}) {
  const isCustom = isCustomAgentScope(config);
  const availableCapabilities = capabilities
    .filter((capability) => capability.enabled && capability.kind !== 'agent' && capability.kind !== 'skill');
  const availableCapabilityIds = availableCapabilities.map((capability) => capability.id);
  const availableSkillNames = skills.map(skillLookupName);
  const selectedCapabilityIds = config?.capabilities ?? [];
  const selectedSkillNames = config?.skills ?? [];
  const selectedCapabilities = new Set(selectedCapabilityIds);
  const selectedSkills = new Set(selectedSkillNames);
  const groups = capabilityOptionGroups(availableCapabilities);
  const availableCapabilityIdSet = new Set(availableCapabilityIds);
  const mcpServerCounts = mcpServers
    .map((server) => ({
      name: server.name,
      ids: server.tools
        .filter((tool) => tool.enabled && availableCapabilityIdSet.has(tool.id))
        .map((tool) => tool.id),
    }))
    .filter((server) => server.ids.length);
  const customConfig = (capabilityIds = selectedCapabilityIds, skillNames = selectedSkillNames): UserDagAgentConfig => ({
    capabilities: capabilityIds,
    skills: skillNames,
  });
  const selectAllScope = () => onChange(customConfig(availableCapabilityIds, availableSkillNames));
  const clearAll = () => onChange(customConfig([], []));
  const patchCapabilities = (capabilityIds: string[]) => onChange(customConfig(capabilityIds, selectedSkillNames));
  const patchSkills = (skillNames: string[]) => onChange(customConfig(selectedCapabilityIds, skillNames));
  const selectedCount = selectedCapabilityIds.length + selectedSkillNames.length;

  return (
    <section className="agent-node-scope">
      <div className="agent-node-scope-head">
        <span>Agent 可用能力</span>
        <strong>{isCustom ? `已选 ${selectedCount}` : '全部启用'}</strong>
      </div>
      <div className="scope-mode-switch agent-node-scope-mode" role="tablist" aria-label="Agent capability scope mode">
        <button className={!isCustom ? 'active' : ''} onClick={() => onChange(undefined)} type="button">
          全部
        </button>
        <button className={isCustom ? 'active' : ''} onClick={selectAllScope} type="button">
          自定义
        </button>
      </div>
      {isCustom ? (
        <>
          <div className="agent-node-scope-actions">
            <button className="secondary-button compact-button" onClick={selectAllScope} type="button">
              全选
            </button>
            <button className="secondary-button compact-button" onClick={clearAll} type="button">
              清空
            </button>
          </div>
          {mcpServerCounts.length ? (
            <div className="agent-node-mcp-groups">
              {mcpServerCounts.map((server) => (
                <button
                  key={server.name}
                  className="scope-server-button"
                  onClick={() => patchCapabilities(mergeValues(selectedCapabilityIds, server.ids))}
                  type="button"
                >
                  <span>{server.name}</span>
                  <strong>{server.ids.length}</strong>
                </button>
              ))}
            </div>
          ) : null}
          <div className="agent-node-scope-list">
            {groups.map((group) => (
              <section className="scope-group" key={group.kind}>
                <h3>{group.label}</h3>
                {group.items.map((capability) => (
                  <label className="scope-row" key={capability.id}>
                    <input
                      type="checkbox"
                      checked={selectedCapabilities.has(capability.id)}
                      onChange={(event) => {
                        patchCapabilities(toggleValue(selectedCapabilityIds, capability.id, event.target.checked));
                      }}
                    />
                    <span>
                      <strong>{capabilityDisplayName(capability)}</strong>
                      <span>{capabilityScopeDetail(capability)}</span>
                    </span>
                  </label>
                ))}
              </section>
            ))}
            {skills.length ? (
              <section className="scope-group">
                <h3>Skills</h3>
                {skills.map((skill) => {
                  const lookup = skillLookupName(skill);
                  return (
                    <label className="scope-row" key={skill.path}>
                      <input
                        type="checkbox"
                        checked={selectedSkills.has(lookup)}
                        onChange={(event) => {
                          patchSkills(toggleValue(selectedSkillNames, lookup, event.target.checked));
                        }}
                      />
                      <span>
                        <strong>{skill.name}</strong>
                        <span>{skill.category ? `${skill.category} · ${skill.path}` : skill.path}</span>
                      </span>
                    </label>
                  );
                })}
              </section>
            ) : null}
          </div>
        </>
      ) : (
        <div className="agent-node-scope-default">
          当前 Runner 的可用能力
        </div>
      )}
    </section>
  );
}

function InspectorArgumentEditor({
  value,
  parameters,
  dag,
  nodeId,
  inputSchema = {},
  artifacts = {},
  capabilities = [],
  onEnsureDependency,
  onChange,
}: {
  value: Record<string, unknown>;
  parameters?: Record<string, unknown>;
  dag?: Dag;
  nodeId?: string;
  inputSchema?: Record<string, unknown>;
  artifacts?: Record<string, Artifact>;
  capabilities?: Pick<CapabilityDefinition, 'id' | 'kind' | 'output_schema'>[];
  onEnsureDependency?: (value: Record<string, unknown>) => DagEdge[] | undefined;
  onChange: (value: Record<string, unknown>, edges?: DagEdge[]) => void;
}) {
  const normalizedValue = ensureSchemaArguments(value, parameters);
  const fields = buildSchemaArgumentFields(value, parameters);
  const variableCatalog = dag && nodeId
    ? buildVariableCatalog(dag, nodeId, inputSchema, artifacts, capabilities)
    : null;
  const [mode, setMode] = useState<'kv' | 'raw'>('kv');
  const [rawText, setRawText] = useState(() => JSON.stringify(normalizedValue, null, 2));

  useEffect(() => {
    setRawText(JSON.stringify(ensureSchemaArguments(value, parameters), null, 2));
  }, [value, parameters]);

  const emitChange = (next: Record<string, unknown>) => {
    onChange(next, onEnsureDependency?.(next));
  };
  const updateKey = (oldKey: string, nextKey: string) => {
    const cleanKey = nextKey.trim();
    if (!cleanKey || (cleanKey !== oldKey && Object.prototype.hasOwnProperty.call(normalizedValue, cleanKey))) return;
    const next: Record<string, unknown> = {};
    for (const [key, itemValue] of Object.entries(normalizedValue)) {
      next[key === oldKey ? cleanKey : key] = itemValue;
    }
    emitChange(next);
  };
  const updateValue = (key: string, rawValue: string, type: ArgumentValueType) => {
    emitChange({
      ...normalizedValue,
      [key]: parseArgumentValue(rawValue, type, normalizedValue[key]),
    });
  };
  const updateBoundValue = (key: string, nextValue: unknown) => {
    emitChange({
      ...normalizedValue,
      [key]: nextValue,
    });
  };
  const addField = () => {
    let index = Object.keys(normalizedValue).length + 1;
    let key = `arg_${index}`;
    while (Object.prototype.hasOwnProperty.call(normalizedValue, key)) {
      index += 1;
      key = `arg_${index}`;
    }
    emitChange({ ...normalizedValue, [key]: '' });
  };
  const removeField = (key: string) => {
    const next = { ...normalizedValue };
    delete next[key];
    emitChange(next);
  };
  const applyRawText = () => {
    const parsed = parseJsonObject(rawText);
    if (parsed) emitChange(parsed);
  };

  return (
    <section className="inspector-argument-editor">
      <div className="inspector-argument-head">
        <span>参数</span>
        <div className="inspector-argument-toggle">
          <button className={mode === 'kv' ? 'active' : ''} onClick={() => setMode('kv')} type="button">
            键值
          </button>
          <button className={mode === 'raw' ? 'active' : ''} onClick={() => setMode('raw')} type="button">
            Raw
          </button>
        </div>
      </div>
      {mode === 'kv' ? (
        <div className="inspector-argument-kv">
          <div className="inspector-argument-header">
            <span>KEY</span>
            <span>VALUE</span>
            <span />
          </div>
          {fields.map((field, index) => {
            const { key, value: itemValue, valueType: type } = field;
            return (
              <div className="inspector-argument-row" key={`inspector-argument-${field.fixed ? key : index}`}>
                <input
                  value={key}
                  disabled={field.fixed}
                  onChange={(event) => updateKey(key, event.target.value)}
                  placeholder="key"
                  aria-label="参数名"
                />
                <ValueBindingEditor
                  value={itemValue}
                  valueType={type}
                  catalog={variableCatalog}
                  onLiteralChange={(rawValue) => updateValue(key, rawValue, type)}
                  onChange={(nextValue) => updateBoundValue(key, nextValue)}
                  title={field.description}
                />
                <button
                  onClick={() => removeField(key)}
                  disabled={field.fixed}
                  title={field.fixed ? 'Schema 参数不可删除' : '删除参数'}
                  type="button"
                >
                  <X size={13} />
                </button>
              </div>
            );
          })}
          <button className="inspector-argument-add" onClick={addField} type="button">
            <Plus size={13} />
            添加参数
          </button>
        </div>
      ) : (
        <textarea
          className="inspector-argument-raw"
          value={rawText}
          onChange={(event) => setRawText(event.target.value)}
          onBlur={applyRawText}
          rows={5}
          spellCheck={false}
        />
      )}
    </section>
  );
}

function ValueBindingEditor({
  value,
  valueType,
  catalog,
  title,
  onLiteralChange,
  onChange,
}: {
  value: unknown;
  valueType: ArgumentValueType;
  catalog: VariableCatalog | null;
  title?: string;
  onLiteralChange: (rawValue: string) => void;
  onChange: (value: unknown) => void;
}) {
  const isBinding = isValueBinding(value);
  const [mode, setMode] = useState<'literal' | 'binding'>(isBinding ? 'binding' : 'literal');
  const optionGroups = buildVariableOptionGroups(catalog);
  const options = optionGroups.flatMap((group) => group.items);
  const selectedValue = isBinding ? bindingOptionValue(value) : '';
  const hasSelectedOption = Boolean(selectedValue && options.some((option) => bindingOptionValue(option.binding) === selectedValue));

  useEffect(() => {
    setMode(isValueBinding(value) ? 'binding' : 'literal');
  }, [value]);

  const setBindingMode = () => {
    setMode('binding');
    if (!isBinding && options[0]) {
      onChange(options[0].binding);
    }
  };
  const setLiteralMode = () => {
    setMode('literal');
    if (isBinding) {
      onChange('');
    }
  };
  const selectBinding = (optionValue: string) => {
    const selected = options.find((option) => bindingOptionValue(option.binding) === optionValue);
    if (selected) onChange(selected.binding);
  };

  return (
    <div className="value-binding-editor">
      <div className="value-binding-toggle" role="group" aria-label="参数值类型">
        <button className={mode === 'literal' ? 'active' : ''} onClick={setLiteralMode} type="button">
          固定值
        </button>
        <button className={mode === 'binding' ? 'active' : ''} onClick={setBindingMode} type="button">
          变量
        </button>
      </div>
      {mode === 'binding' ? (
        <select
          value={selectedValue}
          onChange={(event) => selectBinding(event.target.value)}
          disabled={!options.length}
          aria-label="变量"
          title={isBinding ? bindingLabel(value) : title}
        >
          {!options.length ? <option value="">暂无可用变量</option> : null}
          {isBinding && selectedValue && !hasSelectedOption ? (
            <option value={selectedValue}>{bindingLabel(value)}（当前引用）</option>
          ) : null}
          {optionGroups.map((group) => (
            <optgroup key={group.label} label={group.label}>
              {group.items.map((item) => (
                <option key={item.id} value={bindingOptionValue(item.binding)}>
                  {item.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      ) : (
        <input
          value={isBinding ? '' : formatArgumentValue(value)}
          onChange={(event) => onLiteralChange(event.target.value)}
          placeholder="value"
          aria-label="参数值"
          title={title}
          data-value-type={valueType}
        />
      )}
    </div>
  );
}

function buildVariableOptionGroups(catalog: VariableCatalog | null): Array<{ label: string; items: VariableCatalogItem[] }> {
  if (!catalog) return [];
  return [
    { label: 'DAG 输入', items: catalog.graphInputs },
    { label: '节点输出', items: catalog.nodeOutputs },
    { label: 'Artifacts', items: catalog.artifacts },
  ].filter((group) => group.items.length);
}

function bindingOptionValue(binding: ValueBinding): string {
  return JSON.stringify(binding);
}

function RunDagDialog({
  specName,
  summary,
  run,
  timeline,
  running,
  message,
  inputText,
  onInputTextChange,
  onStart,
  onClose,
}: {
  specName: string;
  summary: RunDialogSummary;
  run: DagRun | null;
  timeline: RunTranscriptItem[];
  running: boolean;
  message: string;
  inputText: string;
  onInputTextChange: (value: string) => void;
  onStart: () => void;
  onClose: () => void;
}) {
  const state = running ? 'running' : run?.status ?? 'ready';
  const startLabel = running ? '运行中...' : run ? '再次运行' : '开始运行';
  const riskLabel = summary.riskyNodes.length ? `${summary.riskyNodes.length} 个中/高` : '0';
  const workspacePath = run?.workspace_path || '.dagent/runs';
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="运行编排">
      <div className="run-dialog">
        <header className="run-dialog-header">
          <div className="run-dialog-title">
            <Play size={17} />
            <strong>运行编排</strong>
            <code>{specName || 'untitled_dag'}</code>
            <span className={`run-state ${state}`}>{state}</span>
          </div>
          <button className="icon-button" onClick={onClose} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        <div className="run-dialog-body">
          <aside className="run-dialog-side">
            <div className="run-meta-table">
              <div>
                <span>节点</span>
                <strong>{summary.nodeCount}</strong>
              </div>
              <div>
                <span>风险</span>
                <strong>{riskLabel}</strong>
              </div>
              <div>
                <span>目录</span>
                <code>{workspacePath}</code>
              </div>
            </div>
            <label className="run-input-block">
              <span>初始输入 (可选 JSON)</span>
              <textarea
                value={inputText}
                disabled={running}
                spellCheck={false}
                placeholder='{ "topic": "竞品定价" }'
                onChange={(event) => onInputTextChange(event.target.value)}
              />
            </label>
            {summary.issues.length ? (
              <div className="run-issue-list">
                {summary.issues.map((issue, index) => (
                  <p key={`${issue.nodeId ?? 'spec'}-${index}`}>
                    <strong>{issue.nodeId ?? 'DAG'}</strong>
                    {runIssueText(issue)}
                  </p>
                ))}
              </div>
            ) : null}
            {message ? <p className="run-dialog-message">{message}</p> : null}
            <button
              className="primary-button run-start-button"
              onClick={onStart}
              disabled={running || !summary.canRun}
              type="button"
            >
              {running ? <Loader size={16} className="spin" /> : <Play size={16} />}
              {startLabel}
            </button>
          </aside>
          <section className="run-timeline-panel">
            <span className="run-panel-title">运行时间线</span>
            <RunTimeline timeline={timeline} running={running} state={state} />
          </section>
        </div>
      </div>
    </div>
  );
}

function RunTimeline({
  timeline,
  running,
  state,
}: {
  timeline: RunTranscriptItem[];
  running: boolean;
  state: string;
}) {
  const rows = timeline.map(runTimelineRow);
  const listRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    list.scrollTop = list.scrollHeight;
  }, [rows.length]);
  return (
    <div className="run-timeline-list" ref={listRef}>
      {rows.length ? rows.map((row, index) => (
        <details className={`run-timeline-row ${row.status}`} key={`${row.label}-${index}`} open={index === rows.length - 1}>
          <summary>
            <span>{row.label}</span>
            <code>{row.kind}</code>
            <ChevronRight size={15} />
          </summary>
          {row.item.type === 'capability' ? (
            <RunTimelineCapabilityDetails item={row.item} />
          ) : row.item.type === 'trace' ? (
            <RunTimelineTraceDetails item={row.item} />
          ) : (
            <RunTimelineTextDetails content={row.detail} />
          )}
        </details>
      )) : (
        <div className="run-timeline-empty">
          <span>{running ? '编排正在启动...' : state === 'ready' ? '点击「开始运行」启动编排' : '暂无运行事件'}</span>
        </div>
      )}
    </div>
  );
}

function RunTimelineTraceDetails({ item }: { item: Extract<RunTranscriptItem, { type: 'trace' }> }) {
  const payload = item.event.payload ?? {};
  const resultRecord = recordValue(payload.result);
  const argsText = formatTraceValue(payload.input) || '{}';
  const error = stringValue(payload.error) || stringValue(resultRecord?.error);
  const output = error
    || payload.output
    || resultRecord?.content
    || resultRecord?.value
    || item.event.detail;
  const resultText = formatTraceValue(output) || (item.event.status === 'running' ? '等待执行结果...' : '执行完成，未返回文本结果。');
  const failed = item.event.status === 'failed' || Boolean(error);
  return (
    <div className="run-timeline-detail">
      <section className="run-timeline-section">
        <span>参数</span>
        <RunTimelineCodeBlock value={argsText} />
      </section>
      <section className={`run-timeline-section ${failed ? 'failed' : ''}`}>
        <span>{failed ? '错误' : '执行结果'}</span>
        <RunTimelineCodeBlock value={resultText} />
      </section>
    </div>
  );
}

function RunTimelineCapabilityDetails({ item }: { item: Extract<RunTranscriptItem, { type: 'capability' }> }) {
  const argsText = formatCapabilityArguments(item.event.arguments) || '{}';
  const result = item.result ?? (item.event.type === 'capability.call.started' ? undefined : item.event);
  const failed = result?.type === 'capability.call.failed';
  const resultText = result
    ? result.content || (failed ? '调用失败，未返回错误详情。' : '执行完成，未返回文本结果。')
    : '等待执行结果...';
  return (
    <div className="run-timeline-detail">
      <section className="run-timeline-section">
        <span>参数</span>
        <RunTimelineCodeBlock value={argsText} />
      </section>
      <section className={`run-timeline-section ${failed ? 'failed' : ''}`}>
        <span>{failed ? '错误' : '执行结果'}</span>
        <RunTimelineCodeBlock value={resultText} />
      </section>
    </div>
  );
}

function RunTimelineTextDetails({ content }: { content: string }) {
  if (!content) return null;
  return (
    <div className="run-timeline-detail">
      <section className="run-timeline-section">
        <span>输出</span>
        <RunTimelineCodeBlock value={content} />
      </section>
    </div>
  );
}

function RunTimelineCodeBlock({ value }: { value: string }) {
  return (
    <pre className="run-timeline-code">{value}</pre>
  );
}

function runTimelineRow(item: RunTranscriptItem): { item: RunTranscriptItem; label: string; kind: string; detail: string; status: string } {
  if (item.type === 'text') {
    const content = item.content.trim();
    return {
      item,
      label: content.split('\n').find(Boolean)?.slice(0, 70) || '运行输出',
      kind: 'trace',
      detail: content,
      status: 'done',
    };
  }
  if (item.type === 'trace') {
    const payload = item.event.payload ?? {};
    const capabilityId = stringValue(payload.capability_id) || item.event.label;
    return {
      item,
      label: item.event.node_id ? `${item.event.node_id} · ${capabilityId}` : capabilityId,
      kind: item.event.type,
      detail: item.event.detail,
      status: item.event.status === 'failed' ? 'failed' : item.event.status === 'running' ? 'running' : 'done',
    };
  }
  const event = item.event;
  const result = item.result;
  const failed = result?.type === 'capability.call.failed';
  return {
    item,
    label: event.capability_id ? `${event.capability_id}` : '能力调用',
    kind: event.type.includes('review') ? 'review' : 'tool',
    detail: result?.content ?? '',
    status: failed ? 'failed' : result ? 'done' : 'running',
  };
}

function formatTraceValue(value: unknown): string {
  if (value === undefined || value === null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function runIssueText(issue: RunDialogSummary['issues'][number]): string {
  if (issue.message === 'Add at least one node before running.') return '请先添加至少一个节点。';
  const missingTarget = issue.message.match(/^Node '([^']+)' is missing a target\.$/);
  if (missingTarget) return '节点缺少目标能力。';
  const unknownArtifact = issue.message.match(/^Node '([^']+)' references unknown (input|output) artifact '([^']+)'\.$/);
  if (unknownArtifact) return `引用了未知 ${unknownArtifact[2] === 'input' ? '输入' : '输出'}产物 ${unknownArtifact[3]}。`;
  return issue.message;
}

function OrchestrationNodeEditor({
  node,
  dag,
  artifacts,
  capabilities,
  logs,
  onPatch,
  onDelete,
}: {
  node: DagNode;
  dag: Dag;
  artifacts: Record<string, Artifact>;
  capabilities: CapabilityDefinition[];
  logs: TraceLogEvent[];
  onPatch: (patch: Partial<DagNode>, edges?: DagEdge[]) => void;
  onDelete: () => void;
}) {
  if (!isCapabilityNode(node)) {
    return (
      <div className="node-editor">
        <label>
          Node ID
          <input value={node.id} disabled />
        </label>
        <div className="empty-state compact">Internal start node</div>
      </div>
    );
  }
  const invocation = node.payload.invocation;
  const selectedCapability = capabilities.find((capability) => capability.id === invocation.capability_id);
  const pickerCapabilities = visibleCapabilitiesForPicker(capabilities);
  const selectableCapabilities = selectedCapability && !pickerCapabilities.some((capability) => capability.id === selectedCapability.id)
    ? [selectedCapability, ...pickerCapabilities]
    : pickerCapabilities;
  const dependsOn = dag.edges.filter((edge) => edge.target === node.id).map((edge) => edge.source);
  const agentCapabilities = capabilities.filter((capability) => capability.kind === 'agent' && capability.enabled);
  const boundary = invocation.boundary ?? {
    allowed_paths: ['.'],
  };
  const artifactItems = Object.values(artifacts).sort(compareArtifactsByPath);
  const patchInvocation = (patch: Partial<typeof invocation>) =>
    onPatch({ payload: { type: 'capability', invocation: { ...invocation, ...patch } } });
  const patchArtifactList = (field: 'inputs' | 'outputs', artifactId: string, checked: boolean) => {
    const current = node[field] ?? [];
    const next = checked
      ? [...current.filter((id) => id !== artifactId), artifactId].sort()
      : current.filter((id) => id !== artifactId);
    if (field === 'inputs' && checked) {
      const token = artifactPathExpr(artifactId);
      const allowedPaths = boundary.allowed_paths ?? [];
      onPatch({
        [field]: next,
        payload: {
          type: 'capability',
          invocation: {
            ...invocation,
            boundary: {
              ...boundary,
              allowed_paths: addUniqueBoundaryValue(allowedPaths, token),
            },
          },
        },
      });
      return;
    }
    onPatch({ [field]: next });
  };
  const setPathArgumentFromArtifact = (artifactId: string) => {
    patchInvocation({
      arguments: {
        ...(invocation.arguments ?? {}),
        path: artifactPathExpr(artifactId),
      },
    });
  };
  const addAllowedPathFromArtifact = (artifactId: string) => {
    const token = artifactPathExpr(artifactId);
    patchInvocation({
      boundary: {
        ...boundary,
        allowed_paths: addUniqueBoundaryValue(boundary.allowed_paths ?? [], token),
      },
    });
  };
  return (
    <div className="node-editor">
      <label>
        Node ID
        <input
          value={node.id}
          onChange={(event) => onPatch({ id: event.target.value })}
        />
      </label>
      <label>
        Capability
        <select
          value={invocation.capability_id}
          onChange={(event) => {
            const capability = capabilities.find((item) => item.id === event.target.value);
            patchInvocation({
              capability_id: event.target.value,
              kind: capability?.kind ?? invocation.kind,
              arguments: resetSchemaArguments(
                invocation.arguments ?? {},
                capability?.parameters,
                selectedCapability?.parameters,
              ),
              risk: capabilityRisk(capability),
            });
          }}
        >
          <option value="">Select capability...</option>
          {selectableCapabilities.map((capability) => (
            <option key={capability.id} value={capability.id}>
              {capabilityDisplayName(capability)}
            </option>
          ))}
        </select>
      </label>
      <div className="two-col">
        <label>
          Type
          <input value={selectedCapability?.kind ?? invocation.kind ?? 'tool'} disabled />
        </label>
        <label>
          Risk
          <select
            value={invocation.risk ?? capabilityRisk(selectedCapability)}
            onChange={(event) => patchInvocation({ risk: event.target.value as RiskLevel })}
          >
            {riskLevels.map((risk) => (
              <option key={risk} value={risk}>{risk}</option>
            ))}
          </select>
        </label>
      </div>
      {selectedCapability?.kind === 'agent' && agentCapabilities.length === 0 ? (
        <div className="empty-state compact">No agent capabilities are enabled.</div>
      ) : null}
      <ArgumentForm
        value={invocation.arguments ?? {}}
        parameters={selectedCapability?.parameters}
        onChange={(argumentsValue) => patchInvocation({ arguments: argumentsValue })}
      />
      <ArtifactBindingEditor
        artifacts={artifactItems}
        inputs={node.inputs ?? []}
        outputs={node.outputs ?? []}
        onToggle={patchArtifactList}
        onUseAsPath={setPathArgumentFromArtifact}
        onAllowPath={addAllowedPathFromArtifact}
      />
      <label>
        Depends On
        <input
          value={dependsOn.join(', ')}
          onChange={(event) => {
            const sources = splitCsv(event.target.value).filter((source) => source !== node.id);
            const nextEdges = [
              ...dag.edges.filter((edge) => edge.target !== node.id),
              ...sources.map((source) => ({ source, target: node.id, reason: 'User dependency.' })),
            ];
            onPatch({}, nextEdges);
          }}
        />
      </label>
      <details className="node-policy-details">
        <summary>Execution Policy</summary>
        <label>
          Allowed Paths
          <BoundaryValueEditor
            values={boundary.allowed_paths ?? []}
            onChange={(allowedPaths) => patchInvocation({ boundary: { ...boundary, allowed_paths: allowedPaths } })}
          />
        </label>
      </details>
      <button className="secondary-button danger-button" onClick={() => onDelete()} type="button">
        <Trash2 size={16} />
        Delete Node
      </button>
      <NodeExecutionLog logs={logs} />
    </div>
  );
}

function ArtifactBindingEditor({
  artifacts,
  inputs,
  outputs,
  onToggle,
  onUseAsPath,
  onAllowPath,
}: {
  artifacts: Artifact[];
  inputs: string[];
  outputs: string[];
  onToggle: (field: 'inputs' | 'outputs', artifactId: string, checked: boolean) => void;
  onUseAsPath: (artifactId: string) => void;
  onAllowPath: (artifactId: string) => void;
}) {
  return (
    <details className="node-policy-details" open>
      <summary>Files & Artifacts</summary>
      {artifacts.length ? (
        <div className="artifact-binding-list">
          {artifacts.map((artifact) => {
            const uploadedFile = isUploadedFileArtifact(artifact);
            return (
              <div className={uploadedFile ? 'artifact-binding-row file-binding-row' : 'artifact-binding-row'} key={artifact.id}>
                <div className="artifact-binding-label">
                  <strong>{uploadedFile ? artifactDisplayName(artifact) : artifact.id}</strong>
                  <span>{artifactDisplayPath(artifact)}</span>
                </div>
                <label className="checkbox-line">
                  <input
                    type="checkbox"
                    checked={inputs.includes(artifact.id)}
                    onChange={(event) => onToggle('inputs', artifact.id, event.target.checked)}
                  />
                  Input
                </label>
                {uploadedFile ? null : (
                  <label className="checkbox-line">
                    <input
                      type="checkbox"
                      checked={outputs.includes(artifact.id)}
                      onChange={(event) => onToggle('outputs', artifact.id, event.target.checked)}
                    />
                    Output
                  </label>
                )}
                <div className="artifact-binding-actions">
                  <button className="secondary-button compact-button" onClick={() => onUseAsPath(artifact.id)} type="button">
                    Set path
                  </button>
                  <button className="secondary-button compact-button" onClick={() => onAllowPath(artifact.id)} type="button">
                    Allow
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="empty-state compact">Upload files or add an advanced artifact first.</div>
      )}
    </details>
  );
}

function ArgumentForm({
  value,
  parameters,
  onChange,
}: {
  value: Record<string, unknown>;
  parameters?: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}) {
  const fields = buildSchemaArgumentFields(value, parameters);
  const updateKey = (oldKey: string, nextKey: string) => {
    const cleanKey = nextKey.trim();
    if (!cleanKey || (cleanKey !== oldKey && Object.prototype.hasOwnProperty.call(value, cleanKey))) return;
    const next: Record<string, unknown> = {};
    for (const [key, itemValue] of Object.entries(value)) {
      next[key === oldKey ? cleanKey : key] = itemValue;
    }
    onChange(next);
  };
  const updateValue = (key: string, rawValue: string, type: ArgumentValueType) => {
    onChange({
      ...value,
      [key]: parseArgumentValue(rawValue, type, value[key]),
    });
  };
  const updateType = (key: string, nextType: ArgumentValueType) => {
    onChange({
      ...value,
      [key]: coerceArgumentValue(value[key], nextType),
    });
  };
  const addField = () => {
    const existing = ensureSchemaArguments(value, parameters);
    let index = Object.keys(existing).length + 1;
    let key = `arg_${index}`;
    while (Object.prototype.hasOwnProperty.call(existing, key)) {
      index += 1;
      key = `arg_${index}`;
    }
    onChange({ ...existing, [key]: '' });
  };
  const removeField = (key: string) => {
    const next = { ...value };
    delete next[key];
    onChange(next);
  };
  return (
    <section className="argument-form">
      <div className="argument-form-head">
        <span>参数</span>
        <button className="secondary-button compact-button" onClick={addField} type="button">
          <Plus size={14} />
          添加参数
        </button>
      </div>
      {fields.length ? fields.map((field) => {
        const { key, value: itemValue, valueType: type } = field;
        return (
          <div className="argument-row" key={`argument-row-${key}`}>
            <div className="argument-key-wrap">
              <input
                className="argument-key"
                value={key}
                disabled={field.fixed}
                onChange={(event) => updateKey(key, event.target.value)}
                aria-label="参数名"
              />
              {field.fixed ? (
                <span className="argument-meta" title={field.description}>
                  {field.required ? '必填' : '可选'}
                </span>
              ) : null}
            </div>
            <select
              className="argument-type"
              value={type}
              disabled={field.fixed}
              onChange={(event) => updateType(key, event.target.value as ArgumentValueType)}
              aria-label="参数类型"
            >
              <option value="string">string</option>
              <option value="number">number</option>
              <option value="boolean">boolean</option>
              <option value="json">json</option>
            </select>
            {type === 'boolean' ? (
              <select
                className="argument-value"
                value={String(Boolean(itemValue))}
                onChange={(event) => updateValue(key, event.target.value, type)}
                aria-label="参数值"
              >
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            ) : (
              <input
                className="argument-value"
                value={formatArgumentValue(itemValue)}
                onChange={(event) => updateValue(key, event.target.value, type)}
                aria-label="参数值"
              />
            )}
            <button
              className="icon-button"
              onClick={() => removeField(key)}
              disabled={field.fixed}
              title={field.fixed ? 'Schema 参数不可删除' : '删除参数'}
              type="button"
            >
              <Trash2 size={15} />
            </button>
          </div>
        );
      }) : (
        <div className="empty-state compact">暂无参数，添加一个字段。</div>
      )}
    </section>
  );
}

function CapabilityDirectory({
  capabilities,
  skills,
  mcpServers,
  pythonTools,
  activeTab,
  creationIntent,
  query,
  selectedCapabilityId,
  selectedMcpName,
  selectedMcpToolId,
  selectedSkillDetail,
  selectedSkillFileDetail,
  selectedSkillName,
  skillImport,
  skillMessage,
  onActiveTabChange,
  onCreationIntentChange,
  onInstallSkillDraft,
  onRemoveManagedSkill,
  onSelectedCapabilityIdChange,
  onSelectedMcpNameChange,
  onSelectedSkillNameChange,
  onSkillImportChange,
  onUploadSkillFile,
  onRefresh,
}: {
  capabilities: CapabilityDefinition[];
  skills: SkillSummary[];
  mcpServers: MCPServer[];
  pythonTools: PythonToolEntry[];
  activeTab: ToolDirectoryTab;
  creationIntent: ToolDirectoryTab | null;
  query: string;
  selectedCapabilityId: string;
  selectedMcpName: string;
  selectedMcpToolId: string;
  selectedSkillDetail: SkillDetail | null;
  selectedSkillFileDetail: SkillFileDetail | null;
  selectedSkillName: string;
  skillImport: { name: string; description: string; category: string; content: string };
  skillMessage: string;
  onActiveTabChange: (tab: ToolDirectoryTab) => void;
  onCreationIntentChange: (tab: ToolDirectoryTab | null) => void;
  onInstallSkillDraft: () => void;
  onRemoveManagedSkill: () => void;
  onSelectedCapabilityIdChange: (id: string) => void;
  onSelectedMcpNameChange: (name: string) => void;
  onSelectedSkillNameChange: (name: string) => void;
  onSkillImportChange: React.Dispatch<React.SetStateAction<{ name: string; description: string; category: string; content: string }>>;
  onUploadSkillFile: (file: File | undefined) => void;
  onRefresh: () => Promise<void>;
}) {
  const [pythonToolMode, setPythonToolMode] = useState<'path' | 'managed'>('path');
  const [pythonToolDraft, setPythonToolDraft] = useState<PythonToolConfig>(defaultPythonToolConfig);
  const [pythonToolNamesText, setPythonToolNamesText] = useState('');
  const [pythonToolNamesEdited, setPythonToolNamesEdited] = useState(false);
  const [pythonToolFile, setPythonToolFile] = useState<File | null>(null);
  const [pythonToolMessage, setPythonToolMessage] = useState('');
  const pythonToolDiscoveryRef = useRef<PythonToolDiscoveryState>({
    requestId: 0,
    sourceKey: '',
    namesEditedAt: 0,
  });
  const [argumentsText, setArgumentsText] = useState('{"text":"hello"}');
  const [result, setResult] = useState<CapabilityResult | null>(null);
  const [message, setMessage] = useState('');
  const [mcpDraft, setMcpDraft] = useState<{ name: string } & MCPServerConfig>(defaultMcpConfig);
  const [mcpArgsText, setMcpArgsText] = useState('');
  const [mcpEnvText, setMcpEnvText] = useState('');
  const [mcpHeadersText, setMcpHeadersText] = useState('');
  const [mcpMessage, setMcpMessage] = useState('');
  const normalizedQuery = normalizeSearchQuery(query);
  const toolTree = buildToolManagementTree(capabilities, pythonTools, normalizedQuery);
  const toolRows = [
    ...toolTree.builtin.items.map((item) => item.capability),
    ...toolTree.pythonSources.flatMap((source) => source.items.map((item) => item.capability)),
    ...toolTree.manual.items.map((item) => item.capability),
  ];
  const selectedTool = toolRows.find((capability) => capability.id === selectedCapabilityId) ?? toolRows[0];
  const selectedEditable = Boolean(selectedTool && isEditableToolCapability(selectedTool));
  const selectedPythonToolSource = selectedTool
    ? pythonTools.find((source) => source.capabilities.includes(selectedTool.id)) ?? null
    : null;
  const visibleSkills = skills.filter((skill) => matchesSkillQuery(skill, normalizedQuery));
  const selectedSkill = skills.find((skill) => skillLookupName(skill) === selectedSkillName) ?? visibleSkills[0] ?? skills[0];
  const selectedMcp = mcpServers.find((server) => server.name === selectedMcpName) ?? mcpServers[0];
  const selectedMcpTool = selectedMcp?.tools.find((tool) => tool.id === selectedMcpToolId) ?? null;

  useEffect(() => {
    if (!selectedMcp) {
      setMcpDraft(defaultMcpConfig);
      setMcpArgsText('');
      setMcpEnvText('');
      setMcpHeadersText('');
      return;
    }
    setMcpDraft({
      ...defaultMcpConfig,
      name: selectedMcp.name,
      ...selectedMcp.config,
      transport: selectedMcp.config.transport ?? 'stdio',
    });
    setMcpArgsText((selectedMcp.config.args ?? []).join('\n'));
    setMcpEnvText(formatEnvText(selectedMcp.config.env ?? {}));
    setMcpHeadersText(formatEnvText(selectedMcp.config.headers ?? {}));
  }, [selectedMcp]);

  useEffect(() => {
    if (creationIntent !== 'mcp') return;
    setMcpDraft(defaultMcpConfig);
    setMcpArgsText('');
    setMcpEnvText('');
    setMcpHeadersText('');
  }, [creationIntent]);

  useEffect(() => {
    if (creationIntent !== 'tools') return;
    setPythonToolMode('path');
    setPythonToolDraft(defaultPythonToolConfig);
    setPythonToolNamesText('');
    setPythonToolNamesEdited(false);
    setPythonToolFile(null);
    setPythonToolMessage('');
    pythonToolDiscoveryRef.current = { requestId: 0, sourceKey: '', namesEditedAt: 0 };
  }, [creationIntent]);

  const runTest = async () => {
    if (!selectedTool) return;
    const parsed = parseJsonObject(argumentsText);
    if (!parsed) {
      setMessage('Test arguments must be a JSON object.');
      return;
    }
    setMessage(`Testing ${selectedTool.id}...`);
    try {
      const nextResult = await testCapability(selectedTool.id, parsed);
      setResult(nextResult);
      setMessage(`Test ${nextResult.status}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const toggleCapability = async (enabled: boolean) => {
    if (!selectedTool) return;
    setMessage(enabled ? 'Enabling capability...' : 'Disabling capability...');
    try {
      await setCapabilityEnabled(selectedTool.id, enabled);
      await onRefresh();
      setMessage(`${enabled ? 'Enabled' : 'Disabled'} ${selectedTool.id}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const removeCapability = async () => {
    if (!selectedTool || !isEditableToolCapability(selectedTool)) return;
    setMessage('Deleting tool...');
    try {
      await deleteCapability(selectedTool.id);
      onSelectedCapabilityIdChange('');
      await onRefresh();
      setMessage(`Deleted ${selectedTool.id}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const discoverPythonToolDraftNames = async (file?: File, options: { force?: boolean } = {}) => {
    if (!options.force && pythonToolNamesEdited && pythonToolNamesText.trim()) return;
    const path = pythonToolDraft.path?.trim() ?? '';
    if (!file && !path) return;
    const sourceKey = file
      ? pythonToolDiscoverySourceKey('managed', `${file.name}:${file.size}:${file.lastModified}`)
      : pythonToolDiscoverySourceKey('path', path);
    const request = {
      requestId: pythonToolDiscoveryRef.current.requestId + 1,
      sourceKey,
      namesEditedAtStart: pythonToolDiscoveryRef.current.namesEditedAt,
    };
    pythonToolDiscoveryRef.current = {
      ...pythonToolDiscoveryRef.current,
      requestId: request.requestId,
      sourceKey,
    };
    setPythonToolMessage('Discovering Python tool functions...');
    try {
      const names = file
        ? await discoverPythonToolNames({ source: 'managed', file })
        : await discoverPythonToolNames({ source: 'path', path });
      if (!shouldApplyPythonToolDiscoveryResult(pythonToolDiscoveryRef.current, request)) {
        return;
      }
      if (!names.length) {
        setPythonToolMessage('No @dagent.tool functions found.');
        return;
      }
      setPythonToolNamesText(names.join(', '));
      setPythonToolNamesEdited(false);
      setPythonToolMessage(`Found ${names.join(', ')}.`);
    } catch (exc) {
      setPythonToolMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const pythonToolPayloadFromDraft = (): PythonToolConfig | null => {
    const names = pythonToolNamesFromText(pythonToolNamesText);
    if (!pythonToolDraft.id.trim()) {
      setPythonToolMessage('ID is required.');
      return null;
    }
    if (!names.length) {
      setPythonToolMessage('Function names are required.');
      return null;
    }
    if (pythonToolMode === 'path' && !pythonToolDraft.path?.trim()) {
      setPythonToolMessage('Script path is required.');
      return null;
    }
    return {
      ...pythonToolDraft,
      source: pythonToolMode,
      path: pythonToolMode === 'path' ? pythonToolDraft.path : null,
      module: null,
      names,
    };
  };

  const validatePythonToolDraft = async () => {
    if (pythonToolMode !== 'path') {
      setPythonToolMessage('Uploaded files are checked when they are saved.');
      return;
    }
    const payload = pythonToolPayloadFromDraft();
    if (!payload) return;
    setPythonToolMessage('Checking Python tool...');
    try {
      const result = await validatePythonTool(payload);
      if (result.status === 'error') {
        setPythonToolMessage(result.error ?? 'Python tool check failed.');
      } else {
        setPythonToolMessage(`Loaded ${result.capabilities.join(', ')}.`);
      }
    } catch (exc) {
      setPythonToolMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const savePythonTool = async () => {
    const payload = pythonToolPayloadFromDraft();
    if (!payload) return;
    setPythonToolMessage('Saving Python tool...');
    try {
      const result = pythonToolMode === 'managed'
        ? (() => {
            if (!pythonToolFile) throw new Error('Python file is required.');
            return uploadPythonTool(pythonToolFile, {
              id: payload.id,
              names: payload.names,
              enabled: payload.enabled,
            });
          })()
        : createPythonTool(payload);
      const saved = await result;
      await onRefresh();
      if (saved.status === 'error') {
        setPythonToolMessage(saved.error ?? `Failed to load ${saved.id}.`);
        return;
      }
      onSelectedCapabilityIdChange(saved.capabilities[0] ?? '');
      onCreationIntentChange(null);
      setPythonToolMessage(`${saved.id} saved.`);
    } catch (exc) {
      setPythonToolMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const togglePythonToolSource = async (source: PythonToolEntry, enabled: boolean) => {
    setMessage(enabled ? 'Enabling Python tool source...' : 'Disabling Python tool source...');
    try {
      const updated = await updatePythonTool(source.id, {
        id: source.id,
        source: source.source,
        path: source.path,
        module: source.module,
        names: source.names,
        enabled,
      });
      await onRefresh();
      if (updated.status === 'error') {
        setMessage(updated.error ?? `Failed to load ${updated.id}.`);
        return;
      }
      setMessage(`${updated.id} ${enabled ? 'enabled' : 'disabled'}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const removePythonToolSource = async (source: PythonToolEntry) => {
    setMessage('Deleting Python tool source...');
    try {
      await deletePythonTool(source.id);
      if (selectedTool && source.capabilities.includes(selectedTool.id)) {
        onSelectedCapabilityIdChange('');
      }
      await onRefresh();
      setMessage(`Deleted ${source.id}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const reloadPythonToolSources = async () => {
    setMessage('Reloading Python tools...');
    try {
      await reloadPythonTools();
      await onRefresh();
      setMessage('Python tools reloaded.');
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const saveMcpServer = async () => {
    setMcpMessage('Saving MCP server...');
    try {
      const transport = mcpDraft.transport ?? 'stdio';
      const payload = {
        ...mcpDraft,
        transport,
        args: transport === 'stdio' ? linesFromText(mcpArgsText) : [],
        env: transport === 'stdio' ? parseEnvText(mcpEnvText) : {},
        headers: transport === 'http' ? parseEnvText(mcpHeadersText) : {},
      };
      const editingExistingUserServer = selectedMcp && isEditableMcpSource(selectedMcp.source) && selectedMcp.name === payload.name;
      if (editingExistingUserServer) {
        await updateMcpServer(selectedMcp.name, payload);
      } else {
        await createMcpServer(payload);
      }
      await onRefresh();
      onSelectedMcpNameChange(payload.name);
      onCreationIntentChange(null);
      setMcpMessage(`Saved ${payload.name}.`);
    } catch (exc) {
      setMcpMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const removeMcpServer = async () => {
    if (!selectedMcp || !isEditableMcpSource(selectedMcp.source)) return;
    setMcpMessage('Deleting MCP server...');
    try {
      await deleteMcpServer(selectedMcp.name);
      onSelectedMcpNameChange('');
      await onRefresh();
      setMcpMessage(`Deleted ${selectedMcp.name}.`);
    } catch (exc) {
      setMcpMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const reloadMcp = async () => {
    setMcpMessage('Reloading MCP servers...');
    try {
      await reloadMcpServers();
      await onRefresh();
      setMcpMessage('MCP servers reloaded.');
    } catch (exc) {
      setMcpMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };
  const renderMcpConnectionFields = () => {
    const transport = mcpDraft.transport ?? 'stdio';
    const setTransport = (nextTransport: MCPServerConfig['transport']) =>
      setMcpDraft((current) => ({ ...current, transport: nextTransport }));
    return (
      <>
        <div className="mcp-transport-field">
          <span>传输</span>
          <div className="mcp-transport-toggle" role="group" aria-label="MCP transport">
            <button
              type="button"
              className={transport === 'stdio' ? 'active' : ''}
              aria-pressed={transport === 'stdio'}
              onClick={() => setTransport('stdio')}
            >
              本地 stdio
            </button>
            <button
              type="button"
              className={transport === 'http' ? 'active' : ''}
              aria-pressed={transport === 'http'}
              onClick={() => setTransport('http')}
            >
              HTTP
            </button>
          </div>
        </div>
        {transport === 'http' ? (
          <>
            <label>URL<input value={mcpDraft.url ?? ''} onChange={(event) => setMcpDraft((current) => ({ ...current, url: event.target.value }))} /></label>
            <label>Headers<textarea value={mcpHeadersText} onChange={(event) => setMcpHeadersText(event.target.value)} placeholder="KEY=value" /></label>
          </>
        ) : (
          <>
            <label>命令<input value={mcpDraft.command ?? ''} onChange={(event) => setMcpDraft((current) => ({ ...current, command: event.target.value }))} /></label>
            <label>Args<textarea value={mcpArgsText} onChange={(event) => setMcpArgsText(event.target.value)} placeholder="每行一个参数" /></label>
            <label>环境变量<textarea value={mcpEnvText} onChange={(event) => setMcpEnvText(event.target.value)} placeholder="KEY=value" /></label>
          </>
        )}
      </>
    );
  };
  const createDialogTitle = creationIntent === 'tools'
    ? '导入 Python 工具'
    : creationIntent === 'skills'
      ? '导入技能'
      : '新建 MCP 服务';
  const renderPythonToolSources = () => (
    <section>
      <div className="section-heading-row">
        <h3>Python 工具源</h3>
        <button className="secondary-button compact-button" onClick={() => void reloadPythonToolSources()} type="button">
          <RefreshCw size={13} />
          重载
        </button>
      </div>
      {pythonTools.length ? (
        <div className="python-tool-source-list">
          {pythonTools.map((source) => (
            <div className="python-tool-source-row" key={source.id} data-status={source.status}>
              <div>
                <strong>{source.id}</strong>
                <span>{pythonToolSourcePath(source)}</span>
                {source.error ? <em>{source.error}</em> : null}
              </div>
              <div>
                <span className="status-badge" data-status={source.status === 'loaded' ? 'completed' : source.status === 'error' ? 'failed' : 'queued'}>
                  {pythonToolStatusLabel(source.status)}
                </span>
                <button className="secondary-button compact-button" onClick={() => void togglePythonToolSource(source, !source.enabled)} type="button">
                  {source.enabled ? '停用' : '启用'}
                </button>
                <button className="secondary-button danger-button compact-button" onClick={() => void removePythonToolSource(source)} type="button">
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className="form-message">还没有配置 Python 工具源。</p>
      )}
    </section>
  );

  return (
    <section className={`design-tools-workspace ${activeTab === 'skills' ? 'skills-mode' : ''}`}>
      {creationIntent ? (
        <div className="capability-create-backdrop" onMouseDown={() => onCreationIntentChange(null)}>
          <section
            className="capability-create-dialog"
            role="dialog"
            aria-modal="true"
            aria-label={createDialogTitle}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="capability-create-head">
              <div>
                <span>能力管理</span>
                <h2>{createDialogTitle}</h2>
              </div>
              <button onClick={() => onCreationIntentChange(null)} title="关闭" type="button">
                <X size={15} />
              </button>
            </div>
            {creationIntent === 'tools' ? (
              <section className="tool-create-drawer">
                <div className="mcp-transport-field">
                  <span>来源</span>
                  <div className="mcp-transport-toggle" role="group" aria-label="Python tool source">
                    <button
                      type="button"
                      className={pythonToolMode === 'path' ? 'active' : ''}
                      aria-pressed={pythonToolMode === 'path'}
                      onClick={() => setPythonToolMode('path')}
                    >
                      脚本路径
                    </button>
                    <button
                      type="button"
                      className={pythonToolMode === 'managed' ? 'active' : ''}
                      aria-pressed={pythonToolMode === 'managed'}
                      onClick={() => setPythonToolMode('managed')}
                    >
                      上传文件
                    </button>
                  </div>
                </div>
                <div className="compact-form-grid">
                  <label>ID<input value={pythonToolDraft.id} onChange={(event) => setPythonToolDraft((current) => ({ ...current, id: event.target.value }))} /></label>
                  {pythonToolMode === 'path' ? (
                    <label>
                      脚本路径
                      <input
                        value={pythonToolDraft.path ?? ''}
                        onBlur={() => void discoverPythonToolDraftNames()}
                        onChange={(event) => {
                          const nextPath = event.target.value;
                          pythonToolDiscoveryRef.current = {
                            ...pythonToolDiscoveryRef.current,
                            sourceKey: pythonToolDiscoverySourceKey('path', nextPath.trim()),
                          };
                          setPythonToolDraft((current) => ({ ...current, path: nextPath }));
                        }}
                        placeholder="/Users/olivia/tools/local_tools.py"
                      />
                    </label>
                  ) : (
                    <label className="skill-package-upload">
                      <Upload size={15} />
                      <span>
                        <strong>{pythonToolFile?.name ?? '选择 .py 文件'}</strong>
                        <em>文件会保存到 ~/.dagent/python-tools/</em>
                      </span>
                      <input
                        type="file"
                        accept=".py,text/x-python,text/plain"
                        onChange={(event) => {
                          const selectedFile = event.target.files?.[0] ?? null;
                          setPythonToolFile(selectedFile);
                          if (selectedFile) {
                            void discoverPythonToolDraftNames(selectedFile, { force: true });
                          }
                          event.currentTarget.value = '';
                        }}
                      />
                    </label>
                  )}
                  <label>
                    函数名
                    <textarea
                      value={pythonToolNamesText}
                      onChange={(event) => {
                        setPythonToolNamesText(event.target.value);
                        setPythonToolNamesEdited(true);
                        pythonToolDiscoveryRef.current = {
                          ...pythonToolDiscoveryRef.current,
                          namesEditedAt: pythonToolDiscoveryRef.current.namesEditedAt + 1,
                        };
                      }}
                      placeholder="search_docs, summarize_page"
                    />
                  </label>
                  <label className="inline-checkbox">
                    <input
                      type="checkbox"
                      checked={pythonToolDraft.enabled}
                      onChange={(event) => setPythonToolDraft((current) => ({ ...current, enabled: event.target.checked }))}
                    />
                    启用
                  </label>
                </div>
                {pythonToolMessage ? <p className="form-message">{pythonToolMessage}</p> : null}
                <div className="capability-create-actions">
                  <button className="secondary-button compact-button" onClick={() => onCreationIntentChange(null)} type="button">
                    取消
                  </button>
                  <button className="secondary-button compact-button" onClick={() => void validatePythonToolDraft()} disabled={pythonToolMode !== 'path'} type="button">
                    <Check size={14} />
                    检测
                  </button>
                  <button className="primary-button compact-button" onClick={() => void savePythonTool()} type="button">
                    <Upload size={14} />
                    保存
                  </button>
                </div>
              </section>
            ) : null}
            {creationIntent === 'skills' ? (
              <section className="skill-import-panel">
                <label className="skill-package-upload">
                  <Upload size={15} />
                  <span>
                    <strong>上传技能包</strong>
                    <em>.zip 会直接安装，SKILL.md 会填入下方内容</em>
                  </span>
                  <input
                    type="file"
                    accept=".md,text/markdown,text/plain,.zip,application/zip"
                    onChange={(event) => {
                      onUploadSkillFile(event.target.files?.[0]);
                      event.currentTarget.value = '';
                    }}
                  />
                </label>
                <div className="compact-form-grid">
                  <label>名称<input value={skillImport.name} onChange={(event) => onSkillImportChange((current) => ({ ...current, name: event.target.value }))} /></label>
                  <label>分类<input value={skillImport.category} onChange={(event) => onSkillImportChange((current) => ({ ...current, category: event.target.value }))} /></label>
                  <label>描述<textarea value={skillImport.description} onChange={(event) => onSkillImportChange((current) => ({ ...current, description: event.target.value }))} /></label>
                  <label>SKILL.md<textarea value={skillImport.content} onChange={(event) => onSkillImportChange((current) => ({ ...current, content: event.target.value }))} /></label>
                </div>
                {skillMessage ? <p className="form-message">{skillMessage}</p> : null}
                <div className="capability-create-actions">
                  <button className="secondary-button compact-button" onClick={() => onCreationIntentChange(null)} type="button">
                    取消
                  </button>
                  <button className="primary-button compact-button" onClick={onInstallSkillDraft} type="button">
                    <Upload size={14} />
                    安装
                  </button>
                </div>
              </section>
            ) : null}
            {creationIntent === 'mcp' ? (
              <section className="mcp-config-form">
                <label>名称<input value={mcpDraft.name} onChange={(event) => setMcpDraft((current) => ({ ...current, name: event.target.value }))} /></label>
                {renderMcpConnectionFields()}
                {mcpMessage ? <p className="form-message">{mcpMessage}</p> : null}
                <div className="capability-create-actions">
                  <button className="secondary-button compact-button" onClick={() => onCreationIntentChange(null)} type="button">
                    取消
                  </button>
                  <button className="primary-button compact-button" onClick={saveMcpServer} type="button">
                    <Save size={13} />
                    保存配置
                  </button>
                </div>
              </section>
            ) : null}
          </section>
        </div>
      ) : null}
      <aside className="tools-detail-panel">
        {activeTab === 'tools' ? (
          <div className="capability-detail-pane">
            <div className="agent-editor-toolbar">
              <div className="agent-editor-icon">
                <Wrench size={15} />
              </div>
              <div>
                <strong>{selectedTool ? capabilityDisplayName(selectedTool) : '工具'}</strong>
                <span>{selectedTool ? `${selectedTool.kind} · ${capabilityStatusLabel(selectedTool)}` : 'tool capability'}</span>
              </div>
              <div>
                <button
                  className="secondary-button compact-button"
                  onClick={() => {
                    if (selectedPythonToolSource) {
                      void togglePythonToolSource(selectedPythonToolSource, !selectedPythonToolSource.enabled);
                    } else if (selectedTool) {
                      void toggleCapability(!selectedTool.enabled);
                    }
                  }}
                  disabled={!selectedTool || (!selectedEditable && !selectedPythonToolSource)}
                  type="button"
                >
                  {selectedPythonToolSource ? (selectedPythonToolSource.enabled ? '停用' : '启用') : selectedTool?.enabled ? '停用' : '启用'}
                </button>
                <button
                  className="secondary-button danger-button compact-button"
                  onClick={() => {
                    if (selectedPythonToolSource) {
                      void removePythonToolSource(selectedPythonToolSource);
                    } else {
                      void removeCapability();
                    }
                  }}
                  disabled={!selectedTool || (!selectedEditable && !selectedPythonToolSource)}
                  type="button"
                >
                  <Trash2 size={13} />
                  删除
                </button>
              </div>
            </div>
            {selectedTool ? (
              <div className="tools-detail-scroll">
                <div className="tool-detail-surface">
                  <p className="tool-detail-summary">{selectedTool.description || selectedTool.id}</p>
                  <div className="tool-info-table">
                    <div><span>类型</span><strong>{selectedTool.kind}</strong></div>
                    <div><span>风险</span><strong><i className={`risk-chip risk-${selectedTool.policy.risk}`}>{selectedTool.policy.risk}</i></strong></div>
                    <div><span>执行</span><strong>{toolExecutionLabel(selectedTool)}</strong></div>
                    <div><span>状态</span><strong>{capabilityStatusLabel(selectedTool)}</strong></div>
                  </div>
                  {selectedPythonToolSource ? (
                    <div className="tool-info-table">
                      <div><span>来源</span><strong>Python</strong></div>
                      <div><span>源 ID</span><strong>{selectedPythonToolSource.id}</strong></div>
                      <div><span>模式</span><strong>{pythonToolSourceLabel(selectedPythonToolSource.source)}</strong></div>
                      <div><span>源状态</span><strong>{pythonToolStatusLabel(selectedPythonToolSource.status)}</strong></div>
                    </div>
                  ) : null}
                  <section>
                    <h3>参数 Schema</h3>
                    <pre className="tool-schema-block">{JSON.stringify(selectedTool.parameters, null, 2)}</pre>
                  </section>
                  <section>
                    <h3>测试调用</h3>
                    <textarea
                      value={argumentsText}
                      onChange={(event) => setArgumentsText(event.target.value)}
                      placeholder='{ "pattern": "DAG", "path": "." }'
                      spellCheck={false}
                    />
                    <div className="inline-actions">
                      <button className="primary-button compact-button" onClick={runTest} type="button">
                        <Play size={13} />
                        执行测试
                      </button>
                    </div>
                    {message ? <p className="form-message">{message}</p> : null}
                    {result ? <pre className="tool-schema-block">{JSON.stringify(result, null, 2)}</pre> : null}
                  </section>
                  {renderPythonToolSources()}
                </div>
              </div>
            ) : (
              <div className="tools-detail-scroll">
                <div className="tool-detail-surface">
                  <div className="empty-state agent-empty-card">
                    <Wrench size={28} />
                    <strong>没有加载到工具</strong>
                    <p>导入 Python 工具，或从左侧选择一个已有工具查看参数与测试调用。</p>
                  </div>
                  {renderPythonToolSources()}
                </div>
              </div>
            )}
          </div>
        ) : activeTab === 'skills' ? (
          <div className="skill-editor">
            <div className="skill-editor-toolbar">
              <FileText size={15} />
              <span>{selectedSkill ? skillLookupName(selectedSkill) : 'skill'} <em>/</em> <strong>{selectedSkillFileDetail?.file_path ?? 'SKILL.md'}</strong></span>
              <div>
                <button className="secondary-button danger-button compact-button" onClick={onRemoveManagedSkill} disabled={!selectedSkillDetail || !isManagedSkill(selectedSkillDetail.skill)} type="button">
                  <Trash2 size={13} />
                  删除
                </button>
                <button className="primary-button compact-button" type="button" disabled title="后端暂未提供技能文件保存接口">
                  <Save size={13} />
                  保存
                </button>
              </div>
            </div>
            <div className="skill-editor-body">
              <textarea
                value={selectedSkillFileDetail?.content ?? selectedSkillDetail?.content ?? ''}
                readOnly
                spellCheck={false}
              />
            </div>
          </div>
        ) : (
          <div className="capability-detail-pane">
            <div className="agent-editor-toolbar">
              <div className="agent-editor-icon">
                {selectedMcpTool ? <Wrench size={15} /> : <Database size={15} />}
              </div>
              <div>
                <strong>{selectedMcpTool ? capabilityDisplayName(selectedMcpTool) : selectedMcp?.name ?? 'MCP 服务'}</strong>
                <span>{selectedMcpTool && selectedMcp ? `${selectedMcp.name} · MCP 工具` : selectedMcp ? `${selectedMcp.source} · ${selectedMcp.tools.length} tools` : 'MCP server'}</span>
              </div>
              <div>
                {selectedMcpTool ? (
                  <span className="status-badge mcp-status-badge" data-status={selectedMcpTool.enabled ? 'completed' : 'queued'}>
                    {capabilityStatusLabel(selectedMcpTool)}
                  </span>
                ) : selectedMcp ? (
                  <span className="status-badge mcp-status-badge" data-status={selectedMcp.status === 'connected' ? 'completed' : selectedMcp.status === 'error' ? 'failed' : 'running'}>
                    {mcpStatusLabel(selectedMcp.status)}
                  </span>
                ) : null}
                {selectedMcpTool ? null : (
                  <button className="secondary-button danger-button compact-button" onClick={removeMcpServer} disabled={!selectedMcp || !isEditableMcpSource(selectedMcp.source)} type="button">
                    <Trash2 size={13} />
                    删除
                  </button>
                )}
              </div>
            </div>
            {selectedMcpTool && selectedMcp ? (
              <div className="tools-detail-scroll">
                <div className="mcp-detail-surface">
                  {selectedMcp.error ? <div className="error-banner">{selectedMcp.error}</div> : null}
                  <p className="tool-detail-summary">{selectedMcpTool.description || selectedMcpTool.id}</p>
                  <div className="tool-info-table">
                    <div><span>服务</span><strong>{selectedMcp.name}</strong></div>
                    <div><span>能力 ID</span><strong>{selectedMcpTool.id}</strong></div>
                    <div><span>状态</span><strong>{capabilityStatusLabel(selectedMcpTool)}</strong></div>
                    <div><span>风险</span><strong><i className={`risk-chip risk-${selectedMcpTool.policy.risk}`}>{selectedMcpTool.policy.risk}</i></strong></div>
                  </div>
                  <section>
                    <h3>参数 Schema</h3>
                    <pre className="tool-schema-block">{JSON.stringify(selectedMcpTool.parameters, null, 2)}</pre>
                  </section>
                  <section>
                    <h3>输出 Schema</h3>
                    <pre className="tool-schema-block">{JSON.stringify(selectedMcpTool.output_schema, null, 2)}</pre>
                  </section>
                  <section>
                    <h3>配置</h3>
                    <pre className="tool-schema-block">{JSON.stringify(selectedMcpTool.config, null, 2)}</pre>
                  </section>
                </div>
              </div>
            ) : selectedMcp ? (
              <div className="tools-detail-scroll">
                <div className="mcp-detail-surface">
                  {selectedMcp.error ? <div className="error-banner">{selectedMcp.error}</div> : null}
                  <div className="mcp-config-form">
                    {renderMcpConnectionFields()}
                    <div className="inline-actions">
                      <button className="primary-button compact-button" onClick={saveMcpServer} type="button">
                        <Save size={13} />
                        保存配置
                      </button>
                      <button className="secondary-button compact-button" onClick={() => void reloadMcp()} type="button">
                        <RefreshCw size={13} />
                        重载
                      </button>
                    </div>
                  </div>
                  <section>
                    <h3>发现的工具</h3>
                    <pre className="tool-schema-block">{JSON.stringify(selectedMcp.tools, null, 2)}</pre>
                  </section>
                  {mcpMessage ? <p className="form-message">{mcpMessage}</p> : null}
                </div>
              </div>
            ) : (
              <div className="empty-state agent-empty-card">
                <Database size={28} />
                <strong>暂无 MCP 服务</strong>
                <p>连接一个 MCP 服务，把它发现的工具接入能力库。</p>
                <button className="primary-button compact-button" onClick={() => onCreationIntentChange('mcp')} type="button">
                  <Plus size={14} />
                  新建 MCP 服务
                </button>
              </div>
            )}
          </div>
        )}
      </aside>
    </section>
  );
}

function profileSourceLabel(profile: AgentProfile): string {
  if (profile.source === 'builtin') return '内置配置';
  if (profile.source === 'managed') return '本地配置';
  return '配置目录';
}

function uniqueProfileName(baseName: string, profiles: AgentProfile[]): string {
  const safeBase = cleanProfileNameDraft(baseName) || 'agent';
  const names = new Set(profiles.map((profile) => profile.name));
  let candidate = safeBase;
  let index = 2;
  while (names.has(candidate)) {
    candidate = `${safeBase}_${index}`;
    index += 1;
  }
  return candidate;
}

function cleanProfileNameDraft(value: string): string {
  return cleanWorkspaceKeyDraft(value, { requireLeadingLetter: true });
}

function uniqueAgentPresetName(baseName: string, presets: AgentPreset[]): string {
  const safeBase = cleanWorkspaceKeyDraft(baseName) || 'helper';
  const names = new Set(presets.map((preset) => preset.name));
  let candidate = safeBase;
  let index = 2;
  while (names.has(candidate)) {
    candidate = `${safeBase}_${index}`;
    index += 1;
  }
  return candidate;
}

function emptyAgentPresetDraft(profiles: AgentProfile[], presets: AgentPreset[]): AgentPresetInput {
  return {
    name: uniqueAgentPresetName('helper', presets),
    profile: profiles[0]?.name ?? 'conversation',
    description: '',
    max_steps: 4,
    capabilities: [],
    skills: [],
    agents: [],
    review: 'fast',
  };
}

function agentPresetToInput(preset: AgentPreset): AgentPresetInput {
  return {
    name: preset.name,
    profile: preset.profile,
    description: preset.description,
    max_steps: preset.max_steps,
    capabilities: preset.capabilities ?? [],
    skills: preset.skills ?? [],
    agents: [],
    review: 'fast',
  };
}

function agentPresetDraftEquals(preset: AgentPreset, draft: AgentPresetInput): boolean {
  const normalized = agentPresetToInput(preset);
  return normalized.name === draft.name
    && normalized.profile === draft.profile
    && normalized.description === draft.description
    && normalized.max_steps === draft.max_steps
    && arrayEqual(normalized.capabilities ?? [], draft.capabilities ?? [])
    && arrayEqual(normalized.skills ?? [], draft.skills ?? []);
}

function arrayEqual(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((item, index) => item === right[index]);
}

function toolExecutionLabel(capability: CapabilityDefinition): string {
  if (capability.policy.sandbox_required) return 'sandbox';
  if (capability.policy.network) return 'network';
  return 'local';
}

function capabilityStatusLabel(capability: CapabilityDefinition): string {
  return capability.enabled ? '已启用' : '已停用';
}

function mcpStatusLabel(status: MCPServer['status']): string {
  if (status === 'connected') return '已连接';
  if (status === 'disabled') return '已停用';
  if (status === 'error') return '连接错误';
  return '连接中';
}

function pythonToolNamesFromText(value: string): string[] {
  return value
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function pythonToolSourceLabel(source: PythonToolEntry['source']): string {
  if (source === 'managed') return '上传文件';
  if (source === 'module') return 'Python module';
  return '脚本路径';
}

function pythonToolStatusLabel(status: PythonToolEntry['status']): string {
  if (status === 'loaded') return '已加载';
  if (status === 'disabled') return '已停用';
  return '加载错误';
}

function pythonToolSourcePath(source: PythonToolEntry): string {
  if (source.source === 'module') return source.module ?? '';
  return source.path ?? '';
}

function isEditableMcpSource(source: MCPServer['source']): boolean {
  return source === 'user';
}

function SystemManagementWorkspace({
  activeSub,
  activeModelId,
  creating,
  models,
  onlyOfficeSettings,
  selectedId,
  onCreatingChange,
  onRefresh,
  onSelect,
}: {
  activeSub: SystemManagementSub;
  activeModelId: string;
  creating: boolean;
  models: ModelProvider[];
  onlyOfficeSettings: OnlyOfficeSettings;
  selectedId: string;
  onCreatingChange: (creating: boolean) => void;
  onRefresh: () => Promise<void>;
  onSelect: (id: string) => void;
}) {
  return activeSub === 'models' ? (
    <ModelManagementWorkspace
      activeModelId={activeModelId}
      creating={creating}
      models={models}
      selectedId={selectedId}
      onCreatingChange={onCreatingChange}
      onRefresh={onRefresh}
      onSelect={onSelect}
    />
  ) : (
    <OnlyOfficeSettingsWorkspace
      settings={onlyOfficeSettings}
      onRefresh={onRefresh}
    />
  );
}

function OnlyOfficeSettingsWorkspace({
  settings,
  onRefresh,
}: {
  settings: OnlyOfficeSettings;
  onRefresh: () => Promise<void>;
}) {
  const [draft, setDraft] = useState<OnlyOfficeSettings>(() => normalizeOnlyOfficeDraft(settings));
  const [message, setMessage] = useState('');
  const [saving, setSaving] = useState(false);
  const configured = Boolean(
    draft.enabled
    && (draft.document_server_url ?? '').trim()
    && (draft.public_api_base ?? '').trim(),
  );

  useEffect(() => {
    setDraft(normalizeOnlyOfficeDraft(settings));
    setMessage('');
  }, [settings]);

  const patchDraft = (patch: Partial<OnlyOfficeSettings>) => {
    setDraft((current) => ({ ...current, ...patch }));
  };

  const saveSettings = async () => {
    setSaving(true);
    setMessage('Saving OnlyOffice settings...');
    try {
      const saved = await updateOnlyOfficeSettings(normalizeOnlyOfficeDraft(draft));
      setDraft(normalizeOnlyOfficeDraft(saved));
      await onRefresh();
      setMessage('Saved OnlyOffice settings.');
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSaving(false);
    }
  };

  const refreshSettings = async () => {
    setSaving(true);
    setMessage('Refreshing OnlyOffice settings...');
    try {
      await onRefresh();
      setMessage('Refreshed OnlyOffice settings.');
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="design-models-workspace">
      <section className="model-config-panel">
        <div className="model-editor-toolbar">
          <div className="agent-editor-icon">
            <Settings size={15} />
          </div>
          <div>
            <strong>文档预览配置</strong>
            <span>{draft.enabled ? 'enabled' : 'disabled'}</span>
          </div>
          <div>
            <button className="secondary-button compact-button" onClick={refreshSettings} disabled={saving} type="button">
              <RefreshCw size={13} />
              刷新
            </button>
            <button className="primary-button compact-button" onClick={saveSettings} disabled={saving} type="button">
              <Save size={13} />
              保存
            </button>
          </div>
        </div>

        <div className="model-config-body">
          <div className="model-secret-state" data-configured={configured}>
            {configured ? <Check size={14} /> : <AlertTriangle size={14} />}
            <span>{configured ? 'OnlyOffice 预览已配置' : draft.enabled ? '启用前需要填写服务地址' : 'OnlyOffice 预览未启用'}</span>
          </div>
          <div className="model-config-form onlyoffice-config-form">
            <label className="model-checkbox-row">
              <input
                checked={draft.enabled}
                onChange={(event) => patchDraft({ enabled: event.target.checked })}
                type="checkbox"
              />
              <span>启用 OnlyOffice 预览</span>
            </label>
            <label>
              Document Server URL
              <input
                value={draft.document_server_url ?? ''}
                onChange={(event) => patchDraft({ document_server_url: event.target.value })}
                placeholder="http://192.168.31.219:8089"
              />
            </label>
            <label>
              Public API Base
              <input
                value={draft.public_api_base ?? ''}
                onChange={(event) => patchDraft({ public_api_base: event.target.value })}
                placeholder="http://192.168.31.10:8001"
              />
            </label>
            <label>
              JWT Secret
              <input
                value={draft.jwt_secret ?? ''}
                onChange={(event) => patchDraft({ jwt_secret: event.target.value })}
                placeholder="OnlyOffice JWT secret"
                type="password"
              />
            </label>
            <label>
              Language
              <input
                value={draft.lang}
                onChange={(event) => patchDraft({ lang: event.target.value })}
                placeholder="zh-CN"
              />
            </label>
          </div>
          <div className="agent-path-note">
            <AlertTriangle size={14} />
            <span>Public API Base 需要使用 Document Server 能访问到的后端地址。</span>
          </div>
          {message ? <p className="form-message">{message}</p> : null}
        </div>
      </section>
    </section>
  );
}

function normalizeOnlyOfficeDraft(settings: OnlyOfficeSettings): OnlyOfficeSettings {
  return {
    enabled: Boolean(settings.enabled),
    document_server_url: cleanOnlyOfficeText(settings.document_server_url),
    public_api_base: cleanOnlyOfficeText(settings.public_api_base),
    jwt_secret: cleanOnlyOfficeText(settings.jwt_secret),
    lang: cleanOnlyOfficeText(settings.lang) ?? 'zh',
  };
}

function cleanOnlyOfficeText(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const text = value.trim();
  return text || null;
}

function ModelManagementWorkspace({
  activeModelId,
  creating,
  models,
  selectedId,
  onCreatingChange,
  onSelect,
  onRefresh,
}: {
  activeModelId: string;
  creating: boolean;
  models: ModelProvider[];
  selectedId: string;
  onCreatingChange: (creating: boolean) => void;
  onSelect: (id: string) => void;
  onRefresh: () => Promise<void>;
}) {
  const selected = models.find((model) => model.id === selectedId)
    ?? models.find((model) => model.id === activeModelId)
    ?? models[0]
    ?? null;
  const [draft, setDraft] = useState<ModelProviderInput>(defaultModelDraft);
  const [apiKeyText, setApiKeyText] = useState('');
  const [apiKeyAction, setApiKeyAction] = useState<ModelApiKeyAction>('replace');
  const [reasoningText, setReasoningText] = useState('');
  const [extraRequestArgsText, setExtraRequestArgsText] = useState('{}');
  const [extraBodyText, setExtraBodyText] = useState('{}');
  const [modelAdvancedOpen, setModelAdvancedOpen] = useState(false);
  const [message, setMessage] = useState('');
  const source = creating ? 'user' : selected?.source ?? 'user';
  const isConfigModel = source === 'config';
  const editable = creating || source === 'user';

  useEffect(() => {
    if (creating) return;
    if (!selected) {
      setDraft(defaultModelDraft);
      setApiKeyText('');
      setApiKeyAction('replace');
      setReasoningText('');
      setExtraRequestArgsText('{}');
      setExtraBodyText('{}');
      setModelAdvancedOpen(false);
      return;
    }
    setDraft(modelInputFromProvider(selected));
    setApiKeyText('');
    setApiKeyAction('preserve');
    setReasoningText(formatModelJson(selected.reasoning, true));
    setExtraRequestArgsText(formatModelJson(selected.extra_request_args));
    setExtraBodyText(formatModelJson(selected.extra_body));
    setModelAdvancedOpen(false);
  }, [creating, selected]);

  useEffect(() => {
    if (!creating) return;
    const userModelCount = models.filter((model) => model.source === 'user').length + 1;
    setDraft({
      ...defaultModelDraft,
      id: `user-model-${userModelCount}`,
    });
    setApiKeyText('');
    setApiKeyAction('replace');
    setReasoningText('');
    setExtraRequestArgsText('{}');
    setExtraBodyText('{}');
    setModelAdvancedOpen(false);
    setMessage('');
  }, [creating, models]);

  const cancelCreate = () => {
    onCreatingChange(false);
    setMessage('');
    if (selected) onSelect(selected.id);
  };

  const patchModelValue = (value: string) => {
    setDraft((current) => ({
      ...current,
      model: value,
      name: modelDisplayNameForDraft(current.name, current.model, value),
    }));
  };

  const updateApiKeyText = (value: string) => {
    setApiKeyText(value);
    setApiKeyAction(value.trim() ? 'replace' : creating ? 'replace' : 'preserve');
  };

  const clearSavedApiKey = () => {
    setApiKeyText('');
    setApiKeyAction('clear');
  };

  const savedApiKeyWillRemain = Boolean(selected?.api_key_saved && apiKeyAction !== 'clear');
  const secretConfigured = Boolean(savedApiKeyWillRemain || draft.api_key_env || apiKeyText.trim());

  const saveModel = async () => {
    if (!editable) return;
    const extraRequestArgs = parseJsonObject(extraRequestArgsText);
    const extraBody = parseJsonObject(extraBodyText);
    const reasoning = reasoningText.trim() ? parseJsonObject(reasoningText) : null;
    if (!extraRequestArgs) {
      setMessage('Extra request args must be a JSON object.');
      return;
    }
    if (!extraBody) {
      setMessage('Extra body must be a JSON object.');
      return;
    }
    if (reasoningText.trim() && !reasoning) {
      setMessage('Reasoning must be a JSON object.');
      return;
    }
    const payload: ModelProviderInput = {
      ...draft,
      id: creating ? uniqueModelDraftId(draft.name || draft.model, models) : draft.id.trim(),
      name: (draft.name || draft.model).trim(),
      base_url: draft.base_url.trim(),
      model: draft.model.trim(),
      api_key: apiKeyText.trim() || null,
      api_key_action: creating ? 'replace' : apiKeyAction,
      api_key_env: draft.api_key_env?.trim() || null,
      reasoning,
      extra_request_args: extraRequestArgs,
      extra_body: extraBody,
    };
    if (!payload.id || !payload.name || !payload.base_url || !payload.model) {
      setMessage('Base URL, model, and display name are required.');
      return;
    }
    setMessage(creating ? 'Creating model...' : 'Saving model...');
    try {
      const result = creating
        ? await createModelProvider(payload)
        : await updateModelProvider(payload.id, payload);
      await onRefresh();
      onSelect(result.model.id);
      onCreatingChange(false);
      setApiKeyText('');
      setApiKeyAction('preserve');
      setMessage(`Saved ${result.model.name}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const activateModel = async () => {
    if (!selected || creating || selected.active) return;
    setMessage(`Activating ${selected.name}...`);
    try {
      const result = await activateModelProvider(selected.id);
      await onRefresh();
      onSelect(result.model.id);
      setMessage(`Activated ${result.model.name}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const removeModel = async () => {
    if (!selected || selected.source !== 'user' || creating) return;
    setMessage(`Deleting ${selected.name}...`);
    try {
      const result = await deleteModelProvider(selected.id);
      await onRefresh();
      onSelect(result.active_model_id);
      setMessage(`Deleted ${selected.name}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  return (
    <section className="design-models-workspace">
      <section className="model-config-panel">
        <div className="model-editor-toolbar">
          <div className="agent-editor-icon">
            <SlidersHorizontal size={15} />
          </div>
          <div>
            <strong>{creating ? '新建模型' : selected?.name ?? '模型管理'}</strong>
            <span>{creating ? 'user' : selected ? `${modelSourceLabel(selected.source)} · ${selected.model}` : 'user'}</span>
          </div>
          <div>
            {creating ? (
              <button className="secondary-button compact-button" onClick={cancelCreate} type="button">
                取消
              </button>
            ) : (
              <button className="secondary-button compact-button" onClick={activateModel} disabled={!selected || selected.active} type="button">
                <Check size={13} />
                激活
              </button>
            )}
            <button className="primary-button compact-button" onClick={saveModel} disabled={!editable} type="button">
              <Save size={13} />
              保存
            </button>
          </div>
        </div>

        {selected || creating ? (
          <div className="model-config-body">
            {isConfigModel ? (
              <div className="agent-path-note">
                <AlertTriangle size={14} />
                配置来源：<code>config.yaml</code>
              </div>
            ) : null}
            <div className="model-secret-state" data-configured={secretConfigured}>
              {secretConfigured ? <Check size={14} /> : <AlertTriangle size={14} />}
              <span>{apiKeyAction === 'clear' ? '保存后清除已保存密钥' : secretConfigured ? '密钥已配置' : '未配置密钥'}</span>
            </div>
            <div className="model-config-form">
              <label>Base URL<input disabled={!editable} value={draft.base_url} onChange={(event) => setDraft((current) => ({ ...current, base_url: event.target.value }))} /></label>
              <label>Model<input disabled={!editable} value={draft.model} onChange={(event) => patchModelValue(event.target.value)} /></label>
              <label>显示名称<input disabled={!editable} value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} /></label>
              <label>API Key<input disabled={!editable} value={apiKeyText} onChange={(event) => updateApiKeyText(event.target.value)} type="password" placeholder="不会回显已保存密钥" /></label>
              {editable && selected?.source === 'user' && selected.api_key_saved ? (
                <button className="secondary-button compact-button model-secret-clear" onClick={clearSavedApiKey} disabled={apiKeyAction === 'clear'} type="button">
                  清除已保存密钥
                </button>
              ) : null}
            </div>
            <section className="model-advanced-section">
              <button
                className="model-advanced-toggle"
                data-open={modelAdvancedOpen}
                onClick={() => setModelAdvancedOpen((value) => !value)}
                type="button"
              >
                <ChevronRight size={14} />
                <span>高级配置</span>
              </button>
              {modelAdvancedOpen ? (
                <div className="model-config-form model-advanced-content">
                  <label>API Key Env<input disabled={!editable} value={draft.api_key_env ?? ''} onChange={(event) => setDraft((current) => ({ ...current, api_key_env: event.target.value }))} /></label>
                  <label>Timeout<input disabled={!editable} value={draft.timeout_seconds} onChange={(event) => setDraft((current) => ({ ...current, timeout_seconds: Number(event.target.value) || 60 }))} type="number" min="1" /></label>
                  <label className="model-checkbox-row">
                    <input disabled={!editable} checked={draft.strip_thinking} onChange={(event) => setDraft((current) => ({ ...current, strip_thinking: event.target.checked }))} type="checkbox" />
                    <span>{'移除 <think> 推理块'}</span>
                  </label>
                  <label>Reasoning JSON<textarea disabled={!editable} value={reasoningText} onChange={(event) => setReasoningText(event.target.value)} placeholder='{"enabled": true, "effort": "medium"}' /></label>
                  <label>Extra Request Args<textarea disabled={!editable} value={extraRequestArgsText} onChange={(event) => setExtraRequestArgsText(event.target.value)} /></label>
                  <label>Extra Body<textarea disabled={!editable} value={extraBodyText} onChange={(event) => setExtraBodyText(event.target.value)} /></label>
                </div>
              ) : null}
            </section>
            <div className="inline-actions model-actions">
              <button className="secondary-button compact-button" onClick={activateModel} disabled={creating || !selected || selected.active} type="button">
                <Check size={13} />
                设为当前模型
              </button>
              <button className="secondary-button danger-button compact-button" onClick={removeModel} disabled={creating || !selected || selected.source !== 'user'} type="button">
                <Trash2 size={13} />
                删除
              </button>
            </div>
            {message ? <p className="form-message">{message}</p> : null}
          </div>
        ) : (
          <div className="empty-state compact">暂无模型配置。</div>
        )}
      </section>
    </section>
  );
}

function modelInputFromProvider(model: ModelProvider): ModelProviderInput {
  return {
    id: model.id,
    name: model.name,
    base_url: model.base_url,
    model: model.model,
    api_key: null,
    api_key_action: 'preserve',
    api_key_env: model.api_key_env ?? '',
    timeout_seconds: model.timeout_seconds,
    strip_thinking: model.strip_thinking,
    reasoning: model.reasoning ?? null,
    extra_request_args: model.extra_request_args ?? {},
    extra_body: model.extra_body ?? {},
  };
}

function modelDisplayNameForDraft(currentName: string, previousModel: string, nextModel: string): string {
  const name = currentName.trim();
  if (!name || name === previousModel.trim()) return nextModel;
  return currentName;
}

function uniqueModelDraftId(label: string, models: ModelProvider[]): string {
  const base = slugValue(label || 'user-model') || 'user-model';
  const used = new Set(models.map((model) => model.id));
  if (!used.has(base)) return base;
  let suffix = 2;
  while (used.has(`${base}-${suffix}`)) suffix += 1;
  return `${base}-${suffix}`;
}

function slugValue(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

function formatModelJson(value: Record<string, unknown> | null | undefined, emptyIsBlank = false): string {
  if (!value || !Object.keys(value).length) return emptyIsBlank ? '' : '{}';
  return JSON.stringify(value, null, 2);
}

function modelSourceLabel(source: ModelProvider['source']): string {
  return source === 'config' ? 'config.yaml' : 'user';
}

function AgentManagementWorkspace({
  activeSub,
  agentPresetErrors,
  agentPresets,
  capabilities,
  creatingAgentPreset,
  creating,
  profiles,
  selectedAgentPresetId,
  warnings,
  selectedId,
  skills,
  onAgentPresetCreate,
  onAgentPresetCreatingChange,
  onAgentPresetDelete,
  onAgentPresetSelect,
  onAgentPresetUpdate,
  onCreate,
  onCreatingChange,
  onDelete,
  onRefresh,
  onSelect,
  onUpdate,
}: {
  activeSub: AgentManagementSub;
  agentPresetErrors: Record<string, string>;
  agentPresets: AgentPreset[];
  capabilities: CapabilityDefinition[];
  creatingAgentPreset: boolean;
  creating: boolean;
  profiles: AgentProfile[];
  selectedAgentPresetId: string;
  warnings: ProfileWarning[];
  selectedId: string;
  skills: SkillSummary[];
  onAgentPresetCreate: (payload: AgentPresetInput) => Promise<AgentPreset>;
  onAgentPresetCreatingChange: (creating: boolean) => void;
  onAgentPresetDelete: (name: string) => Promise<void>;
  onAgentPresetSelect: (id: string) => void;
  onAgentPresetUpdate: (name: string, payload: Omit<AgentPresetInput, 'name'>) => Promise<AgentPreset>;
  onCreate: (name: string, content: string) => Promise<AgentProfile>;
  onCreatingChange: (creating: boolean) => void;
  onDelete: (name: string) => Promise<void>;
  onRefresh: (preferredProfileId?: string) => Promise<void>;
  onSelect: (id: string) => void;
  onUpdate: (name: string, content: string) => Promise<AgentProfile>;
}) {
  const selected = profiles.find((profile) => profile.id === selectedId) ?? profiles[0] ?? null;
  const [draftName, setDraftName] = useState('');
  const [draftContent, setDraftContent] = useState('');
  const [message, setMessage] = useState('');
  const [saving, setSaving] = useState(false);
  const canEdit = creating || Boolean(selected?.editable);
  const isDirty = creating || Boolean(selected && draftContent !== selected.content);

  useEffect(() => {
    if (!selected && profiles[0]) onSelect(profiles[0].id);
  }, [onSelect, profiles, selected]);

  useEffect(() => {
    if (creating) {
      setDraftName(uniqueProfileName('agent', profiles));
      setDraftContent('# New Agent\n\n');
      setMessage('');
    }
  }, [creating, profiles]);

  useEffect(() => {
    if (creating || !selected) return;
    setDraftName(selected.name);
    setDraftContent(selected.content || '');
    setMessage('');
  }, [creating, selected]);

  const startCopy = () => {
    if (!selected) return;
    setDraftName(uniqueProfileName(`${selected.name}_copy`, profiles));
    setDraftContent(selected.content || '');
    setMessage('');
    onCreatingChange(true);
  };
  const cancelCreate = () => {
    onCreatingChange(false);
    if (profiles[0]) onSelect(profiles[0].id);
  };
  const saveDraft = async () => {
    setSaving(true);
    setMessage('');
    try {
      if (creating) {
        const profile = await onCreate(draftName, draftContent);
        onSelect(profile.id);
      } else if (selected?.editable) {
        const profile = await onUpdate(selected.name, draftContent);
        onSelect(profile.id);
      }
      setMessage('已保存。');
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSaving(false);
    }
  };
  const deleteSelected = async () => {
    if (!selected?.deletable) return;
    setSaving(true);
    setMessage('');
    try {
      await onDelete(selected.name);
      setMessage('已删除。');
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSaving(false);
    }
  };

  if (activeSub === 'presets') {
    return (
      <AgentPresetManagementPane
        capabilities={capabilities}
        creating={creatingAgentPreset}
        errors={agentPresetErrors}
        presets={agentPresets}
        profiles={profiles}
        selectedId={selectedAgentPresetId}
        skills={skills}
        onCreate={onAgentPresetCreate}
        onCreatingChange={onAgentPresetCreatingChange}
        onDelete={onAgentPresetDelete}
        onSelect={onAgentPresetSelect}
        onUpdate={onAgentPresetUpdate}
      />
    );
  }

  return (
    <section className="design-agents-workspace">
      <div className="agent-prompt-editor">
        <div className="agent-editor-toolbar">
          <div className="agent-editor-icon">
            <Bot size={15} />
          </div>
          <div>
            <strong>{creating ? '新建本地配置' : selected?.name ?? '角色设定'}</strong>
            <span>{creating ? '本地受管配置' : selected ? profileSourceLabel(selected) : 'Markdown profile'}</span>
          </div>
          <div>
            {creating ? (
              <button className="secondary-button compact-button" onClick={cancelCreate} type="button" disabled={saving}>
                <X size={14} />
                取消
              </button>
            ) : (
              <button className="secondary-button compact-button" onClick={startCopy} type="button" disabled={!selected || saving}>
                <Copy size={14} />
                复制为本地
              </button>
            )}
            <button className="primary-button compact-button" onClick={() => void saveDraft()} type="button" disabled={!canEdit || !isDirty || saving}>
              <Save size={14} />
              保存
            </button>
          </div>
        </div>
        {selected || creating ? (
          <div className="agent-editor-body">
            {creating ? (
              <label className="agent-name-field">
                <span>配置名称</span>
                <input
                  value={draftName}
                  onChange={(event) => setDraftName(event.target.value)}
                  placeholder="agent_name"
                />
              </label>
            ) : null}
            <div className="agent-editor-title-row">
              <span>系统提示词</span>
              <em>{draftContent.length} chars</em>
            </div>
            <textarea
              value={draftContent}
              readOnly={!canEdit}
              spellCheck={false}
              onChange={(event) => setDraftContent(event.target.value)}
            />
            <div className="agent-path-note">
              <AlertTriangle size={14} />
              <span>{creating ? '保存后会生成可用于静态编排的 agent capability。' : `来源：${selected ? profileSourceLabel(selected) : ''}`}</span>
            </div>
            {message ? <p className="form-message">{message}</p> : null}
          </div>
        ) : (
          <div className="empty-state agent-empty-card">
            <UserCog size={28} />
            <strong>还没有角色设定</strong>
            <p>角色设定是一份 Markdown 系统提示词,用来描述智能体的身份与风格。</p>
          </div>
        )}
      </div>

      <aside className="agent-metadata-panel">
        <div className="agent-panel-label">配置信息</div>
        {selected ? (
          <>
            <div className="agent-info-table">
              <div><span>名称</span><strong>{selected.name}</strong></div>
              <div><span>描述</span><strong>{selected.description || 'Markdown profile'}</strong></div>
              <div><span>来源</span><strong>{profileSourceLabel(selected)}</strong></div>
              <div><span>字符数</span><strong>{selected.content.length}</strong></div>
              <div><span>可编辑</span><strong>{selected.editable ? '是' : '否'}</strong></div>
            </div>
            {warnings.length ? (
              <>
                <div className="agent-panel-label">加载警告</div>
                <div className="agent-warning-list">
                  {warnings.map((warning) => (
                    <p key={warning.name}><strong>{warning.name}</strong>: {warning.error}</p>
                  ))}
                </div>
              </>
            ) : null}
            <div className="agent-panel-label">危险操作</div>
            <button className="danger-line-button" onClick={() => void deleteSelected()} type="button" disabled={!selected.deletable || saving}>
              <Trash2 size={14} />
              删除配置
            </button>
            <button className="secondary-button compact-button" onClick={() => void onRefresh(selected.id)} type="button" disabled={saving}>
              <RefreshCw size={14} />
              刷新
            </button>
          </>
        ) : null}
      </aside>
    </section>
  );
}

function AgentPresetManagementPane({
  capabilities,
  creating,
  errors,
  presets,
  profiles,
  selectedId,
  skills,
  onCreate,
  onCreatingChange,
  onDelete,
  onSelect,
  onUpdate,
}: {
  capabilities: CapabilityDefinition[];
  creating: boolean;
  errors: Record<string, string>;
  presets: AgentPreset[];
  profiles: AgentProfile[];
  selectedId: string;
  skills: SkillSummary[];
  onCreate: (payload: AgentPresetInput) => Promise<AgentPreset>;
  onCreatingChange: (creating: boolean) => void;
  onDelete: (name: string) => Promise<void>;
  onSelect: (id: string) => void;
  onUpdate: (name: string, payload: Omit<AgentPresetInput, 'name'>) => Promise<AgentPreset>;
}) {
  const selected = presets.find((preset) => preset.id === selectedId) ?? presets[0] ?? null;
  const [draft, setDraft] = useState<AgentPresetInput>(() => emptyAgentPresetDraft(profiles, presets));
  const [message, setMessage] = useState('');
  const [saving, setSaving] = useState(false);
  const editableCapabilities = capabilities.filter(
    (capability) => capability.enabled && capability.kind !== 'agent' && capability.kind !== 'skill',
  );
  const capabilityGroups = capabilityOptionGroups(editableCapabilities);
  const selectedCapabilities = new Set(draft.capabilities ?? []);
  const selectedSkills = new Set(draft.skills ?? []);
  const errorEntries = Object.entries(errors).sort(([left], [right]) => left.localeCompare(right));
  const isDirty = creating || Boolean(selected && !agentPresetDraftEquals(selected, draft));

  useEffect(() => {
    if (!selected && presets[0]) onSelect(presets[0].id);
  }, [onSelect, presets, selected]);

  useEffect(() => {
    if (!creating) return;
    setDraft(emptyAgentPresetDraft(profiles, presets));
    setMessage('');
  }, [creating, presets, profiles]);

  useEffect(() => {
    if (creating || !selected) return;
    setDraft(agentPresetToInput(selected));
    setMessage('');
  }, [creating, selected]);

  const startCreate = () => {
    onCreatingChange(true);
    setDraft(emptyAgentPresetDraft(profiles, presets));
    setMessage('');
  };
  const cancelCreate = () => {
    onCreatingChange(false);
    if (presets[0]) onSelect(presets[0].id);
  };
  const patchDraft = (patch: Partial<AgentPresetInput>) => {
    setDraft((current) => ({ ...current, ...patch }));
  };
  const patchCapability = (capabilityId: string, checked: boolean) => {
    patchDraft({ capabilities: toggleValue(draft.capabilities ?? [], capabilityId, checked) });
  };
  const patchSkill = (skillName: string, checked: boolean) => {
    patchDraft({ skills: toggleValue(draft.skills ?? [], skillName, checked) });
  };
  const saveDraft = async () => {
    const name = cleanWorkspaceKeyDraft(draft.name);
    if (!name) {
      setMessage('预设名称不能为空。');
      return;
    }
    if (!draft.profile) {
      setMessage('请选择角色设定。');
      return;
    }
    setSaving(true);
    setMessage('');
    const payload: AgentPresetInput = {
      ...draft,
      name,
      agents: [],
      review: 'fast',
      max_steps: Math.max(1, Number(draft.max_steps) || 1),
      capabilities: draft.capabilities ?? [],
      skills: draft.skills ?? [],
    };
    try {
      if (creating) {
        await onCreate(payload);
      } else if (selected) {
        const { name: _name, ...updatePayload } = payload;
        await onUpdate(selected.name, updatePayload);
      }
      setMessage('已保存。');
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSaving(false);
    }
  };
  const deleteSelected = async () => {
    if (!selected) return;
    setSaving(true);
    setMessage('');
    try {
      await onDelete(selected.name);
      setMessage('已删除。');
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="design-agents-workspace">
      <div className="agent-prompt-editor">
        <div className="agent-editor-toolbar">
          <div className="agent-editor-icon">
            <Bot size={15} />
          </div>
          <div>
            <strong>{creating ? '新建智能体预设' : selected?.name ?? '智能体预设'}</strong>
            <span>{creating ? '智能体能力预设' : selected?.id ?? '智能体能力预设'}</span>
          </div>
          <div>
            {creating ? (
              <button className="secondary-button compact-button" onClick={cancelCreate} type="button" disabled={saving}>
                <X size={14} />
                取消
              </button>
            ) : (
              <button className="secondary-button compact-button" onClick={startCreate} type="button" disabled={saving}>
                <Plus size={14} />
                新建
              </button>
            )}
            <button className="primary-button compact-button" onClick={() => void saveDraft()} type="button" disabled={!isDirty || saving}>
              <Save size={14} />
              保存
            </button>
          </div>
        </div>
        {selected || creating ? (
          <div className="agent-editor-body agent-preset-editor-body">
            <div className="compact-form-grid">
              <label>
                预设名称
                <input
                  value={draft.name}
                  disabled={!creating}
                  onChange={(event) => patchDraft({ name: cleanWorkspaceKeyDraft(event.target.value) })}
                  placeholder="helper"
                />
              </label>
              <label>
                角色设定
                <select value={draft.profile} onChange={(event) => patchDraft({ profile: event.target.value })}>
                  {profiles.map((profile) => (
                    <option key={profile.id} value={profile.name}>{profile.name}</option>
                  ))}
                </select>
              </label>
              <label>
                最大步数
                <input
                  min={1}
                  type="number"
                  value={draft.max_steps}
                  onChange={(event) => patchDraft({ max_steps: Math.max(1, Number(event.target.value) || 1) })}
                />
              </label>
              <label>
                审查
                <input value="fast" disabled />
              </label>
            </div>
            <label className="agent-preset-description">
              描述
              <textarea value={draft.description} onChange={(event) => patchDraft({ description: event.target.value })} />
            </label>
            <div className="agent-node-scope-list agent-preset-scope-list">
              {capabilityGroups.map((group) => (
                <section className="scope-group" key={group.kind}>
                  <h3>{group.label}</h3>
                  {group.items.map((capability) => (
                    <label className="scope-row" key={capability.id}>
                      <input
                        type="checkbox"
                        checked={selectedCapabilities.has(capability.id)}
                        onChange={(event) => patchCapability(capability.id, event.target.checked)}
                      />
                      <span>
                        <strong>{capabilityDisplayName(capability)}</strong>
                        <span>{capabilityScopeDetail(capability)}</span>
                      </span>
                    </label>
                  ))}
                </section>
              ))}
              {skills.length ? (
                <section className="scope-group">
                  <h3>技能</h3>
                  {skills.map((skill) => {
                    const lookup = skillLookupName(skill);
                    return (
                      <label className="scope-row" key={skill.path}>
                        <input
                          type="checkbox"
                          checked={selectedSkills.has(lookup)}
                          onChange={(event) => patchSkill(lookup, event.target.checked)}
                        />
                        <span>
                          <strong>{skill.name}</strong>
                          <span>{skill.category ? `${skill.category} · ${skill.path}` : skill.path}</span>
                        </span>
                      </label>
                    );
                  })}
                </section>
              ) : null}
            </div>
            {message ? <p className="form-message">{message}</p> : null}
          </div>
        ) : (
          <div className="empty-state agent-empty-card">
            <Bot size={28} />
            <strong>还没有智能体预设</strong>
            <p>把角色设定与能力、技能组合成一个可复用的智能体。</p>
            <button className="primary-button compact-button" onClick={startCreate} type="button">
              <Plus size={14} />
              新建智能体预设
            </button>
          </div>
        )}
      </div>

      <aside className="agent-metadata-panel">
        <div className="agent-panel-label">预设信息</div>
        {selected ? (
          <>
            <div className="agent-info-table">
              <div><span>ID</span><strong>{selected.id}</strong></div>
              <div><span>角色设定</span><strong>{selected.profile}</strong></div>
              <div><span>能力</span><strong>{selected.capabilities?.length ?? 0}</strong></div>
              <div><span>技能</span><strong>{selected.skills?.length ?? 0}</strong></div>
              <div><span>最大步数</span><strong>{selected.max_steps}</strong></div>
            </div>
            <div className="agent-panel-label">危险操作</div>
            <button className="danger-line-button" onClick={() => void deleteSelected()} type="button" disabled={saving}>
              <Trash2 size={14} />
              删除预设
            </button>
          </>
        ) : null}
        {errorEntries.length ? (
          <>
            <div className="agent-panel-label">加载错误</div>
            <div className="agent-warning-list">
              {errorEntries.map(([name, error]) => (
                <p key={name}><strong>{name}</strong>: {error}</p>
              ))}
            </div>
          </>
        ) : null}
      </aside>
    </section>
  );
}

function chatCapabilityScopeLabel(
  mode: ChatScopeMode,
  capabilityCount: number,
  skillCount: number,
  agentScope: AgentScopeMode = 'none',
  agentCount = 0,
): string {
  const agentText = agentScope === 'registered'
    ? '全部智能体预设'
    : agentScope === 'selected'
      ? `${agentCount} 个智能体预设`
      : '';
  if (mode === 'all') return agentText ? `全部能力 · ${agentText}` : '全部能力';
  const total = capabilityCount + skillCount;
  const base = total === 0
    ? 'No capabilities'
    : skillCount === 0
      ? `${capabilityCount} capabilities`
      : capabilityCount === 0
        ? `${skillCount} skills`
        : `${capabilityCount} capabilities · ${skillCount} skills`;
  return agentText ? `${base} · ${agentText}` : base;
}

function matchesCapabilityQuery(capability: CapabilityDefinition, query: string): boolean {
  const server = typeof capability.config?.server === 'string' ? capability.config.server : '';
  const tool = typeof capability.config?.tool === 'string' ? capability.config.tool : '';
  return matchesSearchQuery(
    [capability.id, capability.name, capability.display_name, capability.kind, capability.description, server, tool],
    query,
  );
}

function matchesSkillQuery(skill: SkillSummary, query: string): boolean {
  return matchesSearchQuery([skill.name, skill.category, skill.description, skill.path], query);
}

function matchesAgentPresetQuery(agent: AgentPreset, query: string): boolean {
  return matchesSearchQuery(
    [
      agent.id,
      agent.name,
      agent.profile,
      agent.description,
      agent.review,
      ...(agent.capabilities ?? []),
      ...(agent.skills ?? []),
      ...(agent.agents ?? []),
    ],
    query,
  );
}

function capabilityScopeDetail(capability: CapabilityDefinition): string {
  const server = typeof capability.config?.server === 'string' ? capability.config.server : '';
  const tool = typeof capability.config?.tool === 'string' ? capability.config.tool : '';
  const source = server ? `${server}${tool ? ` · ${tool}` : ''}` : capability.id;
  return `${capability.kind} · ${source}`;
}

function toggleValue(items: string[], value: string, enabled: boolean): string[] {
  if (enabled) return mergeValues(items, [value]);
  return items.filter((item) => item !== value);
}

function mergeValues(items: string[], values: string[]): string[] {
  const merged = [...items];
  const seen = new Set(merged);
  for (const value of values) {
    if (!seen.has(value)) {
      seen.add(value);
      merged.push(value);
    }
  }
  return merged;
}

function isEditableToolCapability(capability: CapabilityDefinition): boolean {
  return capability.kind === 'tool' && Object.prototype.hasOwnProperty.call(capability.config, 'template');
}

function skillLookupName(skill: SkillSummary): string {
  return skill.category ? `${skill.category}/${skill.name}` : skill.name;
}

function isManagedSkill(skill: SkillSummary): boolean {
  return Boolean(skill.managed);
}

function linesFromText(value: string): string[] {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function parseEnvText(value: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of linesFromText(value)) {
    const index = line.indexOf('=');
    if (index <= 0) throw new Error(`Invalid env line: ${line}`);
    env[line.slice(0, index).trim()] = line.slice(index + 1).trim();
  }
  return env;
}

function formatEnvText(env: Record<string, string>): string {
  return Object.entries(env).map(([key, value]) => `${key}=${value}`).join('\n');
}

function DagReviewDialog({
  dag,
  nodes,
  edges,
  trace,
  selectedNode,
  feedback,
  onFeedbackChange,
  onClose,
  onConfirm,
  onReject,
  onPatchNode,
  onAddNode,
  onDeleteNode,
  onNodesChange,
  onEdgesChange,
  onSelectNode,
}: {
  dag: Dag;
  nodes: Node[];
  edges: Edge[];
  trace: TraceLogEvent[];
  selectedNode?: DagNode;
  feedback: string;
  onFeedbackChange: (value: string) => void;
  onClose: () => void;
  onConfirm: () => void;
  onReject: () => void;
  onPatchNode: (patch: Partial<DagNode>, edges?: DagEdge[]) => void;
  onAddNode: () => void;
  onDeleteNode: (nodeId?: string) => void;
  onNodesChange: (changes: NodeChange[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onSelectNode: (id: string) => void;
}) {
  const canConfirm = dag.nodes.length > 0 && isDagConfirmable(dag);
  const riskyNodes = dag.nodes.filter((node) => nodeReviewInfo(node).reviewAttention).length;
  const selectedNodeLogs = selectedNode
    ? trace.filter((event) => event.node_id === selectedNode.id && (!event.dag_id || event.dag_id === dag.dag_id))
    : [];
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="DAG review">
      <div className="dag-modal dag-review-modal">
        <header className="modal-header dag-review-header">
          <div className="modal-title-wrap">
            <span className="dag-review-eyebrow">人工审核</span>
            <div className="modal-title">
              <GitBranch size={19} />
              <span>DAG 审查</span>
              <StatusBadge status={dag.status} />
              <code>{dag.task_id || dag.dag_id}</code>
            </div>
          </div>
          <div className="modal-actions">
            <button className="secondary-button compact-button" onClick={() => onAddNode()} type="button">
              <Plus size={16} />
              添加节点
            </button>
            <button className="secondary-button compact-button danger-button" onClick={onReject} disabled={!canConfirm} type="button">
              <X size={16} />
              驳回并反馈
            </button>
            <button className="primary-button" onClick={onConfirm} disabled={!canConfirm} type="button">
              <Check size={17} />
              {canConfirm ? '通过并继续' : '已完成'}
            </button>
            <button className="icon-button" onClick={onClose} title="Close" type="button">
              <X size={18} />
            </button>
          </div>
        </header>
        <div className="modal-body">
          <section className="modal-flow">
            <div className="review-flow-card">
              <span className="review-flow-label">流程概览</span>
              <div className="review-flow-summary">
                <div className="review-stat">
                  <strong>{dag.nodes.length}</strong>
                  <span>节点</span>
                </div>
                <div className="review-stat">
                  <strong>{dag.edges.length}</strong>
                  <span>连线</span>
                </div>
                <div className="review-stat">
                  <strong>{riskyNodes}</strong>
                  <span>风险</span>
                </div>
              </div>
            </div>
            <ReactFlow
              className="orchestration-flow"
              nodes={nodes}
              edges={edges}
              nodeTypes={designNodeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onNodeClick={(_, node) => onSelectNode(node.id)}
              fitView
              fitViewOptions={{ padding: 0.2 }}
              proOptions={{ hideAttribution: true }}
            >
              <Background color="#e2e4ea" gap={20} />
              <MiniMap pannable zoomable nodeColor="#4f6ef7" maskColor="rgba(245,246,248,0.7)" />
              <Controls />
            </ReactFlow>
          </section>
          <aside className="modal-side">
            <div className="node-inspector-title">
              <span>节点检查器</span>
              <strong>{selectedNode?.title || selectedNode?.id || '未选择节点'}</strong>
            </div>
            {selectedNode ? (
              <NodeEditor
                node={normalizeNode(selectedNode)}
                dag={dag}
                logs={selectedNodeLogs}
                onPatch={onPatchNode}
                onDelete={() => onDeleteNode(selectedNode.id)}
              />
            ) : (
              <div className="empty-state compact">Select a DAG node to inspect details.</div>
            )}
          </aside>
        </div>
        <footer className="dag-review-footer">
          <div className="review-feedback-shell">
            <label htmlFor="dag-review-feedback">反馈给 Agent</label>
            <textarea
              id="dag-review-feedback"
              rows={2}
              value={feedback}
              onChange={(event) => onFeedbackChange(event.target.value)}
              placeholder="写下拒绝原因或希望调整的规划方向…"
            />
          </div>
          <span className="review-footer-note">通过后继续执行；驳回时会带上反馈重新规划</span>
        </footer>
      </div>
    </div>
  );
}

function NodeEditor({
  node,
  dag,
  logs,
  onPatch,
  onDelete,
}: {
  node: DagNode;
  dag: Dag;
  logs: TraceLogEvent[];
  onPatch: (patch: Partial<DagNode>, edges?: DagEdge[]) => void;
  onDelete: () => void;
}) {
  const dependsOn = dag.edges.filter((edge) => edge.target === node.id).map((edge) => edge.source);
  if (!isCapabilityNode(node)) {
    return (
      <div className="node-editor">
        <label>
          Node ID
          <input value={node.id} disabled />
        </label>
        <div className="empty-state compact">Internal start node</div>
      </div>
    );
  }
  const invocation = node.payload.invocation;
  const boundary = invocation.boundary ?? {
    allowed_paths: ['.'],
  };
  const patchInvocation = (patch: Partial<typeof invocation>) =>
    onPatch({ payload: { type: 'capability', invocation: { ...invocation, ...patch } } });
  return (
    <div className="node-editor">
      <label>
        Node ID
        <input value={node.id} disabled />
      </label>
      <div className="two-col">
        <label>
          Risk
          <select
            value={invocation.risk ?? 'low'}
            onChange={(event) => patchInvocation({ risk: event.target.value as RiskLevel })}
          >
            {riskLevels.map((risk) => (
              <option key={risk} value={risk}>
                {risk}
              </option>
            ))}
          </select>
        </label>
        <label>
          Status
          <input value={node.status ?? 'planned'} disabled />
        </label>
      </div>
      <label>
        Capability
        <input
          value={invocation.capability_id}
          onChange={(event) => patchInvocation({ capability_id: event.target.value })}
        />
      </label>
      <label>
        Type
        <select
          value={invocation.kind ?? 'tool'}
          onChange={(event) => patchInvocation({ kind: event.target.value as CapabilityKind })}
        >
          {capabilityKinds.map((kind) => (
            <option key={kind} value={kind}>
              {kind}
            </option>
          ))}
        </select>
      </label>
      <label>
        Args JSON
        <textarea
          value={JSON.stringify(invocation.arguments ?? {}, null, 2)}
          onChange={(event) => {
            const parsed = parseJsonObject(event.target.value);
            if (parsed) patchInvocation({ arguments: parsed });
          }}
        />
      </label>
      <label>
        Depends On
        <input
          value={dependsOn.join(', ')}
          onChange={(event) => {
            const sources = splitCsv(event.target.value).filter((source) => source !== node.id);
            const nextEdges = [
              ...dag.edges.filter((edge) => edge.target !== node.id),
              ...sources.map((source) => ({ source, target: node.id, reason: 'User dependency.' })),
            ];
            onPatch({}, nextEdges);
          }}
        />
      </label>
      <details className="node-policy-details">
        <summary>Execution Policy</summary>
        <label>
          Allowed Paths
          <BoundaryValueEditor
            values={boundary.allowed_paths ?? []}
            onChange={(allowedPaths) =>
              patchInvocation({ boundary: { ...boundary, allowed_paths: allowedPaths } })
            }
          />
        </label>
      </details>
      <button className="secondary-button danger-button" onClick={() => onDelete()} type="button">
        <Trash2 size={16} />
        Delete Node
      </button>
      <NodeExecutionLog logs={logs} />
    </div>
  );
}

function NodeExecutionLog({ logs }: { logs: TraceLogEvent[] }) {
  return (
    <section className="node-log-panel">
      <div className="node-log-title">
        <Wrench size={15} />
        <span>Execution Log</span>
      </div>
      {logs.length ? (
        <div className="node-log-list">
          {logs.map((event) => (
            <details key={event.id} className={`node-log-row ${event.status}`}>
              <summary>
                <span>{event.event_type ?? event.label}</span>
                <em>{event.timestamp}</em>
              </summary>
              <p>{event.detail}</p>
              {event.payload && Object.keys(event.payload).length ? (
                <pre>{clipText(JSON.stringify(event.payload, null, 2), 1600)}</pre>
              ) : null}
            </details>
          ))}
        </div>
      ) : (
        <div className="empty-state compact">No execution events recorded for this node yet.</div>
      )}
    </section>
  );
}

function BoundaryValueEditor({
  values,
  onChange,
}: {
  values: BoundaryValue[];
  onChange: (values: BoundaryValue[]) => void;
}) {
  const formatted = formatBoundaryValues(values);
  const [draft, setDraft] = useState(formatted);
  const [invalid, setInvalid] = useState(false);

  useEffect(() => {
    setDraft(formatted);
    setInvalid(false);
  }, [formatted]);

  return (
    <textarea
      className={invalid ? 'json-input invalid' : 'json-input'}
      value={draft}
      onChange={(event) => {
        const next = event.target.value;
        setDraft(next);
        const parsed = parseBoundaryValues(next);
        setInvalid(parsed === null);
        if (parsed !== null) {
          onChange(parsed);
        }
      }}
    />
  );
}

function addUniqueBoundaryValue(values: BoundaryValue[], value: BoundaryValue): BoundaryValue[] {
  const key = boundaryValueKey(value);
  return [...values.filter((item) => boundaryValueKey(item) !== key), value];
}

function boundaryValueKey(value: BoundaryValue): string {
  return typeof value === 'string'
    ? `str:${value}`
    : `expr:${JSON.stringify(value)}`;
}

function formatBoundaryValues(values: BoundaryValue[]): string {
  return JSON.stringify(values, null, 2);
}

function parseBoundaryValues(value: string): BoundaryValue[] | null {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? (parsed as BoundaryValue[]) : null;
  } catch {
    return null;
  }
}

function nodeDepths(dag: Dag): Map<string, number> {
  const depths = new Map(dag.nodes.map((node) => [node.id, 0]));
  for (let index = 0; index < dag.nodes.length; index += 1) {
    for (const edge of dag.edges) {
      depths.set(edge.target, Math.max(depths.get(edge.target) ?? 0, (depths.get(edge.source) ?? 0) + 1));
    }
  }
  return depths;
}

function splitCsv(value: string) {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseJsonObject(value: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}
