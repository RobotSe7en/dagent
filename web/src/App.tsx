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
  Database,
  File,
  FileText,
  Folder,
  GitBranch,
  Loader,
  MessageSquare,
  Plus,
  Play,
  RefreshCw,
  Save,
  Search,
  Send,
  SlidersHorizontal,
  Trash2,
  Upload,
  UserCog,
  Wrench,
  X,
} from 'lucide-react';
import {
  createCapability,
  createMcpServer,
  deleteCapability,
  deleteMcpServer,
  deleteSkill,
  getSkill,
  getSkillFile,
  getValidationStatus,
  installSkill,
  listCapabilities,
  listDags,
  listMcpServers,
  listProfiles,
  listSkills,
  reloadMcpServers,
  resetSession,
  resumeCapabilityReview,
  resumeDagReview,
  runDagStream,
  saveDag,
  setCapabilityEnabled,
  setValidationEnabled as apiSetValidation,
  streamTask,
  testCapability,
  uploadDagArtifact,
  updateMcpServer,
} from './api';
import type { ApiRunState } from './api';
import type {
  AgentProfile,
  BoundaryMode,
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
  UserDag,
  ProfileWarning,
  ReviewEventPayload,
  ValidationFeedbackEvent,
  ReviewLevel,
  RiskLevel,
  CapabilityStreamEvent,
  TraceLogEvent,
  WorkspaceKey,
  Artifact,
  MCPServer,
  MCPServerConfig,
  SkillDetail,
  SkillFileDetail,
  SkillSummary,
  BoundaryValue,
  UserDagNode,
} from './types';
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
  createUploadedFileArtifacts,
  isUploadedFileArtifact,
  removeArtifactBinding,
  upsertArtifact,
  type UploadSourceFile,
} from './dagArtifacts';
import {
  appendRunTranscriptCapability,
  appendRunTranscriptToken,
  buildRunDialogSummary,
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
  artifactPreviewText,
  buildWorkbenchArtifacts,
  type WorkbenchArtifactItem,
} from './workbenchArtifacts';

const riskClass: Record<RiskLevel, string> = {
  low: 'risk-low',
  medium: 'risk-medium',
  high: 'risk-high',
};

const riskLevels: RiskLevel[] = ['low', 'medium', 'high'];
const boundaryModes: BoundaryMode[] = ['read_only', 'write_limited', 'full'];
const reviewLevels: ReviewLevel[] = ['fast', 'careful'];
const capabilityKinds: CapabilityKind[] = ['tool', 'mcp', 'skill', 'agent', 'memory'];
const riskRank: Record<RiskLevel, number> = { low: 0, medium: 1, high: 2 };
const boundaryRank: Record<BoundaryMode, number> = { read_only: 0, write_limited: 1, full: 2 };
const defaultWorkspaceRoot = '.dagent-runs';
const emptyDag: Dag = {
  dag_id: 'dag_empty',
  task_id: '',
  version: 1,
  status: 'draft',
  nodes: [],
  edges: [],
};

const defaultCapabilityPolicy = {
  risk: 'low' as RiskLevel,
  requires_review: false,
  sandbox_required: false,
  network: false,
  secrets: [],
};

const defaultCustomCapability: CapabilityDefinition = {
  id: 'tool.example',
  name: 'example',
  kind: 'tool',
  description: '',
  parameters: {
    type: 'object',
    properties: {},
  },
  output_schema: {},
  policy: defaultCapabilityPolicy,
  config: {
    template: 'result:{text}',
  },
  enabled: true,
};

const defaultMcpConfig: { name: string } & MCPServerConfig = {
  name: 'local',
  command: '',
  args: [],
  env: {},
  enabled: true,
  risk: 'medium',
  connect_timeout: 30,
  tool_timeout: 60,
};

const workspaceItems: Array<{ key: WorkspaceKey; label: string; icon: React.ReactNode }> = [
  { key: 'chat', label: '智能对话', icon: <MessageSquare size={16} /> },
  { key: 'orchestration', label: '智能体编排', icon: <GitBranch size={16} /> },
  { key: 'tools', label: '能力管理', icon: <Wrench size={16} /> },
  { key: 'agents', label: '智能体管理', icon: <Bot size={16} /> },
];

const workspacePlaceholderLabels: Record<Exclude<WorkspaceKey, 'chat'>, string> = {
  orchestration: 'AI 编排工作区',
  tools: '能力管理工作区',
  agents: '智能体管理工作区',
};

function isCapabilityNode(node: DagNode): node is DagNode & { payload: CapabilityNodePayload } {
  return node.payload.type === 'capability';
}

interface NodeReviewInfo {
  risk: RiskLevel;
  boundaryMode: BoundaryMode;
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
      mode: invocation.boundary?.mode ?? 'read_only',
      allowed_paths: invocation.boundary?.allowed_paths ?? ['.'],
      allowed_commands: invocation.boundary?.allowed_commands ?? [],
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
    boundaryMode: 'read_only',
    hasBoundary: false,
    reviewAttention: false,
  };
}

function invocationReviewInfo(invocation: CapabilityInvocation): NodeReviewInfo {
  const risk = invocation.risk ?? 'low';
  const boundaryMode = invocation.boundary?.mode ?? 'read_only';
  return {
    risk,
    boundaryMode,
    hasBoundary: true,
    reviewAttention: risk !== 'low' || boundaryMode === 'full',
  };
}

function nodesReviewInfo(nodes: DagNode[]): NodeReviewInfo {
  return nodes.reduce<NodeReviewInfo>(
    (summary, node) => mergeReviewInfo(summary, nodeReviewInfo(node)),
    {
      risk: 'low',
      boundaryMode: 'read_only',
      hasBoundary: false,
      reviewAttention: false,
    },
  );
}

function mergeReviewInfo(left: NodeReviewInfo, right: NodeReviewInfo): NodeReviewInfo {
  const risk = riskRank[right.risk] > riskRank[left.risk] ? right.risk : left.risk;
  const boundaryMode = boundaryRank[right.boundaryMode] > boundaryRank[left.boundaryMode]
    ? right.boundaryMode
    : left.boundaryMode;
  return {
    risk,
    boundaryMode,
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
  };
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
          mode: 'read_only',
          allowed_paths: ['.'],
          allowed_commands: [],
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

function userDagFromRuntimeDag(spec: UserDag, dag: Dag): UserDag {
  const nodes = dag.nodes.filter(isCapabilityNode).map(userNodeFromDagNode);
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

function capabilityDisplayName(capability: CapabilityDefinition): string {
  return `${capability.name} (${capability.id})`;
}

function capabilityRisk(capability?: CapabilityDefinition): RiskLevel {
  return capability?.policy?.risk ?? 'low';
}

type ChatTarget = 'auto' | 'tool' | 'dag';
type ChatScopeMode = 'all' | 'custom';
type ToolDirectoryTab = 'tools' | 'skills' | 'mcp';
type TokenChannel = 'reasoning' | 'content';

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
  boundaryMode: BoundaryMode;
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
    const boundaryMode = reviewInfo.boundaryMode;
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
        title: item.title || item.id,
        detail,
        kind: invocation?.kind ?? item.payload.type,
        risk,
        boundaryMode,
        reviewAttention,
        status,
      },
      type: 'designDag',
    };
  });
  const edges = dag.edges.map((edge) => ({
    id: `${edge.source}-${edge.target}`,
    source: edge.source,
    target: edge.target,
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

function DesignDagNode({ data, selected }: any) {
  const nodeData = data as DesignDagNodeData;
  return (
    <div
      className={selected ? 'orchestration-node-card selected' : 'orchestration-node-card'}
      data-kind={nodeData.kind}
      data-risk={nodeData.risk}
      data-status={nodeData.status}
    >
      <Handle className="orchestration-handle" position={Position.Left} type="target" />
      <span className="orchestration-node-icon">
        <GitBranch size={15} />
      </span>
      <span className="orchestration-node-copy">
        <strong title={nodeData.nodeId}>{nodeData.title}</strong>
        <em title={nodeData.detail}>{nodeData.detail}</em>
      </span>
      {nodeData.reviewAttention ? <span className={`risk-chip risk-${nodeData.risk}`}>{nodeData.risk}</span> : null}
      <Handle className="orchestration-handle" position={Position.Right} type="source" />
    </div>
  );
}

const designNodeTypes = {
  designDag: DesignDagNode,
};

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
  const [capabilityScopeOpen, setCapabilityScopeOpen] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [trace, setTrace] = useState<TraceLogEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [navCollapsed, setNavCollapsed] = useState(false);
  const [artifactPanelOpen, setArtifactPanelOpen] = useState(false);
  const [selectedArtifactId, setSelectedArtifactId] = useState('');
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
  const [capabilities, setCapabilities] = useState<CapabilityDefinition[]>([]);
  const [consoleError, setConsoleError] = useState<string | null>(null);
  const [savedDags, setSavedDags] = useState<UserDag[]>([]);
  const [editorUserDag, setEditorUserDag] = useState<UserDag>(() => createEmptyUserDag());
  const [editorDag, setEditorDag] = useState<Dag>(() => runtimeDagFromUserDag(editorUserDag));
  const [editorLayoutPositions, setEditorLayoutPositionsState] = useState<Record<string, XYPosition>>({});
  const editorLayoutPositionsRef = useRef<Record<string, XYPosition>>({});
  const [editorSelectedId, setEditorSelectedId] = useState('');
  const [editorTrace, setEditorTrace] = useState<TraceLogEvent[]>([]);
  const [editorRun, setEditorRun] = useState<DagRun | null>(null);
  const [editorRunTimeline, setEditorRunTimeline] = useState<RunTranscriptItem[]>([]);
  const [editorMessage, setEditorMessage] = useState('');
  const [editorRunning, setEditorRunning] = useState(false);
  const [editorWorkspaceRoot, setEditorWorkspaceRoot] = useState(defaultWorkspaceRoot);
  const [editorRunInputText, setEditorRunInputText] = useState('');
  const [profiles, setProfiles] = useState<AgentProfile[]>([]);
  const [profileWarnings, setProfileWarnings] = useState<ProfileWarning[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState('');
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [mcpServers, setMcpServers] = useState<MCPServer[]>([]);
  const [toolsDirectoryTab, setToolsDirectoryTab] = useState<ToolDirectoryTab>('tools');
  const [capabilityCreationIntent, setCapabilityCreationIntent] = useState<ToolDirectoryTab | null>(null);
  const [toolsDirectoryQuery, setToolsDirectoryQuery] = useState('');
  const [selectedToolCapabilityId, setSelectedToolCapabilityId] = useState('');
  const [selectedToolSkillName, setSelectedToolSkillName] = useState('');
  const [selectedToolMcpName, setSelectedToolMcpName] = useState('');
  const setEditorLayoutPositions = useCallback((positions: Record<string, XYPosition>) => {
    editorLayoutPositionsRef.current = positions;
    setEditorLayoutPositionsState(positions);
  }, []);

  const chatScopeLabel = chatCapabilityScopeLabel(
    chatScopeMode,
    selectedChatCapabilityIds.length,
    selectedChatSkillNames.length,
  );
  const chatArtifacts = useMemo(
    () => buildWorkbenchArtifacts({
      dag,
      runArtifacts: runState?.trace?.artifacts ?? null,
    }),
    [dag, runState],
  );
  const artifactDrawerOpen = artifactPanelOpen;
  const selectedArtifact = chatArtifacts.find((item) => item.id === selectedArtifactId) ?? chatArtifacts[0] ?? null;
  const chatHistory = useMemo(() => currentChatHistory(messages), [messages]);

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

  const refreshConsoleData = useCallback(async () => {
    setConsoleError(null);
    try {
      const [nextCapabilities, nextSpecs, nextProfiles, nextSkills, nextMcpServers] = await Promise.all([
        listCapabilities(),
        listDags(),
        listProfiles(),
        listSkills(),
        listMcpServers(),
      ]);
      setCapabilities(nextCapabilities);
      setSavedDags(nextSpecs);
      setProfiles(nextProfiles.profiles);
      setProfileWarnings(nextProfiles.warnings);
      setSkills(nextSkills);
      setMcpServers(nextMcpServers);
      setSelectedProfileId((current) => current || nextProfiles.profiles[0]?.id || '');
    } catch (exc) {
      setConsoleError(exc instanceof Error ? exc.message : String(exc));
    }
  }, []);

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
    setSelectedToolMcpName((current) =>
      current && mcpServers.some((server) => server.name === current)
        ? current
        : mcpServers[0]?.name ?? '',
    );
  }, [mcpServers]);

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
      chatArtifacts.some((item) => item.id === current) ? current : chatArtifacts[0].id,
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

  const syncEditorDag = useCallback((nextDag: Dag) => {
    setEditorDag(nextDag);
    const nextPositions = pruneNodePositions(editorLayoutPositionsRef.current, nextDag);
    setEditorLayoutPositions(nextPositions);
    const nextGraph = graphFromDag(nextDag, nextPositions);
    setEditorNodes(nextGraph.nodes);
    setEditorEdges(nextGraph.edges);
    setEditorSelectedId((current) =>
      nextDag.nodes.some((node) => node.id === current) ? current : nextDag.nodes[0]?.id ?? '',
    );
  }, [setEditorLayoutPositions]);

  const setEditorUserDagAndRuntimeDag = useCallback((spec: UserDag) => {
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
    setEditorUserDag(normalizedSpec);
    syncEditorDag(runtimeDagFromUserDag(normalizedSpec));
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

  const handlePendingReview = (pendingReview?: ReviewEventPayload | null) => {
    if (!pendingReview) return;
    if (pendingReview.kind === 'capability_review') {
      setCapabilityReviewFeedback('');
      setCapabilityReview(pendingReview as ReviewEventPayload);
      return;
    }
    setDagReviewFeedback('');
    setDagReview(pendingReview);
  };

  const appendTrace = (event: Omit<TraceLogEvent, 'id' | 'timestamp'>): TraceLogEvent => {
    const timestamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const nextEvent = { ...event, id: crypto.randomUUID(), timestamp };
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
                  mode: 'read_only',
                  allowed_paths: ['.'],
                  allowed_commands: [],
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
    setEditorUserDagAndRuntimeDag(createEmptyUserDag());
    setEditorWorkspaceRoot(defaultWorkspaceRoot);
    setEditorRunInputText('');
  };

  const loadEditorUserDag = (spec: UserDag) => {
    setEditorUserDagAndRuntimeDag(spec);
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
                mode: 'read_only',
                allowed_paths: ['.'],
                allowed_commands: [],
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
    updateEditorDag((current) => {
      const updatedNodes = current.nodes.map((node) => {
        if (node.id !== nodeId) return node;
        const merged = normalizeNode({ ...node, ...patch });
        if (patch.id && patch.id !== nodeId) {
          setEditorSelectedId(patch.id);
          const positions = { ...editorLayoutPositionsRef.current };
          positions[patch.id] = positions[nodeId] ?? editorNodes.find((item) => item.id === nodeId)?.position ?? nextHorizontalNodePosition(editorNodes);
          delete positions[nodeId];
          setEditorLayoutPositions(positions);
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

  const deleteEditorNode = (nodeId: string = editorSelectedId) => {
    if (!nodeId) return;
    updateEditorDag((current) => ({
      ...current,
      status: 'draft',
      nodes: current.nodes.filter((node) => node.id !== nodeId),
      edges: current.edges.filter((edge) => edge.source !== nodeId && edge.target !== nodeId),
    }));
  };

  const saveEditorDraftSpec = async (
    spec: UserDag,
    savingMessage = '正在保存 DAG...',
    savedMessage?: (saved: UserDag) => string,
  ): Promise<UserDag | null> => {
    const validation = validateUserDagDraft(spec);
    if (validation) {
      setEditorMessage(validation);
      return null;
    }
    setEditorMessage(savingMessage);
    try {
      const saved = await saveDag(spec);
      setEditorUserDagAndRuntimeDag(saved);
      await refreshConsoleData();
      setEditorMessage(savedMessage ? savedMessage(saved) : `已保存 ${saved.name || 'DAG'}。`);
      return saved;
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
      return null;
    }
  };

  const persistEditorUserDag = async (): Promise<boolean> => {
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    return Boolean(await saveEditorDraftSpec(spec));
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

  const deleteEditorArtifact = (artifactId: string) => {
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    const nextSpec = removeArtifactBinding(spec, artifactId);
    setEditorUserDagAndRuntimeDag(nextSpec);
    setEditorMessage(`已删除 artifact ${artifactId}。`);
  };

  const uploadEditorFiles = async (fileList: FileList | null) => {
    const files = filesFromList(fileList);
    if (!files.length) return;
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    const uploadDraft = createUploadedFileArtifacts(files as UploadSourceFile[], {
      artifacts: spec.artifacts ?? {},
      uploadRoot: 'inputs/uploads',
    });
    const saved = await saveEditorDraftSpec(
      { ...spec, artifacts: uploadDraft.artifacts },
      `正在保存并上传 ${files.length} 个文件...`,
      () => `正在上传 ${files.length} 个文件...`,
    );
    if (!saved) return;
    try {
      await Promise.all(uploadDraft.uploads.map((upload, index) =>
        uploadDagArtifact(saved.id, upload.artifact.id, [files[index]], { preserveRelativePath: false }),
      ));
      await refreshConsoleData();
      setEditorMessage(`已上传 ${files.length} 个文件。`);
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const runEditorSpec = async () => {
    if (editorRunning) return;
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    const parsedInput = parseDagRunInput(editorRunInputText);
    if (!parsedInput.ok) {
      setEditorMessage(parsedInput.message);
      return;
    }
    const saved = await persistEditorUserDag();
    if (!saved) return;
    const validation = validateUserDagDraft(spec);
    if (validation) return;
    setEditorRunning(true);
    setEditorTrace([]);
    setEditorRun(null);
    setEditorRunTimeline([]);
    setEditorMessage(`Running ${spec.name || 'DAG'}...`);
    try {
      await runDagStream(spec.id, {
        onTrace: (event) => {
          setEditorTrace((items) => [...items, event]);
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
        workspaceRoot: editorWorkspaceRoot,
        ...(parsedInput.hasInput ? { input: parsedInput.value } : {}),
      });
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setEditorRunning(false);
    }
  };

  const runStream = async () => {
    if (!draft.trim() || streaming) return;
    const prompt = draft.trim();
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
    const capabilityScope = chatScopeMode === 'all'
      ? undefined
      : { capabilityIds: selectedChatCapabilityIds, skills: selectedChatSkillNames };
    appendTrace({
      type: 'model',
      label: 'runtime_started',
      detail: `Agent target=${target}; capabilities=${chatScopeLabel}.`,
      status: 'running',
    });
    try {
      await streamTask(prompt, target, reviewLevel, {
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
      }, capabilityScope, runState);
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      appendTrace({ type: 'model', label: 'dag_agent_failed', detail: message, status: 'failed' });
    } finally {
      await waitForTokenQueue();
      setStreaming(false);
    }
  };

  const stopStream = () => {
    tokenQueueRef.current = [];
    contentStreamedRef.current = false;
    stopTokenTimer();
    resolveTokenDrain();
    setStreaming(false);
    appendTrace({ type: 'model', label: 'interrupted', detail: 'The current UI stream was interrupted.', status: 'failed' });
  };

  const resumeDag = async (approved: boolean) => {
    if (!dagReview || streaming) return;
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
      }, runState, feedback);
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      appendTrace({ type: 'model', label: 'resume_failed', detail: message, status: 'failed' });
    } finally {
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
      }, runState, feedback);
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      appendTrace({ type: 'model', label: 'capability_review_failed', detail: message, status: 'failed' });
    } finally {
      await waitForTokenQueue();
      setStreaming(false);
    }
  };

  const newChat = async () => {
    if (streaming) return;
    try {
      await resetSession();
      const enabled = await getValidationStatus();
      setValidationEnabled(enabled);
      setValidationError(null);
      setDagReview(null);
      setDagReviewFeedback('');
      setCapabilityReview(null);
      setCapabilityReviewFeedback('');
      setRunState(null);
    } catch (exc) {
      setValidationError(exc instanceof Error ? exc.message : String(exc));
    }
    setMessages([]);
    setDraft('');
    syncDag(emptyDag);
    setTrace([]);
    setError(null);
    setReviewOpen(false);
    tokenQueueRef.current = [];
    contentStreamedRef.current = false;
    stopTokenTimer();
  };

  return (
    <div className={`app-shell ${navCollapsed ? 'nav-collapsed' : ''}`}>
      <WorkspaceSidebar
        activeWorkspace={activeWorkspace}
        artifacts={editorArtifacts}
        collapsed={navCollapsed}
        capabilities={capabilities}
        capabilityCount={capabilities.length}
        history={chatHistory}
        mcpCount={mcpServers.length}
        mcpServers={mcpServers}
        profiles={profiles}
        savedDags={savedDags}
        selectedDagId={editorUserDag.id}
        selectedProfileId={selectedProfileId}
        selectedToolCapabilityId={selectedToolCapabilityId}
        selectedToolMcpName={selectedToolMcpName}
        selectedToolSkillName={selectedToolSkillName}
        skills={skills}
        skillCount={skills.length}
        toolsSub={toolsDirectoryTab}
        toolsQuery={toolsDirectoryQuery}
        onCreateArtifact={createEditorArtifact}
        onCreateMcp={() => requestCapabilityCreation('mcp')}
        onCreateTool={() => requestCapabilityCreation('tools')}
        onDeleteArtifact={deleteEditorArtifact}
        onImportSkill={() => requestCapabilityCreation('skills')}
        onLoadDag={loadEditorUserDag}
        onNewChat={() => void newChat()}
        onNewDag={newEditorUserDag}
        onSelectProfile={setSelectedProfileId}
        onSelectToolCapability={setSelectedToolCapabilityId}
        onSelectToolMcp={setSelectedToolMcpName}
        onSelectToolSkill={setSelectedToolSkillName}
        onSelectWorkspace={setActiveWorkspace}
        onToolsSubChange={selectToolsDirectoryTab}
        onToggleCollapsed={() => setNavCollapsed((value) => !value)}
        onToolsQueryChange={setToolsDirectoryQuery}
        onUploadFiles={(files) => void uploadEditorFiles(files)}
      />
      <main className="workspace">
        {consoleError ? <div className="error-banner global-error">{consoleError}</div> : null}
        {activeWorkspace === 'chat' ? (
          <ChatWorkspace
            artifactPanelOpen={artifactDrawerOpen}
            artifacts={chatArtifacts}
            chatScopeLabel={chatScopeLabel}
            currentDag={dag}
            draft={draft}
            error={error}
            loading={streaming}
            messageListRef={messageListRef}
            messages={messages}
            reviewLevel={reviewLevel}
            selectedArtifact={selectedArtifact}
            selectedArtifactId={selectedArtifactId}
            target={target}
            validationEnabled={validationEnabled}
            validationError={validationError}
            validationPending={validationPending}
            onArtifactSelect={setSelectedArtifactId}
            onDraftChange={setDraft}
            onOpenDag={(snapshot, snapshotTrace) => {
              syncDag(snapshot);
              if (snapshotTrace) setTrace(snapshotTrace);
              setReviewOpen(true);
            }}
            onOpenScope={() => setCapabilityScopeOpen(true)}
            onReviewLevelChange={setReviewLevel}
            onRun={() => void runStream()}
            onStop={stopStream}
            onTargetChange={setTarget}
            onToggleArtifacts={() => setArtifactPanelOpen((value) => !value)}
            onToggleValidation={() => void toggleValidation()}
          />
        ) : activeWorkspace === 'orchestration' ? (
          <OrchestrationWorkspace
            capabilities={capabilities}
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
            activeTab={toolsDirectoryTab}
            creationIntent={capabilityCreationIntent}
            query={toolsDirectoryQuery}
            selectedCapabilityId={selectedToolCapabilityId}
            selectedMcpName={selectedToolMcpName}
            selectedSkillName={selectedToolSkillName}
            onActiveTabChange={selectToolsDirectoryTab}
            onCreationIntentChange={setCapabilityCreationIntent}
            onSelectedCapabilityIdChange={setSelectedToolCapabilityId}
            onSelectedMcpNameChange={setSelectedToolMcpName}
            onSelectedSkillNameChange={setSelectedToolSkillName}
            onRefresh={refreshConsoleData}
          />
        ) : activeWorkspace === 'agents' ? (
          <AgentManagementWorkspace
            capabilities={capabilities}
            profiles={profiles}
            selectedId={selectedProfileId}
            warnings={profileWarnings}
            onSelect={setSelectedProfileId}
          />
        ) : (
          null
        )}
      </main>

      {capabilityScopeOpen ? (
        <ChatCapabilityScopeDialog
          capabilities={capabilities}
          skills={skills}
          mcpServers={mcpServers}
          mode={chatScopeMode}
          selectedCapabilityIds={selectedChatCapabilityIds}
          selectedSkillNames={selectedChatSkillNames}
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
  artifacts,
  collapsed,
  capabilities,
  capabilityCount,
  history,
  mcpCount,
  mcpServers,
  profiles,
  savedDags,
  selectedDagId,
  selectedProfileId,
  selectedToolCapabilityId,
  selectedToolMcpName,
  selectedToolSkillName,
  skills,
  skillCount,
  toolsSub,
  toolsQuery,
  onCreateArtifact,
  onCreateMcp,
  onCreateTool,
  onDeleteArtifact,
  onImportSkill,
  onLoadDag,
  onNewChat,
  onNewDag,
  onSelectProfile,
  onSelectToolCapability,
  onSelectToolMcp,
  onSelectToolSkill,
  onSelectWorkspace,
  onToolsSubChange,
  onToggleCollapsed,
  onToolsQueryChange,
  onUploadFiles,
}: {
  activeWorkspace: WorkspaceKey;
  artifacts: Artifact[];
  collapsed: boolean;
  capabilities: CapabilityDefinition[];
  capabilityCount: number;
  history: Array<{ id: string; title: string; time: string }>;
  mcpCount: number;
  mcpServers: MCPServer[];
  profiles: AgentProfile[];
  savedDags: UserDag[];
  selectedDagId: string;
  selectedProfileId: string;
  selectedToolCapabilityId: string;
  selectedToolMcpName: string;
  selectedToolSkillName: string;
  skills: SkillSummary[];
  skillCount: number;
  toolsSub: ToolDirectoryTab;
  toolsQuery: string;
  onCreateArtifact: () => void;
  onCreateMcp: () => void;
  onCreateTool: () => void;
  onDeleteArtifact: (artifactId: string) => void;
  onImportSkill: () => void;
  onLoadDag: (spec: UserDag) => void;
  onNewChat: () => void;
  onNewDag: () => void;
  onSelectProfile: (id: string) => void;
  onSelectToolCapability: (id: string) => void;
  onSelectToolMcp: (name: string) => void;
  onSelectToolSkill: (name: string) => void;
  onSelectWorkspace: (workspace: WorkspaceKey) => void;
  onToolsSubChange: (tab: ToolDirectoryTab) => void;
  onToggleCollapsed: () => void;
  onToolsQueryChange: (query: string) => void;
  onUploadFiles: (files: FileList | null) => void;
}) {
  const toolSubnav = [
    { key: 'tools' as const, label: '工具', icon: <Wrench size={16} />, count: capabilityCount },
    { key: 'skills' as const, label: '技能', icon: <FileText size={16} />, count: skillCount },
    { key: 'mcp' as const, label: 'MCP 服务', icon: <Database size={16} />, count: mcpCount },
  ];
  const normalizedToolsQuery = toolsQuery.trim().toLowerCase();
  const sidebarCapabilities = capabilities.filter((capability) => matchesCapabilityQuery(capability, normalizedToolsQuery));
  const sidebarSkills = skills.filter((skill) => matchesSkillQuery(skill, normalizedToolsQuery));
  const sidebarMcp = mcpServers.filter((server) =>
    !normalizedToolsQuery
    || `${server.name} ${server.config.command ?? ''} ${server.source}`.toLowerCase().includes(normalizedToolsQuery),
  );
  const activeToolSubnav = toolSubnav.find((item) => item.key === toolsSub) ?? toolSubnav[0];
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
    ? '新建工具'
    : toolsSub === 'skills'
      ? '导入技能'
      : '新建 MCP';

  return (
    <aside className="workspace-sidebar" data-collapsed={collapsed}>
      <div className="sidebar-brand-row">
        <button className="brand-mark" onClick={collapsed ? onToggleCollapsed : undefined} title={collapsed ? '展开侧栏' : 'dagent'} type="button">
          <GitBranch className="brand-logo-glyph" size={18} />
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
          if (item.key === 'tools') {
            return (
              <div className="sidebar-capability-nav" key={item.key}>
                <button
                  className={activeWorkspace === item.key ? 'active sidebar-capability-button' : 'sidebar-capability-button'}
                  onClick={() => onSelectWorkspace(item.key)}
                  title={item.label}
                  type="button"
                >
                  {item.icon}
                  <span>{item.label}</span>
                  <span className="sidebar-capability-chevron" data-open={activeWorkspace === 'tools'}>
                    <ChevronRight size={14} />
                  </span>
                </button>
                {activeWorkspace === 'tools' ? (
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

      {activeWorkspace === 'chat' ? (
        <section className="sidebar-history">
          <div className="sidebar-history-head">
            <span>历史对话</span>
            <button onClick={onNewChat} title="新建对话" type="button">
              <Plus size={14} />
            </button>
          </div>
          <div className="sidebar-history-list">
            {history.map((item, index) => (
              <button className={index === 0 ? 'active' : ''} key={item.id} type="button">
                <span>
                  <MessageSquare size={13} />
                  <strong>{item.title}</strong>
                </span>
                <em>{item.time}</em>
              </button>
            ))}
          </div>
        </section>
      ) : null}

      {activeWorkspace === 'orchestration' ? (
        <section className="sidebar-context-section">
          <div className="sidebar-history-head">
            <span>编排列表</span>
            <button onClick={onNewDag} title="新建编排" type="button">
              <Plus size={14} />
            </button>
          </div>
          <div className="sidebar-context-list">
            {savedDags.length ? savedDags.map((item) => (
              <button
                className={item.id === selectedDagId ? 'active' : ''}
                key={item.id}
                onClick={() => onLoadDag(item)}
                title={item.name || item.id}
                type="button"
              >
                <span>
                  <GitBranch size={13} />
                  <strong>{item.name || item.id}</strong>
                  <code>v{item.version ?? 1}</code>
                </span>
                <em>{item.description || `${item.nodes.length} 节点`}</em>
              </button>
            )) : (
              <div className="sidebar-empty-row">暂无编排</div>
            )}
          </div>
        </section>
      ) : null}

      {activeWorkspace === 'orchestration' ? (
        <section className="sidebar-artifact-section">
          <div className="sidebar-artifact-head">
            <span>Artifacts</span>
            <label className="sidebar-artifact-icon" title="上传文件">
              <Upload size={13} />
              <input
                type="file"
                multiple
                onChange={(event) => {
                  onUploadFiles(event.target.files);
                  event.currentTarget.value = '';
                }}
              />
            </label>
            <button className="sidebar-artifact-icon" onClick={onCreateArtifact} title="添加路径" type="button">
              <Plus size={13} />
            </button>
          </div>
          <div className="sidebar-artifact-list">
            {artifacts.length ? artifacts.map((artifact) => (
              <div className="sidebar-artifact-row" key={artifact.id}>
                <span>{artifactKindLabel(artifact)}</span>
                <strong title={artifactDisplayPath(artifact)}>
                  {artifactDisplayName(artifact)}
                </strong>
                <button onClick={() => onDeleteArtifact(artifact.id)} title="删除 artifact" type="button">
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
              {toolsSub === 'skills' ? <Upload size={14} /> : <Plus size={14} />}
            </button>
          </div>
          <label className="sidebar-search-field">
            <Search size={13} />
            <input
              value={toolsQuery}
              onChange={(event) => onToolsQueryChange(event.target.value)}
              placeholder="搜索…"
            />
          </label>
          <div className="sidebar-tool-list">
            {toolsSub === 'tools' ? (
              sidebarCapabilities.length ? sidebarCapabilities.map((capability) => (
                <button
                  className={selectedToolCapabilityId === capability.id ? 'active' : ''}
                  key={capability.id}
                  onClick={() => onSelectToolCapability(capability.id)}
                  type="button"
                >
                  <Wrench size={14} />
                  <span>{capability.id}</span>
                  <em data-enabled={capability.enabled} />
                </button>
              )) : <div className="sidebar-empty-row">没有匹配的工具</div>
            ) : toolsSub === 'skills' ? (
              sidebarSkills.length ? sidebarSkills.map((skill) => {
                const name = skillLookupName(skill);
                return (
                  <button
                    className={selectedToolSkillName === name ? 'active' : ''}
                    key={skill.path}
                    onClick={() => onSelectToolSkill(name)}
                    type="button"
                  >
                    <FileText size={14} />
                    <span>{name}</span>
                    <em data-enabled={skill.managed} />
                  </button>
                );
              }) : <div className="sidebar-empty-row">没有匹配的技能</div>
            ) : (
              sidebarMcp.length ? sidebarMcp.map((server) => (
                <button
                  className={selectedToolMcpName === server.name ? 'active' : ''}
                  key={server.name}
                  onClick={() => onSelectToolMcp(server.name)}
                  type="button"
                >
                  <Database size={14} />
                  <span>{server.name}</span>
                  <em data-enabled={server.status === 'connected'} />
                </button>
              )) : <div className="sidebar-empty-row">暂无 MCP 服务</div>
            )}
          </div>
        </section>
      ) : null}

      {activeWorkspace === 'agents' ? (
        <section className="sidebar-context-section agent-config-list">
          <div className="sidebar-history-head">
            <span>智能体配置</span>
            <button title="新建配置（暂未接入）" type="button" disabled>
              <Plus size={14} />
            </button>
          </div>
          <div className="sidebar-agent-list">
            {profiles.length ? profiles.map((profile) => (
              <button
                className={selectedProfileId === profile.id ? 'active' : ''}
                key={profile.id}
                onClick={() => onSelectProfile(profile.id)}
                type="button"
              >
                <span className="sidebar-agent-icon">
                  <Bot size={14} />
                </span>
                <span>
                  <strong>{profile.name}</strong>
                  <em>{profile.description || profilePathLabel(profile)}</em>
                </span>
              </button>
            )) : <div className="sidebar-empty-row">暂无智能体配置</div>}
          </div>
        </section>
      ) : null}

      <div className="sidebar-foot">
        <div className="workspace-root-chip">
          <span />
          <code>.dagent-runs</code>
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
  const label = workspace === 'chat' ? '智能对话' : workspacePlaceholderLabels[workspace];
  return (
    <section className="design-workspace-placeholder">
      <div>
        <div className="design-workspace-placeholder-icon">
          <GitBranch size={26} />
        </div>
        <strong>{label}</strong>
        <p>先评审「智能对话」这一版的视觉方案。确认方向后,我会把同一套设计语言铺到这个工作区。</p>
        <button onClick={onBackToChat} type="button">
          ← 返回智能对话
        </button>
      </div>
    </section>
  );
}

function ChatWorkspace({
  artifactPanelOpen,
  artifacts,
  chatScopeLabel,
  currentDag,
  draft,
  error,
  loading,
  messageListRef,
  messages,
  reviewLevel,
  selectedArtifact,
  selectedArtifactId,
  target,
  validationEnabled,
  validationError,
  validationPending,
  onArtifactSelect,
  onDraftChange,
  onOpenDag,
  onOpenScope,
  onReviewLevelChange,
  onRun,
  onStop,
  onTargetChange,
  onToggleArtifacts,
  onToggleValidation,
}: {
  artifactPanelOpen: boolean;
  artifacts: WorkbenchArtifactItem[];
  chatScopeLabel: string;
  currentDag: Dag;
  draft: string;
  error: string | null;
  loading: boolean;
  messageListRef: React.RefObject<HTMLDivElement | null>;
  messages: ChatMessage[];
  reviewLevel: ReviewLevel;
  selectedArtifact: WorkbenchArtifactItem | null;
  selectedArtifactId: string;
  target: ChatTarget;
  validationEnabled: boolean;
  validationError: string | null;
  validationPending: boolean;
  onArtifactSelect: (id: string) => void;
  onDraftChange: (value: string) => void;
  onOpenDag: (dag: Dag, trace?: TraceLogEvent[]) => void;
  onOpenScope: () => void;
  onReviewLevelChange: (value: ReviewLevel) => void;
  onRun: () => void;
  onStop: () => void;
  onTargetChange: (value: ChatTarget) => void;
  onToggleArtifacts: () => void;
  onToggleValidation: () => void;
}) {
  const title = currentChatTitle(messages);

  return (
    <section className={`chat-workspace ${artifactPanelOpen ? 'with-artifacts' : 'without-artifacts'}`}>
      <div className="chat-main">
        <div className="chat-scroll" ref={messageListRef}>
          <div className="conversation-frame">
            <div className="conversation-meta">
              <strong>{title}</strong>
              <span />
              <code>session · {messages.length} turns</code>
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
            <div className="composer-toolbar">
              <button
                className="icon-button attachment-button"
                title="上传附件（暂未接入）"
                aria-label="上传附件（暂未接入）"
                type="button"
              >
                <Upload size={17} />
              </button>
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
        artifacts={artifacts}
        open={artifactPanelOpen}
        selectedArtifact={selectedArtifact}
        selectedArtifactId={selectedArtifactId}
        onSelect={onArtifactSelect}
        onToggle={onToggleArtifacts}
      />
    </section>
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

function ArtifactPanel({
  artifacts,
  open,
  selectedArtifact,
  selectedArtifactId,
  onSelect,
  onToggle,
}: {
  artifacts: WorkbenchArtifactItem[];
  open: boolean;
  selectedArtifact: WorkbenchArtifactItem | null;
  selectedArtifactId: string;
  onSelect: (id: string) => void;
  onToggle: () => void;
}) {
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
    <aside className="artifact-drawer">
      <div className="artifact-drawer-head">
        <Folder size={17} />
        <strong>产物</strong>
        <span>{artifacts.length}</span>
        <button className="icon-button" title="刷新" type="button">
          <RefreshCw size={15} />
        </button>
        <button className="icon-button" onClick={onToggle} title="收起面板" type="button">
          <ChevronRight size={16} />
        </button>
      </div>

      <div className="artifact-file-label">
        <ChevronRight size={13} />
        <span>文件</span>
        <em>{artifacts.length}</em>
      </div>
      <div className="artifact-file-list">
        {artifacts.length ? artifacts.map((artifact) => (
          <button
            className={artifact.id === selectedArtifactId ? 'active' : ''}
            key={artifact.id}
            onClick={() => onSelect(artifact.id)}
            type="button"
          >
            <span className="artifact-extension">{artifact.extension}</span>
            <span>
              <strong>{artifact.name}</strong>
              <em>{artifact.meta}</em>
            </span>
          </button>
        )) : (
          <div className="artifact-empty">当前运行还没有产物。</div>
        )}
      </div>

      <div className="artifact-preview">
        {selectedArtifact ? (
          <>
            <div className="artifact-preview-head">
              <File size={14} />
              <strong>{selectedArtifact.name}</strong>
              <span>{selectedArtifact.meta}</span>
              <button className="icon-button" title="复制" type="button">
                <Copy size={13} />
              </button>
            </div>
            <pre>{artifactPreviewText(selectedArtifact)}</pre>
          </>
        ) : (
          <div className="artifact-preview-empty">选择一次运行产物后在这里预览。</div>
        )}
      </div>
    </aside>
  );
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
          <pre>{clipText(argsText, 800)}</pre>
        </div>
      ) : null}
      {resultContent ? (
        <div className="capability-section">
          <div className="capability-section-label">{showError ? 'Error' : 'Result'}</div>
          <pre>{clipText(resultContent, 1200)}</pre>
        </div>
      ) : null}
    </details>
  );
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

function currentChatHistory(messages: ChatMessage[]): Array<{ id: string; title: string; time: string }> {
  const title = currentChatTitle(messages);
  const userTurns = messages.filter((message) => message.role === 'user').length;
  return [{
    id: 'current',
    title,
    time: userTurns ? `${userTurns} turns` : '刚刚',
  }];
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
  capabilities,
  skills,
  mcpServers,
  mode,
  selectedCapabilityIds,
  selectedSkillNames,
  onModeChange,
  onCapabilityIdsChange,
  onSkillNamesChange,
  onClose,
}: {
  capabilities: CapabilityDefinition[];
  skills: SkillSummary[];
  mcpServers: MCPServer[];
  mode: ChatScopeMode;
  selectedCapabilityIds: string[];
  selectedSkillNames: string[];
  onModeChange: React.Dispatch<React.SetStateAction<ChatScopeMode>>;
  onCapabilityIdsChange: React.Dispatch<React.SetStateAction<string[]>>;
  onSkillNamesChange: React.Dispatch<React.SetStateAction<string[]>>;
  onClose: () => void;
}) {
  const [query, setQuery] = useState('');
  const selectedCapabilities = new Set(selectedCapabilityIds);
  const selectedSkills = new Set(selectedSkillNames);
  const normalizedQuery = query.trim().toLowerCase();
  const enabledCapabilities = capabilities.filter((capability) => capability.enabled);
  const visibleCapabilities = enabledCapabilities.filter((capability) => matchesCapabilityQuery(capability, normalizedQuery));
  const visibleSkills = skills.filter((skill) => matchesSkillQuery(skill, normalizedQuery));
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
            <p>{chatCapabilityScopeLabel(mode, selectedCapabilityIds.length, selectedSkillNames.length)}</p>
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
          </aside>
          <div className="capability-scope-list">
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
                      <strong>{capability.name}</strong>
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
            {!groups.length && !visibleSkills.length ? <div className="empty-state compact">No matching capabilities.</div> : null}
          </div>
        </div>
      </div>
    </div>
  );
}

function OrchestrationWorkspace({
  capabilities,
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
  const runSummary = buildRunDialogSummary(userDagFromRuntimeDag(spec, dag));
  const enabledCapabilities = visibleCapabilitiesForPicker(capabilities);
  const contextCapability = enabledCapabilities.find((capability) => capability.id === contextCapabilityId) ?? enabledCapabilities[0];
  const selectedNormalized = selectedNode ? normalizeNode(selectedNode) : null;
  const selectedInvocation = selectedNormalized && isCapabilityNode(selectedNormalized)
    ? selectedNormalized.payload.invocation
    : null;
  const selectedCapability = selectedInvocation
    ? capabilities.find((capability) => capability.id === selectedInvocation.capability_id)
    : null;
  const selectableCapabilities = selectedCapability && !enabledCapabilities.some((capability) => capability.id === selectedCapability.id)
    ? [selectedCapability, ...enabledCapabilities]
    : enabledCapabilities;
  const artifactItems = Object.values(spec.artifacts ?? {}).sort(compareArtifactsByPath);
  const flowPositionFromEvent = (event: MouseEvent | React.MouseEvent<Element>) =>
    flowInstance?.screenToFlowPosition({ x: event.clientX, y: event.clientY });

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
  const patchSelectedInvocation = (patch: Partial<CapabilityInvocation>) => {
    if (!selectedNode || !selectedInvocation) return;
    onPatchNode(selectedNode.id, {
      payload: {
        type: 'capability',
        invocation: { ...selectedInvocation, ...patch },
      },
    });
  };
  const patchArtifactList = (field: 'inputs' | 'outputs', artifactId: string, checked: boolean) => {
    if (!selectedNode || !selectedInvocation) return;
    const current = selectedNode[field] ?? [];
    const next = checked
      ? [...current.filter((id) => id !== artifactId), artifactId].sort()
      : current.filter((id) => id !== artifactId);
    if (field === 'inputs' && checked) {
      const boundary = selectedInvocation.boundary ?? {
        mode: 'read_only' as BoundaryMode,
        allowed_paths: ['.'],
        allowed_commands: [],
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

        <div className="orchestration-canvas">
          <ReactFlow
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
          </ReactFlow>
          {!nodes.length ? (
            <button className="orchestration-empty-canvas" onClick={() => onAddNode()} type="button">
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
            <div className="context-menu-title">{contextMenu.nodeId ? `节点：${contextMenu.nodeId}` : '画布'}</div>
            <label className="context-select">
              能力
              <select value={contextCapability?.id ?? ''} onChange={(event) => setContextCapabilityId(event.target.value)}>
                {enabledCapabilities.map((capability) => (
                  <option key={capability.id} value={capability.id}>
                    {capabilityDisplayName(capability)}
                  </option>
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
        <aside className="node-inspector" aria-label="节点检查器">
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
                        {capability.id}
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
                <div className="inspector-field">
                  <label>边界</label>
                  <select
                    value={selectedInvocation.boundary?.mode ?? 'read_only'}
                    onChange={(event) => patchSelectedInvocation({
                      boundary: {
                        ...(selectedInvocation.boundary ?? { allowed_paths: ['.'], allowed_commands: [] }),
                        mode: event.target.value as BoundaryMode,
                      },
                    })}
                  >
                    {boundaryModes.map((mode) => <option key={mode} value={mode}>{mode}</option>)}
                  </select>
                </div>
                <div className="inspector-field">
                  <InspectorArgumentEditor
                    value={selectedInvocation.arguments ?? {}}
                    parameters={selectedCapability?.parameters}
                    onChange={(argumentsValue) => patchSelectedInvocation({ arguments: argumentsValue })}
                  />
                </div>
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

function InspectorArgumentEditor({
  value,
  parameters,
  onChange,
}: {
  value: Record<string, unknown>;
  parameters?: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}) {
  const normalizedValue = ensureSchemaArguments(value, parameters);
  const fields = buildSchemaArgumentFields(value, parameters);
  const [mode, setMode] = useState<'kv' | 'raw'>('kv');
  const [rawText, setRawText] = useState(() => JSON.stringify(normalizedValue, null, 2));

  useEffect(() => {
    setRawText(JSON.stringify(ensureSchemaArguments(value, parameters), null, 2));
  }, [value, parameters]);

  const updateKey = (oldKey: string, nextKey: string) => {
    const cleanKey = nextKey.trim();
    if (!cleanKey || (cleanKey !== oldKey && Object.prototype.hasOwnProperty.call(normalizedValue, cleanKey))) return;
    const next: Record<string, unknown> = {};
    for (const [key, itemValue] of Object.entries(normalizedValue)) {
      next[key === oldKey ? cleanKey : key] = itemValue;
    }
    onChange(next);
  };
  const updateValue = (key: string, rawValue: string, type: ArgumentValueType) => {
    onChange({
      ...normalizedValue,
      [key]: parseArgumentValue(rawValue, type, normalizedValue[key]),
    });
  };
  const addField = () => {
    let index = Object.keys(normalizedValue).length + 1;
    let key = `arg_${index}`;
    while (Object.prototype.hasOwnProperty.call(normalizedValue, key)) {
      index += 1;
      key = `arg_${index}`;
    }
    onChange({ ...normalizedValue, [key]: '' });
  };
  const removeField = (key: string) => {
    const next = { ...normalizedValue };
    delete next[key];
    onChange(next);
  };
  const applyRawText = () => {
    const parsed = parseJsonObject(rawText);
    if (parsed) onChange(parsed);
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
                <input
                  value={formatArgumentValue(itemValue)}
                  onChange={(event) => updateValue(key, event.target.value, type)}
                  placeholder="value"
                  aria-label="参数值"
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
  const workspacePath = run?.workspace_path || '.dagent-runs';
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
  return (
    <div className="run-timeline-list">
      {rows.length ? rows.map((row, index) => (
        <details className={`run-timeline-row ${row.status}`} key={`${row.label}-${index}`} open={index === rows.length - 1}>
          <summary>
            <span>{row.label}</span>
            <code>{row.kind}</code>
            <ChevronRight size={15} />
          </summary>
          {row.detail ? <p>{row.detail}</p> : null}
        </details>
      )) : (
        <div className="run-timeline-empty">
          <span>{running ? '编排正在启动...' : state === 'ready' ? '点击「开始运行」启动编排' : '暂无运行事件'}</span>
        </div>
      )}
    </div>
  );
}

function runTimelineRow(item: RunTranscriptItem): { label: string; kind: string; detail: string; status: string } {
  if (item.type === 'text') {
    const content = item.content.trim();
    return {
      label: content.split('\n').find(Boolean)?.slice(0, 70) || '运行输出',
      kind: 'trace',
      detail: content,
      status: 'done',
    };
  }
  const event = item.event;
  const result = item.result;
  const failed = result?.type === 'capability.call.failed';
  return {
    label: event.capability_id ? `${event.capability_id}` : '能力调用',
    kind: event.type.includes('review') ? 'review' : 'tool',
    detail: JSON.stringify(event.arguments ?? {}, null, 2),
    status: failed ? 'failed' : result ? 'done' : 'running',
  };
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
    mode: 'read_only' as BoundaryMode,
    allowed_paths: ['.'],
    allowed_commands: [],
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
        <div className="two-col">
          <label>
            Boundary
            <select
              value={boundary.mode}
              onChange={(event) => patchInvocation({ boundary: { ...boundary, mode: event.target.value as BoundaryMode } })}
            >
              {boundaryModes.map((mode) => (
                <option key={mode} value={mode}>{mode}</option>
              ))}
            </select>
          </label>
          <label>
            Allowed Paths
            <BoundaryValueEditor
              values={boundary.allowed_paths ?? []}
              onChange={(allowedPaths) => patchInvocation({ boundary: { ...boundary, allowed_paths: allowedPaths } })}
            />
          </label>
        </div>
        <label>
          Allowed Commands
          <BoundaryValueEditor
            values={boundary.allowed_commands ?? []}
            onChange={(allowedCommands) => patchInvocation({ boundary: { ...boundary, allowed_commands: allowedCommands } })}
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
  activeTab,
  creationIntent,
  query,
  selectedCapabilityId,
  selectedMcpName,
  selectedSkillName,
  onActiveTabChange,
  onCreationIntentChange,
  onSelectedCapabilityIdChange,
  onSelectedMcpNameChange,
  onSelectedSkillNameChange,
  onRefresh,
}: {
  capabilities: CapabilityDefinition[];
  skills: SkillSummary[];
  mcpServers: MCPServer[];
  activeTab: ToolDirectoryTab;
  creationIntent: ToolDirectoryTab | null;
  query: string;
  selectedCapabilityId: string;
  selectedMcpName: string;
  selectedSkillName: string;
  onActiveTabChange: (tab: ToolDirectoryTab) => void;
  onCreationIntentChange: (tab: ToolDirectoryTab | null) => void;
  onSelectedCapabilityIdChange: (id: string) => void;
  onSelectedMcpNameChange: (name: string) => void;
  onSelectedSkillNameChange: (name: string) => void;
  onRefresh: () => Promise<void>;
}) {
  const [draftCapability, setDraftCapability] = useState<CapabilityDefinition>(defaultCustomCapability);
  const [draftParametersText, setDraftParametersText] = useState(JSON.stringify(defaultCustomCapability.parameters, null, 2));
  const [argumentsText, setArgumentsText] = useState('{"text":"hello"}');
  const [result, setResult] = useState<CapabilityResult | null>(null);
  const [message, setMessage] = useState('');
  const [skillDetail, setSkillDetail] = useState<SkillDetail | null>(null);
  const [skillFileDetail, setSkillFileDetail] = useState<SkillFileDetail | null>(null);
  const [skillMessage, setSkillMessage] = useState('');
  const [skillImport, setSkillImport] = useState({ name: '', description: '', category: '', content: '' });
  const [mcpDraft, setMcpDraft] = useState<{ name: string } & MCPServerConfig>(defaultMcpConfig);
  const [mcpArgsText, setMcpArgsText] = useState('');
  const [mcpEnvText, setMcpEnvText] = useState('');
  const [mcpMessage, setMcpMessage] = useState('');
  const normalizedQuery = query.toLowerCase();
  const toolRows = capabilities.filter((capability) => matchesCapabilityQuery(capability, normalizedQuery));
  const selectedTool = capabilities.find((capability) => capability.id === selectedCapabilityId) ?? toolRows[0] ?? capabilities[0];
  const selectedEditable = Boolean(selectedTool && isEditableToolCapability(selectedTool));
  const visibleSkills = skills.filter((skill) => matchesSkillQuery(skill, normalizedQuery));
  const selectedSkill = skills.find((skill) => skillLookupName(skill) === selectedSkillName) ?? visibleSkills[0] ?? skills[0];
  const linkedFileGroups = Object.entries(skillDetail?.linked_files ?? {})
    .filter(([, files]) => files.length);
  const selectedMcp = creationIntent === 'mcp'
    ? undefined
    : mcpServers.find((server) => server.name === selectedMcpName) ?? mcpServers[0];
  const creatingMcp = creationIntent === 'mcp';

  useEffect(() => {
    if (!selectedMcp) {
      setMcpDraft(defaultMcpConfig);
      setMcpArgsText('');
      setMcpEnvText('');
      return;
    }
    setMcpDraft({
      ...defaultMcpConfig,
      name: selectedMcp.name,
      ...selectedMcp.config,
    });
    setMcpArgsText((selectedMcp.config.args ?? []).join('\n'));
    setMcpEnvText(formatEnvText(selectedMcp.config.env ?? {}));
  }, [selectedMcp]);

  const runCreate = async () => {
    const parameters = parseJsonObject(draftParametersText);
    if (!parameters) {
      setMessage('Parameters must be a JSON object.');
      return;
    }
    if (!draftCapability.id.startsWith('tool.')) {
      setMessage('Tool ID must start with tool.');
      return;
    }
    setMessage('Creating tool...');
    try {
      const definition = {
        ...draftCapability,
        kind: 'tool' as const,
        parameters,
      };
      await createCapability(definition);
      await onRefresh();
      onSelectedCapabilityIdChange(definition.id);
      onCreationIntentChange(null);
      setMessage(`Created ${definition.id}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

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

  const openSkill = useCallback(async (skill: SkillSummary) => {
    const lookup = skillLookupName(skill);
    onSelectedSkillNameChange(lookup);
    setSkillMessage(`Loading ${lookup}...`);
    try {
      const detail = await getSkill(lookup);
      setSkillDetail(detail);
      setSkillFileDetail(null);
      setSkillMessage('');
    } catch (exc) {
      setSkillDetail(null);
      setSkillFileDetail(null);
      setSkillMessage(exc instanceof Error ? exc.message : String(exc));
    }
  }, [onSelectedSkillNameChange]);

  useEffect(() => {
    if (activeTab !== 'skills' || !selectedSkill) return;
    const lookup = skillLookupName(selectedSkill);
    if (skillDetail && skillLookupName(skillDetail.skill) === lookup) return;
    void openSkill(selectedSkill);
  }, [activeTab, openSkill, selectedSkill, skillDetail]);

  const openSkillLinkedFile = async (filePath: string) => {
    const skill = skillDetail?.skill ?? selectedSkill;
    if (!skill) return;
    const lookup = skillLookupName(skill);
    setSkillMessage(`Loading ${filePath}...`);
    try {
      const detail = await getSkillFile(lookup, filePath);
      setSkillFileDetail(detail);
      setSkillMessage('');
    } catch (exc) {
      setSkillFileDetail(null);
      setSkillMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const installSkillDraft = async () => {
    setSkillMessage('Installing skill...');
    try {
      const detail = await installSkill({
        content: skillImport.content,
        name: skillImport.name || undefined,
        description: skillImport.description || undefined,
        category: skillImport.category || undefined,
      });
      setSkillDetail(detail);
      setSkillFileDetail(null);
      onSelectedSkillNameChange(skillLookupName(detail.skill));
      onCreationIntentChange(null);
      setSkillMessage(`Installed ${skillLookupName(detail.skill)}.`);
      try {
        await onRefresh();
      } catch (exc) {
        setSkillMessage(`Installed ${skillLookupName(detail.skill)}, but refresh failed: ${exc instanceof Error ? exc.message : String(exc)}`);
      }
    } catch (exc) {
      setSkillMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const removeManagedSkill = async () => {
    const skill = skillDetail?.skill ?? selectedSkill;
    if (!skill || !isManagedSkill(skill)) return;
    setSkillMessage('Deleting skill...');
    try {
      await deleteSkill(skillLookupName(skill));
      setSkillDetail(null);
      setSkillFileDetail(null);
      onSelectedSkillNameChange('');
      setSkillMessage(`Deleted ${skillLookupName(skill)}.`);
      try {
        await onRefresh();
      } catch (exc) {
        setSkillMessage(`Deleted ${skillLookupName(skill)}, but refresh failed: ${exc instanceof Error ? exc.message : String(exc)}`);
      }
    } catch (exc) {
      setSkillMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const loadSkillFile = async (file: File | undefined) => {
    if (!file) return;
    if (file.name.toLowerCase().endsWith('.zip')) {
      setSkillMessage('Installing skill package...');
      try {
      const detail = await installSkill({ file });
      setSkillDetail(detail);
      setSkillFileDetail(null);
      onSelectedSkillNameChange(skillLookupName(detail.skill));
      onCreationIntentChange(null);
      setSkillMessage(`Installed ${skillLookupName(detail.skill)}.`);
      try {
        await onRefresh();
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
    };
    reader.readAsText(file);
  };

  const saveMcpServer = async () => {
    setMcpMessage('Saving MCP server...');
    try {
      const payload = {
        ...mcpDraft,
        args: linesFromText(mcpArgsText),
        env: parseEnvText(mcpEnvText),
      };
      const editingExistingMemoryServer = selectedMcp?.source === 'memory' && selectedMcp.name === payload.name;
      if (editingExistingMemoryServer) {
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
    if (!selectedMcp || selectedMcp.source !== 'memory') return;
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

  return (
    <section className={`design-tools-workspace ${activeTab === 'skills' ? 'skills-mode' : ''}`}>
      {activeTab === 'skills' ? (
        <aside className="tools-workspace-skill-tree">
          <div className="skill-tree-head">
            <div>
              <strong>{selectedSkill ? skillLookupName(selectedSkill) : 'skill'}</strong>
              <span>{skillDetail?.description || selectedSkill?.description || '选择技能查看文件'}</span>
            </div>
            <em>{skillDetail && isManagedSkill(skillDetail.skill) ? 'installed' : 'local'}</em>
          </div>
          <div className="skill-file-list">
            {skillDetail ? (
              <>
                <button
                  className={!skillFileDetail ? 'skill-file-row active' : 'skill-file-row'}
                  onClick={() => setSkillFileDetail(null)}
                  type="button"
                >
                  <FileText size={14} />
                  <span>SKILL.md</span>
                </button>
                {linkedFileGroups.map(([folder, files]) => (
                  <div className="skill-file-group" key={folder}>
                    <div>
                      <Folder size={14} />
                      <span>{folder}</span>
                    </div>
                    {files.map((filePath) => (
                      <button
                        className={skillFileDetail?.file_path === filePath ? 'skill-file-row active' : 'skill-file-row'}
                        key={filePath}
                        onClick={() => void openSkillLinkedFile(filePath)}
                        type="button"
                      >
                        <FileText size={14} />
                        <span>{filePath.split('/').pop() ?? filePath}</span>
                      </button>
                    ))}
                  </div>
                ))}
              </>
            ) : (
              <div className="sidebar-empty-row">正在加载技能内容</div>
            )}
            <label className="skill-add-file-button">
              <Plus size={12} />
              添加文件
              <input
                type="file"
                accept=".md,text/markdown,text/plain,.zip,application/zip"
                onChange={(event) => void loadSkillFile(event.target.files?.[0])}
              />
            </label>
          </div>
        </aside>
      ) : null}

      <aside className="tools-detail-panel">
        {activeTab === 'tools' ? (
          <div className="tools-detail-scroll">
            {selectedTool || creationIntent === 'tools' ? (
              <div className="tool-detail-surface">
                {selectedTool ? (
                  <>
                    <div className="tool-detail-head">
                      <div>
                        <h2>{selectedTool.id}</h2>
                        <p>{selectedTool.description || selectedTool.name}</p>
                      </div>
                      <button className="secondary-button compact-button" onClick={runTest} type="button">
                        <Play size={13} />
                        测试
                      </button>
                    </div>
                    <div className="tool-info-table">
                      <div><span>类型</span><strong>{selectedTool.kind}</strong></div>
                      <div><span>风险</span><strong><i className={`risk-chip risk-${selectedTool.policy.risk}`}>{selectedTool.policy.risk}</i></strong></div>
                      <div><span>边界</span><strong>{toolBoundaryLabel(selectedTool)}</strong></div>
                      <div><span>状态</span><strong>{capabilityStatusLabel(selectedTool)}</strong></div>
                    </div>
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
                        <button className="secondary-button compact-button" onClick={() => void toggleCapability(!selectedTool.enabled)} disabled={!selectedEditable} type="button">
                          {selectedTool.enabled ? '停用' : '启用'}
                        </button>
                        <button className="secondary-button danger-button compact-button" onClick={removeCapability} disabled={!selectedEditable} type="button">
                          删除
                        </button>
                      </div>
                      {message ? <p className="form-message">{message}</p> : null}
                      {result ? <pre className="tool-schema-block">{JSON.stringify(result, null, 2)}</pre> : null}
                    </section>
                  </>
                ) : (
                  <div className="empty-state compact">没有加载到工具。</div>
                )}
                {creationIntent === 'tools' ? (
                  <section className="tool-create-drawer">
                  <div className="section-title-row">
                    <span>新建工具</span>
                    <div className="inline-actions">
                      <button className="secondary-button compact-button" onClick={() => onCreationIntentChange(null)} type="button">
                        <X size={13} />
                        取消
                      </button>
                      <button className="primary-button compact-button" onClick={runCreate} type="button">
                        <Plus size={14} />
                        创建
                      </button>
                    </div>
                  </div>
                  <div className="compact-form-grid">
                    <label>ID<input value={draftCapability.id} onChange={(event) => setDraftCapability((current) => ({ ...current, id: event.target.value, kind: 'tool' }))} /></label>
                    <label>名称<input value={draftCapability.name} onChange={(event) => setDraftCapability((current) => ({ ...current, name: event.target.value, kind: 'tool' }))} /></label>
                    <label>描述<textarea value={draftCapability.description} onChange={(event) => setDraftCapability((current) => ({ ...current, description: event.target.value, kind: 'tool' }))} /></label>
                    <label>参数 Schema<textarea value={draftParametersText} onChange={(event) => setDraftParametersText(event.target.value)} /></label>
                    <label>Template<textarea value={String(draftCapability.config.template ?? '')} onChange={(event) => setDraftCapability((current) => ({ ...current, kind: 'tool', config: { ...current.config, template: event.target.value } }))} /></label>
                  </div>
                  </section>
                ) : null}
              </div>
            ) : <div className="empty-state compact">没有加载到工具。</div>}
          </div>
        ) : activeTab === 'skills' ? (
          <div className="skill-editor">
            <div className="skill-editor-toolbar">
              <FileText size={15} />
              <span>{selectedSkill ? skillLookupName(selectedSkill) : 'skill'} <em>/</em> <strong>{skillFileDetail?.file_path ?? 'SKILL.md'}</strong></span>
              <div>
                <button className="secondary-button danger-button compact-button" onClick={removeManagedSkill} disabled={!skillDetail || !isManagedSkill(skillDetail.skill)} type="button">
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
                value={skillFileDetail?.content ?? skillDetail?.content ?? ''}
                readOnly
                spellCheck={false}
              />
              {creationIntent === 'skills' ? (
                <section className="skill-import-panel">
                  <div className="section-title-row">
                    <span>导入技能</span>
                    <div className="inline-actions">
                      <button className="secondary-button compact-button" onClick={() => onCreationIntentChange(null)} type="button">
                        <X size={13} />
                        取消
                      </button>
                      <button className="primary-button compact-button" onClick={installSkillDraft} type="button">
                        <Upload size={14} />
                        安装
                      </button>
                    </div>
                  </div>
                  <div className="compact-form-grid">
                    <label>名称<input value={skillImport.name} onChange={(event) => setSkillImport((current) => ({ ...current, name: event.target.value }))} /></label>
                    <label>分类<input value={skillImport.category} onChange={(event) => setSkillImport((current) => ({ ...current, category: event.target.value }))} /></label>
                    <label>描述<textarea value={skillImport.description} onChange={(event) => setSkillImport((current) => ({ ...current, description: event.target.value }))} /></label>
                    <label>SKILL.md<textarea value={skillImport.content} onChange={(event) => setSkillImport((current) => ({ ...current, content: event.target.value }))} /></label>
                  </div>
                  {skillMessage ? <p className="form-message">{skillMessage}</p> : null}
                </section>
              ) : null}
            </div>
          </div>
        ) : (
          <div className="tools-detail-scroll">
            <div className="mcp-detail-surface">
              {selectedMcp || creatingMcp ? (
                <>
                  <div className="tool-detail-head">
                    <div>
                      <h2>{selectedMcp?.name ?? '新建 MCP 服务'}</h2>
                      <p>{selectedMcp ? `${selectedMcp.source} · ${selectedMcp.tools.length} tools` : '配置一个可连接的 MCP 服务'}</p>
                    </div>
                    {selectedMcp ? (
                      <span className="status-badge" data-status={selectedMcp.status === 'connected' ? 'completed' : selectedMcp.status === 'error' ? 'failed' : 'running'}>
                        {mcpStatusLabel(selectedMcp.status)}
                      </span>
                    ) : null}
                  </div>
                  {selectedMcp?.error ? <div className="error-banner">{selectedMcp.error}</div> : null}
                  <div className="mcp-config-form">
                    {creatingMcp ? (
                      <label>名称<input value={mcpDraft.name} onChange={(event) => setMcpDraft((current) => ({ ...current, name: event.target.value }))} /></label>
                    ) : null}
                    <label>命令<input value={mcpDraft.command} onChange={(event) => setMcpDraft((current) => ({ ...current, command: event.target.value }))} /></label>
                    <label>Args<textarea value={mcpArgsText} onChange={(event) => setMcpArgsText(event.target.value)} placeholder="每行一个参数" /></label>
                    <label>环境变量<textarea value={mcpEnvText} onChange={(event) => setMcpEnvText(event.target.value)} placeholder="KEY=value" /></label>
                    <div className="inline-actions">
                      {creatingMcp ? (
                        <button className="secondary-button compact-button" onClick={() => onCreationIntentChange(null)} type="button">
                          <X size={13} />
                          取消
                        </button>
                      ) : null}
                      <button className="primary-button compact-button" onClick={saveMcpServer} type="button">
                        <Save size={13} />
                        保存配置
                      </button>
                      {selectedMcp ? (
                        <>
                          <button className="secondary-button compact-button" onClick={() => void reloadMcp()} type="button">
                            <RefreshCw size={13} />
                            重载
                          </button>
                          <button className="secondary-button danger-button compact-button" onClick={removeMcpServer} disabled={selectedMcp.source !== 'memory'} type="button">
                            删除
                          </button>
                        </>
                      ) : null}
                    </div>
                  </div>
                  {selectedMcp ? (
                    <section>
                      <h3>发现的工具</h3>
                      <pre className="tool-schema-block">{JSON.stringify(selectedMcp.tools, null, 2)}</pre>
                    </section>
                  ) : null}
                  {mcpMessage ? <p className="form-message">{mcpMessage}</p> : null}
                </>
              ) : (
                <div className="empty-state compact">暂无 MCP 服务，点击左侧列表中的 + 新建。</div>
              )}
            </div>
          </div>
        )}
      </aside>
    </section>
  );
}

function profilePathLabel(profile: AgentProfile): string {
  return profile.source === 'builtin'
    ? `dagent/resources/profiles/${profile.name}.md`
    : `profiles/${profile.name}.md`;
}

function toolBoundaryLabel(capability: CapabilityDefinition): string {
  const configured = capability.config.boundary ?? capability.config.boundary_mode ?? capability.config.mode;
  if (typeof configured === 'string' && configured.trim()) return configured;
  if (capability.policy.sandbox_required) return 'sandbox';
  if (capability.policy.network) return 'network';
  return 'read_only';
}

function capabilityStatusLabel(capability: CapabilityDefinition): string {
  return capability.enabled ? '已启用' : '已停用';
}

function mcpStatusLabel(status: MCPServer['status']): string {
  if (status === 'connected') return 'connected';
  if (status === 'disabled') return 'disabled';
  if (status === 'error') return 'error';
  return 'pending';
}

function AgentManagementWorkspace({
  capabilities,
  profiles,
  warnings,
  selectedId,
  onSelect,
}: {
  capabilities: CapabilityDefinition[];
  profiles: AgentProfile[];
  warnings: ProfileWarning[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  const selected = profiles.find((profile) => profile.id === selectedId) ?? profiles[0] ?? null;
  const agentCapabilities = capabilities.filter((capability) => capability.kind === 'agent');
  const capabilityRows = agentCapabilities.length
    ? agentCapabilities
    : capabilities.filter((capability) => capability.enabled).slice(0, 3);

  useEffect(() => {
    if (!selected && profiles[0]) onSelect(profiles[0].id);
  }, [onSelect, profiles, selected]);

  return (
    <section className="design-agents-workspace">
      <div className="agent-prompt-editor">
        {selected ? (
          <>
            <div className="agent-editor-toolbar">
              <div className="agent-editor-icon">
                <Bot size={15} />
              </div>
              <div>
                <strong>{selected.name}</strong>
                <span>{profilePathLabel(selected)}</span>
              </div>
              <div>
                <button className="secondary-button compact-button" type="button" disabled title="后端暂未提供导入 profile 接口">
                  <Upload size={14} />
                  导入
                </button>
                <button className="primary-button compact-button" type="button" disabled title="后端暂未提供保存 profile 接口">
                  <Save size={14} />
                  保存
                </button>
              </div>
            </div>
            <div className="agent-editor-body">
              <div className="agent-editor-title-row">
                <span>系统提示词</span>
                <em>{selected.content.length} chars</em>
              </div>
              <textarea value={selected.content || ''} readOnly spellCheck={false} />
              <div className="agent-path-note">
                <AlertTriangle size={14} />
                配置文件路径：<code>{profilePathLabel(selected)}</code>
              </div>
            </div>
          </>
        ) : (
          <div className="empty-state compact">暂无智能体配置。</div>
        )}
      </div>

      <aside className="agent-metadata-panel">
        <div className="agent-panel-label">配置信息</div>
        {selected ? (
          <>
            <div className="agent-info-table">
              <div><span>名称</span><strong>{selected.name}</strong></div>
              <div><span>描述</span><strong>{selected.description || 'Markdown profile'}</strong></div>
              <div><span>来源</span><strong>{selected.source}</strong></div>
              <div><span>字符数</span><strong>{selected.content.length}</strong></div>
            </div>
            <div className="agent-panel-label">能力范围</div>
            <div className="agent-capability-list">
              {capabilityRows.length ? capabilityRows.map((capability) => (
                <div key={capability.id}>
                  <Wrench size={13} />
                  <span>{capability.id}</span>
                </div>
              )) : <div className="sidebar-empty-row">暂无可展示能力</div>}
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
            <button className="danger-line-button" type="button" disabled title="后端暂未提供删除 profile 接口">
              <Trash2 size={14} />
              删除配置
            </button>
          </>
        ) : null}
      </aside>
    </section>
  );
}

function chatCapabilityScopeLabel(mode: ChatScopeMode, capabilityCount: number, skillCount: number): string {
  if (mode === 'all') return '全部能力';
  const total = capabilityCount + skillCount;
  if (total === 0) return 'No capabilities';
  if (skillCount === 0) return `${capabilityCount} capabilities`;
  if (capabilityCount === 0) return `${skillCount} skills`;
  return `${capabilityCount} capabilities · ${skillCount} skills`;
}

function matchesCapabilityQuery(capability: CapabilityDefinition, query: string): boolean {
  if (!query) return true;
  const server = typeof capability.config?.server === 'string' ? capability.config.server : '';
  const haystack = `${capability.id} ${capability.name} ${capability.kind} ${capability.description} ${server}`.toLowerCase();
  return haystack.includes(query);
}

function matchesSkillQuery(skill: SkillSummary, query: string): boolean {
  if (!query) return true;
  const haystack = `${skill.name} ${skill.category ?? ''} ${skill.description} ${skill.path}`.toLowerCase();
  return haystack.includes(query);
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
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onNodeClick={(_, node) => onSelectNode(node.id)}
              fitView
              fitViewOptions={{ padding: 0.2 }}
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
    mode: 'read_only' as BoundaryMode,
    allowed_paths: ['.'],
    allowed_commands: [],
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
        <div className="two-col">
          <label>
            Boundary
            <select
              value={boundary.mode}
              onChange={(event) =>
                patchInvocation({ boundary: { ...boundary, mode: event.target.value as BoundaryMode } })
              }
            >
              {boundaryModes.map((mode) => (
                <option key={mode} value={mode}>
                  {mode}
                </option>
              ))}
            </select>
          </label>
          <label>
            Allowed Paths
            <BoundaryValueEditor
              values={boundary.allowed_paths ?? []}
              onChange={(allowedPaths) =>
                patchInvocation({ boundary: { ...boundary, allowed_paths: allowedPaths } })
              }
            />
          </label>
        </div>
        <label>
          Allowed Commands
          <BoundaryValueEditor
            values={boundary.allowed_commands ?? []}
            onChange={(allowedCommands) =>
              patchInvocation({ boundary: { ...boundary, allowed_commands: allowedCommands } })
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
