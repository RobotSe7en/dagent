import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Background,
  Controls,
  Edge,
  MiniMap,
  Node,
  ReactFlow,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type EdgeChange,
  type NodeChange,
} from '@xyflow/react';
import {
  AlertTriangle,
  Bot,
  Check,
  CircleStop,
  Database,
  FileText,
  FolderUp,
  GitBranch,
  Loader,
  MessageSquare,
  MessageSquarePlus,
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
  updateMcpServer,
  uploadDagArtifact,
} from './api';
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

const riskClass: Record<RiskLevel, string> = {
  low: 'risk-low',
  medium: 'risk-medium',
  high: 'risk-high',
};

const riskLevels: RiskLevel[] = ['low', 'medium', 'high'];
const boundaryModes: BoundaryMode[] = ['read_only', 'write_limited', 'full'];
const reviewLevels: ReviewLevel[] = ['fast', 'careful'];
const capabilityKinds: CapabilityKind[] = ['tool', 'mcp', 'skill', 'shell', 'agent', 'memory', 'file'];
const defaultWorkspaceRoot = '.dagent-runs';
const directoryInputProps = {
  directory: '',
  webkitdirectory: '',
} as React.InputHTMLAttributes<HTMLInputElement> & {
  directory: string;
  webkitdirectory: string;
};
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
  { key: 'orchestration', label: 'AI编排', icon: <GitBranch size={16} /> },
  { key: 'tools', label: '工具管理', icon: <Wrench size={16} /> },
  { key: 'agents', label: '智能体管理', icon: <UserCog size={16} /> },
];

function isCapabilityNode(node: DagNode): node is DagNode & { payload: CapabilityNodePayload } {
  return node.payload.type === 'capability';
}

function normalizeInvocation(invocation: CapabilityInvocation): CapabilityInvocation {
  return {
    ...invocation,
    capability_id: invocation.capability_id ?? '',
    kind: invocation.kind ?? 'tool',
    arguments: invocation.arguments ?? {},
    boundary: {
      mode: invocation.boundary?.mode ?? 'read_only',
      allowed_paths: invocation.boundary?.allowed_paths ?? [],
      allowed_commands: invocation.boundary?.allowed_commands ?? [],
    },
    risk: invocation.risk ?? 'low',
  };
}

function normalizeNode(node: DagNode): DagNode {
  if (!isCapabilityNode(node)) {
    return {
      ...node,
      payload: { type: 'start' },
      status: node.status ?? 'planned',
    };
  }
  return {
    ...node,
    payload: {
      type: 'capability',
      invocation: normalizeInvocation(node.payload.invocation),
    },
    status: node.status ?? 'planned',
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
          allowed_paths: [],
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

function capabilityDisplayName(capability: CapabilityDefinition): string {
  return `${capability.name} (${capability.id})`;
}

function capabilityRisk(capability?: CapabilityDefinition): RiskLevel {
  return capability?.policy?.risk ?? 'low';
}

interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  kind?: 'text';
  capabilityEvents?: CapabilityStreamEvent[];
  timeline?: MessageTimelineItem[];
  dagSnapshot?: Dag;
  traceSnapshot?: TraceLogEvent[];
}

type MessageTimelineItem =
  | { type: 'text'; content: string }
  | { type: 'dag'; dag: Dag }
  | { type: 'capability'; event: CapabilityStreamEvent; result?: CapabilityStreamEvent }
  | { type: 'validation'; event: ValidationFeedbackEvent }
  | { type: 'validating' };

type ChatTarget = 'auto' | 'tool' | 'dag';
type ChatScopeMode = 'all' | 'custom';

function graphFromDag(dag: Dag): { nodes: Node[]; edges: Edge[] } {
  const depths = nodeDepths(dag);
  const laneCounts = new Map<number, number>();
  const nodes = dag.nodes.map((rawItem) => {
    const item = normalizeNode(rawItem);
    const invocation = isCapabilityNode(item) ? item.payload.invocation : null;
    const risk = invocation?.risk ?? 'low';
    const status = item.status ?? 'planned';
    const depth = depths.get(item.id) ?? 0;
    const lane = laneCounts.get(depth) ?? 0;
    const detail = !invocation
      ? 'internal start'
      : invocation.capability_id
        ? `${invocation.capability_id} ${JSON.stringify(invocation.arguments)}`
        : 'capability not set';
    const detailTitle = invocation?.capability_id ? JSON.stringify(invocation.arguments) : '';
    laneCounts.set(depth, lane + 1);
    return {
      id: item.id,
      position: { x: 80 + depth * 300, y: 70 + lane * 170 },
      className: `status-${status}`,
      data: {
        label: (
          <div className={`dag-node dag-node-status-${status}`}>
            <div className="dag-node-top">
              <span title={item.id}>{item.id}</span>
              <span className={`risk-pill ${riskClass[risk]}`}>{risk}</span>
            </div>
            <div
              className="dag-node-tools"
              title={invocation ? detailTitle : 'Internal DAG start node'}
            >
              {detail}
            </div>
          </div>
        ),
      },
      type: 'default',
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

function isDagConfirmable(dag: Dag): boolean {
  return !['completed', 'failed', 'aborted', 'running'].includes(dag.status);
}

export function App() {
  const [activeWorkspace, setActiveWorkspace] = useState<WorkspaceKey>('chat');
  const [dag, setDag] = useState<Dag>(emptyDag);
  const [selectedId, setSelectedId] = useState<string>('');
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'assistant',
      content: 'Enter a task, and I will either use tools directly or create and execute a DAG plan when orchestration is useful. Auto chooses for you.',
    },
  ]);
  const [draft, setDraft] = useState('');
  const [target, setTarget] = useState<ChatTarget>('auto');
  const [reviewLevel, setReviewLevel] = useState<ReviewLevel>('fast');
  const [chatScopeMode, setChatScopeMode] = useState<ChatScopeMode>('all');
  const [selectedChatCapabilityIds, setSelectedChatCapabilityIds] = useState<string[]>([]);
  const [selectedChatSkillNames, setSelectedChatSkillNames] = useState<string[]>([]);
  const [capabilityScopeOpen, setCapabilityScopeOpen] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [trace, setTrace] = useState<TraceLogEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [validationEnabled, setValidationEnabled] = useState(false);
  const [validationPending, setValidationPending] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [dagReview, setDagReview] = useState<ReviewEventPayload | null>(null);
  const [capabilityReview, setCapabilityReview] = useState<ReviewEventPayload | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const validationRequestIdRef = useRef(0);
  const tokenQueueRef = useRef<string[]>([]);
  const tokenTimerRef = useRef<number | null>(null);
  const tokenDrainResolversRef = useRef<Array<() => void>>([]);
  const [capabilities, setCapabilities] = useState<CapabilityDefinition[]>([]);
  const [consoleError, setConsoleError] = useState<string | null>(null);
  const [savedDags, setSavedDags] = useState<UserDag[]>([]);
  const [editorUserDag, setEditorUserDag] = useState<UserDag>(() => createEmptyUserDag());
  const [editorDag, setEditorDag] = useState<Dag>(() => runtimeDagFromUserDag(editorUserDag));
  const [editorSelectedId, setEditorSelectedId] = useState('');
  const [editorTrace, setEditorTrace] = useState<TraceLogEvent[]>([]);
  const [editorRun, setEditorRun] = useState<DagRun | null>(null);
  const [editorRunTimeline, setEditorRunTimeline] = useState<RunTranscriptItem[]>([]);
  const [editorMessage, setEditorMessage] = useState('');
  const [editorRunning, setEditorRunning] = useState(false);
  const [editorWorkspaceRoot, setEditorWorkspaceRoot] = useState(defaultWorkspaceRoot);
  const [profiles, setProfiles] = useState<AgentProfile[]>([]);
  const [profileWarnings, setProfileWarnings] = useState<ProfileWarning[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState('');
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [mcpServers, setMcpServers] = useState<MCPServer[]>([]);

  const chatScopeLabel = chatCapabilityScopeLabel(
    chatScopeMode,
    selectedChatCapabilityIds.length,
    selectedChatSkillNames.length,
  );

  const selectedNode = dag.nodes.find((node) => node.id === selectedId) ?? dag.nodes[0];
  const graph = useMemo(() => graphFromDag(dag), [dag]);
  const [nodes, setNodes] = useState<Node[]>(graph.nodes);
  const [edges, setEdges] = useState<Edge[]>(graph.edges);
  const editorGraph = useMemo(() => graphFromDag(editorDag), [editorDag]);
  const [editorNodes, setEditorNodes] = useState<Node[]>(editorGraph.nodes);
  const [editorEdges, setEditorEdges] = useState<Edge[]>(editorGraph.edges);

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
    const availableSkills = new Set(skills.map((skill) => skillLookupName(skill)));
    setSelectedChatSkillNames((items) => items.filter((name) => availableSkills.has(name)));
  }, [skills]);

  useEffect(() => {
    const element = messageListRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
  }, [messages, streaming]);

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
    const nextGraph = graphFromDag(nextDag);
    setEditorNodes(nextGraph.nodes);
    setEditorEdges(nextGraph.edges);
    setEditorSelectedId((current) =>
      nextDag.nodes.some((node) => node.id === current) ? current : nextDag.nodes[0]?.id ?? '',
    );
  }, []);

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

  const upsertEditorArtifact = (artifact: Artifact, previousId?: string) => {
    const nextArtifactId = artifact.id.trim();
    if (!nextArtifactId) return;
    const normalizedArtifact = { ...artifact, id: nextArtifactId };
    const nextArtifacts = upsertArtifact(editorUserDag.artifacts ?? {}, normalizedArtifact, previousId);
    const nextNodes = previousId && previousId !== nextArtifactId
      ? editorDag.nodes.map((node) => ({
          ...node,
          inputs: (node.inputs ?? []).map((id) => (id === previousId ? nextArtifactId : id)),
          outputs: (node.outputs ?? []).map((id) => (id === previousId ? nextArtifactId : id)),
        }))
      : editorDag.nodes;
    const nextSpec = {
      ...userDagFromRuntimeDag(editorUserDag, { ...editorDag, nodes: nextNodes }),
      artifacts: nextArtifacts,
    };
    setEditorUserDag(nextSpec);
    syncEditorDag(runtimeDagFromUserDag(nextSpec));
  };

  const deleteEditorArtifact = (artifactId: string) => {
    const nextSpec = removeArtifactBinding(userDagFromRuntimeDag(editorUserDag, editorDag), artifactId);
    setEditorUserDag(nextSpec);
    syncEditorDag(runtimeDagFromUserDag(nextSpec));
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
      timeline: appendTextTimeline(message.timeline, content),
    }));
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

    const chunk = next.slice(0, 14);
    const rest = next.slice(14);
    if (rest) {
      tokenQueueRef.current.unshift(rest);
    }
    appendAssistantContent(chunk);
  };

  const flushQueuedTokensNow = () => {
    const pending = tokenQueueRef.current.join('');
    tokenQueueRef.current = [];
    stopTokenTimer();
    resolveTokenDrain();
    if (pending) appendAssistantContent(pending);
  };

  const ensureTokenTimer = () => {
    if (tokenTimerRef.current !== null) return;
    tokenTimerRef.current = window.setInterval(flushTokenQueue, 24);
  };

  const enqueueAssistantToken = (content: string) => {
    if (!content) return;
    const shouldFlushImmediately = tokenQueueRef.current.length === 0 && tokenTimerRef.current === null;
    tokenQueueRef.current.push(content);
    if (shouldFlushImmediately) {
      flushTokenQueue();
    }
    ensureTokenTimer();
  };

  const enqueueFinalAnswer = (finalAnswer: string) => {
    if (!finalAnswer) return;
    tokenQueueRef.current.push(finalAnswer);
    ensureTokenTimer();
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
    updateLastAssistantText((message) => ({
      ...message,
      dagSnapshot: nextDag,
      timeline: upsertDagTimeline(message.timeline, nextDag),
      traceSnapshot: message.traceSnapshot,
    }));
  };

  const shouldOpenDagReview = (nextDag: Dag, pendingReview?: unknown) =>
    Boolean(pendingReview) || nextDag.status === 'review_required';

  const handlePendingReview = (pendingReview?: ReviewEventPayload | null) => {
    if (!pendingReview) return;
    if (pendingReview.kind === 'capability_review') {
      setCapabilityReview(pendingReview as ReviewEventPayload);
      return;
    }
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
    updateLastAssistantText((message) => ({
      ...message,
      timeline: [...(message.timeline ?? []), { type: 'validation', event }],
    }));
  };

  const appendValidating = () => {
    flushQueuedTokensNow();
    updateLastAssistantText((message) => ({
      ...message,
      timeline: [...(message.timeline ?? []), { type: 'validating' }],
    }));
  };

  const appendCapabilityMessage = (event: CapabilityStreamEvent) => {
    if (event.type === 'capability.call.completed' && event.content?.startsWith('[PENDING_REVIEW]')) return;
    flushQueuedTokensNow();
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
  const onEditorNodesChange = useCallback((changes: NodeChange[]) => setEditorNodes((nds) => applyNodeChanges(changes, nds)), []);
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
                  allowed_paths: [],
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
  };

  const loadEditorUserDag = (spec: UserDag) => {
    setEditorUserDagAndRuntimeDag(spec);
  };

  const addEditorNode = (capability?: CapabilityDefinition) => {
    const selectedCapability = capability ?? capabilities.find((item) => item.enabled);
    const id = uniqueNodeId(editorDag);
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
                allowed_paths: [],
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

  const persistEditorUserDag = async (): Promise<boolean> => {
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    const validation = validateUserDagDraft(spec);
    if (validation) {
      setEditorMessage(validation);
      return false;
    }
    setEditorMessage('Saving DAG...');
    try {
      const saved = await saveDag(spec);
      setEditorUserDagAndRuntimeDag(saved);
      await refreshConsoleData();
      setEditorMessage(`Saved ${saved.name || 'DAG'}.`);
      return true;
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
      return false;
    }
  };

  const uploadEditorArtifact = async (artifactId: string, fileList: FileList | null) => {
    const files = Array.from(fileList ?? []);
    if (!files.length) return;
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    const validation = validateUserDagDraft(spec);
    if (validation) {
      setEditorMessage(validation);
      return;
    }
    setEditorMessage(`Uploading ${files.length} file${files.length === 1 ? '' : 's'}...`);
    try {
      const saved = await saveDag(spec);
      setEditorUserDagAndRuntimeDag(saved);
      await uploadDagArtifact(saved.id, artifactId, files);
      await refreshConsoleData();
      setEditorMessage(`Uploaded ${files.length} file${files.length === 1 ? '' : 's'}.`);
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const uploadEditorFiles = async (fileList: FileList | null) => {
    const files = Array.from(fileList ?? []);
    if (!files.length) return;
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
    const uploadRoot = uploadBatchRoot(files);
    const uploadDraft = createUploadedFileArtifacts(uploadSourceFiles(files), {
      artifacts: spec.artifacts ?? {},
      uploadRoot,
    });
    const nextSpec = {
      ...spec,
      artifacts: uploadDraft.artifacts,
    };
    const validation = validateUserDagDraft(nextSpec);
    if (validation) {
      setEditorMessage(validation);
      return;
    }
    setEditorMessage(`Uploading ${files.length} file${files.length === 1 ? '' : 's'}...`);
    try {
      const saved = await saveDag(nextSpec);
      setEditorUserDagAndRuntimeDag(saved);
      for (let index = 0; index < uploadDraft.uploads.length; index += 1) {
        await uploadDagArtifact(
          saved.id,
          uploadDraft.uploads[index].artifact.id,
          [files[index]],
          { preserveRelativePath: false },
        );
      }
      await refreshConsoleData();
      setEditorMessage(`Uploaded ${files.length} file${files.length === 1 ? '' : 's'} to ${uploadRoot}.`);
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const runEditorSpec = async () => {
    if (editorRunning) return;
    const spec = userDagFromRuntimeDag(editorUserDag, editorDag);
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
        onStatus: (status) => {
          setEditorTrace((items) => [
            ...items,
            {
              id: crypto.randomUUID(),
              type: 'model',
              label: status,
              detail: 'DAG run event.',
              status: 'running',
              timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
            },
          ]);
        },
        onTrace: (event) => {
          setEditorTrace((items) => [...items, event]);
        },
        onCapability: (event) => {
          setEditorRunTimeline((items) => appendRunTranscriptCapability(items, event));
          setEditorTrace((items) => [
            ...items,
            {
              id: `${event.invocation_id}-${event.type}-${items.length}`,
              type: 'capability',
              label: event.capability_id,
              detail: event.content || JSON.stringify(event.arguments ?? {}),
              status: event.type === 'capability.call.failed' ? 'failed' : event.type === 'capability.call.completed' ? 'completed' : 'running',
              timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
            },
          ]);
        },
        onToken: (content) => {
          setEditorRunTimeline((items) => appendRunTranscriptToken(items, content));
        },
        onDone: (payload) => {
          const dagRun = payload.result.dag_run;
          if (!dagRun) return;
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
      }, { workspaceRoot: editorWorkspaceRoot });
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
        onStatus: (status) => appendTrace({ type: 'model', label: status, detail: 'HarnessRuntime request accepted.', status: 'running' }),
        onDag: (nextDag) => {
          flushQueuedTokensNow();
          syncDag(nextDag);
          attachDagToLastAssistant(nextDag);
          if (shouldOpenDagReview(nextDag)) setReviewOpen(true);
        },
        onTrace: appendRuntimeTrace,
        onCapability: appendCapabilityMessage,
        onToken: enqueueAssistantToken,
        onRetry: appendValidationFeedback,
        onValidating: appendValidating,
        onDone: (payload) => {
          const result = payload.result;
          flushQueuedTokensNow();
          if (result.dag) {
            syncDag(result.dag);
            attachDagToLastAssistant(result.dag);
            if (shouldOpenDagReview(result.dag, result.pending_review)) setReviewOpen(true);
            appendTrace({ type: 'dag', label: 'dag_generated', detail: `Generated ${result.dag.nodes.length} node(s).`, status: 'completed' });
          }
          handlePendingReview(result.pending_review);
          enqueueFinalAnswer(result.output_text);
          appendTrace({
            type: 'model',
            label: 'runtime_completed',
            detail: result.dag ? 'DAG loop completed the request.' : 'Capability loop completed the request.',
            status: result.status === 'failed' ? 'failed' : 'completed',
          });
        },
        onError: (message) => {
          setError(message);
          appendTrace({ type: 'model', label: 'dag_agent_failed', detail: message, status: 'failed' });
        },
      }, capabilityScope);
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
    stopTokenTimer();
    setStreaming(true);
    setMessages((items) => [
      ...items,
      { role: 'assistant', kind: 'text', content: '' },
    ]);
    const reviewId = dagReview.review_id;
    setDagReview(null);
    appendTrace({
      type: 'dag',
      label: approved ? 'dag_confirmed' : 'dag_rejected',
      detail: `${approved ? 'Approving' : 'Rejecting'} review ${reviewId}.`,
      status: 'running',
    });

    try {
      await resumeDagReview(reviewId, approved ? dag : null, reviewLevel, approved, {
        onStatus: (status) => appendTrace({ type: 'model', label: status, detail: 'HarnessRuntime resumed from DAG review.', status: 'running' }),
        onDag: (nextDag) => {
          syncDag(nextDag);
          attachDagToLastAssistant(nextDag);
        },
        onTrace: appendRuntimeTrace,
        onCapability: appendCapabilityMessage,
        onToken: enqueueAssistantToken,
        onRetry: appendValidationFeedback,
        onValidating: appendValidating,
        onDone: (payload) => {
          const result = payload.result;
          flushQueuedTokensNow();
          if (result.dag) {
            syncDag(result.dag);
            attachDagToLastAssistant(result.dag);
            if (shouldOpenDagReview(result.dag, result.pending_review)) setReviewOpen(true);
          }
          handlePendingReview(result.pending_review);
          enqueueFinalAnswer(result.output_text);
          appendTrace({ type: 'model', label: 'runtime_completed', detail: 'DAG loop completed the request.', status: 'completed' });
        },
        onError: (message) => {
          setError(message);
          appendTrace({ type: 'model', label: 'resume_failed', detail: message, status: 'failed' });
        },
      });
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
    setCapabilityReview(null);
    setError(null);
    tokenQueueRef.current = [];
    stopTokenTimer();
    setStreaming(true);
    setMessages((items) => [
      ...items,
      { role: 'assistant', kind: 'text', content: '' },
    ]);
    appendTrace({ type: 'model', label: 'capability_review_resumed', detail: `Capability review ${approved ? 'approved' : 'rejected'}.`, status: 'running' });

    try {
      await resumeCapabilityReview(capabilityReview.review_id, approved, {
        onStatus: (status) => appendTrace({ type: 'model', label: status, detail: 'Capability loop resumed from capability review.', status: 'running' }),
        onToken: enqueueAssistantToken,
        onRetry: appendValidationFeedback,
        onValidating: appendValidating,
        onDone: (payload) => {
          flushQueuedTokensNow();
          handlePendingReview(payload.result.pending_review);
          enqueueFinalAnswer(payload.result.output_text);
          appendTrace({ type: 'model', label: 'runtime_completed', detail: 'Capability loop completed the request.', status: 'completed' });
        },
        onError: (message) => {
          setError(message);
          appendTrace({ type: 'model', label: 'capability_review_failed', detail: message, status: 'failed' });
        },
      });
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
      setCapabilityReview(null);
    } catch (exc) {
      setValidationError(exc instanceof Error ? exc.message : String(exc));
    }
    setMessages([{
      role: 'assistant',
      content: 'Enter a task, and I will either use tools directly or create and execute a DAG plan when orchestration is useful. Auto chooses for you.',
    }]);
    setDraft('');
    syncDag(emptyDag);
    setTrace([]);
    setError(null);
    setReviewOpen(false);
    tokenQueueRef.current = [];
    stopTokenTimer();
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <div className="brand">
            <GitBranch size={20} />
            <span>dagent</span>
          </div>
          <p>Human-reviewed Agent DAG Harness</p>
        </div>
        <nav className="workspace-nav" aria-label="Workspace navigation">
          {workspaceItems.map((item) => (
            <button
              key={item.key}
              className={activeWorkspace === item.key ? 'active' : ''}
              type="button"
              onClick={() => setActiveWorkspace(item.key)}
            >
              {item.icon}
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
      </header>

      <main className="workspace">
        {consoleError ? <div className="error-banner global-error">{consoleError}</div> : null}
        {activeWorkspace === 'chat' ? (
          <section className="chat-pane">
            <PaneTitle icon={<Bot size={18} />} title="智能对话" />
            {error ? <div className="error-banner">{error}</div> : null}
            <div className="message-list" ref={messageListRef}>
              {messages.map((message, index) => (
                <div key={`${message.role}-${index}`} className={`message ${message.role} ${message.kind ?? 'text'}`}>
                  <span>{message.role}</span>
                  <MessageTimeline
                    message={message}
                    loading={streaming}
                    onOpenDag={(snapshot, snapshotTrace) => {
                      syncDag(snapshot);
                      if (snapshotTrace) setTrace(snapshotTrace);
                      setReviewOpen(true);
                    }}
                  />
                </div>
              ))}
            </div>
            <div className="composer">
              <textarea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) runStream();
                }}
                placeholder="Ask for a plan, review, or execution result"
              />
              <div className="composer-bar">
                <div className="composer-controls">
                  <button className="icon-button" onClick={newChat} disabled={streaming} title="New chat" type="button">
                    <MessageSquarePlus size={18} />
                  </button>
                  <div className="mode-switch" aria-label="Agent target">
                    {(['auto', 'dag', 'tool'] as ChatTarget[]).map((item) => (
                      <button
                        key={item}
                        className={target === item ? 'active' : ''}
                        onClick={() => setTarget(item)}
                        type="button"
                      >
                        {item}
                      </button>
                    ))}
                  </div>
                  <select
                    className="review-select"
                    value={reviewLevel}
                    onChange={(event) => setReviewLevel(event.target.value as ReviewLevel)}
                    aria-label="Review level"
                  >
                    {reviewLevels.map((level) => (
                      <option key={level} value={level}>
                        {level}
                      </option>
                    ))}
                  </select>
                  <button
                    className={`validation-toggle ${validationEnabled ? 'active' : ''} ${validationError ? 'error' : ''}`}
                    type="button"
                    onClick={toggleValidation}
                    disabled={validationPending}
                    title={validationError ?? 'Validate final answers against the user request'}
                    aria-pressed={validationEnabled}
                  >
                    {validationPending ? 'Validation saving' : validationEnabled ? 'Validation on' : validationError ? 'Validation error' : 'Validation off'}
                  </button>
                  <button
                    className={`secondary-button compact-button scope-button ${chatScopeMode === 'custom' ? 'active' : ''}`}
                    onClick={() => setCapabilityScopeOpen(true)}
                    title="Select chat capabilities"
                    type="button"
                  >
                    <SlidersHorizontal size={16} />
                    {chatScopeLabel}
                  </button>
                  {dag.nodes.length ? (
                    <>
                      <StatusBadge status={dag.status} />
                      <button className="secondary-button compact-button" onClick={() => setReviewOpen(true)} type="button">
                        <GitBranch size={16} />
                        Review DAG
                      </button>
                    </>
                  ) : null}
                </div>
                <div className="composer-actions">
                  <button className="icon-button" onClick={stopStream} disabled={!streaming} title="Stop stream" type="button">
                    <CircleStop size={18} />
                  </button>
                  <button className="primary-button" onClick={runStream} disabled={streaming} type="button">
                    <Send size={17} />
                    Send
                  </button>
                </div>
              </div>
            </div>
          </section>
        ) : activeWorkspace === 'orchestration' ? (
          <OrchestrationWorkspace
            capabilities={capabilities}
            savedDags={savedDags}
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
            workspaceRoot={editorWorkspaceRoot}
            onNew={newEditorUserDag}
            onLoad={loadEditorUserDag}
            onPatchDag={patchEditorUserDag}
            onWorkspaceRootChange={setEditorWorkspaceRoot}
            onUpsertArtifact={upsertEditorArtifact}
            onDeleteArtifact={deleteEditorArtifact}
            onUploadArtifact={uploadEditorArtifact}
            onUploadFiles={uploadEditorFiles}
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
            onRefresh={refreshConsoleData}
          />
        ) : (
          <AgentDirectory
            profiles={profiles}
            warnings={profileWarnings}
            selectedId={selectedProfileId}
            onSelect={setSelectedProfileId}
          />
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
          onApprove={() => confirmCapabilityReview(true)}
          onReject={() => confirmCapabilityReview(false)}
          onClose={() => setCapabilityReview(null)}
        />
      ) : null}
    </div>
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
        ) : item.type === 'validation' ? (
          <ValidationFeedbackCard key={`validation-${index}`} event={item.event} />
        ) : item.type === 'validating' ? (
          <ValidatingIndicator key={`validating-${index}`} />
        ) : item.content ? (
          <MessageContent key={`text-${index}`} content={item.content} />
        ) : null,
      )}
      {!message.content && loading ? <MessageContent content="..." /> : null}
    </div>
  );
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
          <details key={`${part.type}-${index}`} className="think-block" open={!part.closed}>
            <summary>Thinking</summary>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{part.content || '...'}</ReactMarkdown>
          </details>
        ) : looksLikeDsl(part.content) ? null : (
          <ReactMarkdown key={`${part.type}-${index}`} remarkPlugins={[remarkGfm]}>{part.content}</ReactMarkdown>
        ),
      )}
    </div>
  );
}

function appendTextTimeline(
  timeline: MessageTimelineItem[] | undefined,
  content: string,
): MessageTimelineItem[] {
  if (!content) return timeline ?? [];
  const items = [...(timeline ?? [])];
  const last = items[items.length - 1];
  if (last?.type === 'text') {
    items[items.length - 1] = { type: 'text', content: `${last.content}${content}` };
    return items;
  }
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.type === 'text' && hasUnclosedThink(item.content)) {
      items[i] = { type: 'text', content: `${item.content}${content}` };
      return items;
    }
  }
  items.push({ type: 'text', content });
  return items;
}

function hasUnclosedThink(content: string): boolean {
  return (content.match(/<think>/g) || []).length > (content.match(/<\/think>/g) || []).length;
}

function ensureTextTimeline(
  timeline: MessageTimelineItem[] | undefined,
  content: string,
): MessageTimelineItem[] {
  if (!content) return timeline ?? [];
  const items = timeline ?? [];
  const last = items[items.length - 1];
  if (last?.type === 'text' && last.content.trim()) {
    return items;
  }
  return appendTextTimeline(timeline, content);
}

function upsertDagTimeline(
  timeline: MessageTimelineItem[] | undefined,
  dag: Dag,
): MessageTimelineItem[] {
  const items = [...(timeline ?? [])];
  const dagKey = dag.task_id || dag.dag_id;
  const existingIndex = items.findIndex(
    (item) => item.type === 'dag' && (item.dag.task_id || item.dag.dag_id) === dagKey && item.dag.version === dag.version,
  );
  if (existingIndex !== -1) {
    items[existingIndex] = { type: 'dag', dag };
    return items;
  }
  const last = items[items.length - 1];
  if (last?.type === 'dag' && (last.dag.task_id || last.dag.dag_id) === dagKey && last.dag.version === dag.version) {
    items[items.length - 1] = { type: 'dag', dag };
  } else {
    items.push({ type: 'dag', dag });
  }
  return items;
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

function StatusBadge({ status }: { status: Dag['status'] }) {
  return <span className="status-badge" data-status={status}>{status}</span>;
}

function DagSummaryCard({
  dag,
  onOpen,
}: {
  dag: Dag;
  onOpen: () => void;
}) {
  const riskyNodes = dag.nodes.filter((node) => isCapabilityNode(node) && node.payload.invocation.risk !== 'low').length;
  const actionLabel = isDagConfirmable(dag) ? 'open review' : 'view flow';
  return (
    <button className="dag-summary-card" onClick={onOpen} type="button">
      <div className="dag-summary-head">
        <GitBranch size={17} />
        <strong>{dag.task_id || dag.dag_id}</strong>
        <StatusBadge status={dag.status} />
      </div>
      <div className="dag-summary-stats">
        <span>{dag.nodes.length} nodes</span>
        <span>{dag.edges.length} edges</span>
        <span>{riskyNodes} review</span>
        <span>{actionLabel}</span>
      </div>
    </button>
  );
}

function ValidatingIndicator() {
  return (
    <details className="timeline-card">
      <summary className="timeline-card-head">
        <Loader size={14} />
        <strong>Validating result quality...</strong>
        <span>validating</span>
      </summary>
    </details>
  );
}

function ValidationFeedbackCard({ event }: { event: ValidationFeedbackEvent }) {
  const passed = event.type === 'validation.passed' || event.passed === true;
  return (
    <details className={`timeline-card ${passed ? 'validation-passed' : 'validation-feedback'}`}>
      <summary className="timeline-card-head">
        {passed ? <Check size={14} /> : <AlertTriangle size={14} />}
        <strong>Validation {passed ? 'Passed' : 'Feedback'}</strong>
        <span>{passed ? 'passed' : 'retry'}</span>
      </summary>
      {event.summary ? (
        <div className="timeline-section">
          <div className="timeline-section-label">Summary</div>
          <p>{event.summary}</p>
        </div>
      ) : null}
      {!passed && event.issues.length ? (
        <div className="timeline-section">
          <div className="timeline-section-label">Issues</div>
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
      {!passed && event.reason ? (
        <div className="timeline-section">
          <div className="timeline-section-label">Feedback to Agent</div>
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
  const isExitError = !isError && hasNonZeroExitCode(resultContent);
  const showError = isError || isExitError;
  const statusLabel = result
    ? (isError ? 'failed' : isExitError ? 'error' : 'done')
    : (event.type === 'capability.call.started' ? 'running' : event.type === 'capability.call.failed' ? 'failed' : 'done');
  const argsText = formatCapabilityArguments(event.arguments);
  const eventClass = showError ? 'capability-event-error' : `capability-event-${statusLabel}`;
  return (
    <details className={`capability-event-card ${eventClass}`}>
      <summary className="capability-event-head">
        <Wrench size={14} />
        <strong>{event.capability_id}</strong>
        <span>{statusLabel}</span>
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

function uploadBatchRoot(files: File[]) {
  const firstPath = files[0]?.webkitRelativePath || files[0]?.name || 'upload';
  const firstSegment = firstPath.replace(/\\/g, '/').split('/').filter(Boolean)[0] || 'upload';
  return `inputs/uploads/${safeUploadSegment(firstSegment)}-${Date.now()}`;
}

function uploadSourceFiles(files: File[]): UploadSourceFile[] {
  const rawPaths = files.map((file) => (file.webkitRelativePath || file.name).replace(/\\/g, '/'));
  const commonFolder = commonUploadedFolder(rawPaths);
  return files.map((file) => {
    const rawPath = (file.webkitRelativePath || file.name).replace(/\\/g, '/');
    return {
      name: file.name,
      relativePath: commonFolder && rawPath.startsWith(`${commonFolder}/`)
        ? rawPath.slice(commonFolder.length + 1)
        : rawPath,
      webkitRelativePath: file.webkitRelativePath,
    };
  });
}

function commonUploadedFolder(paths: string[]) {
  if (!paths.length || paths.some((path) => !path.includes('/'))) return '';
  const firstSegments = paths.map((path) => path.replace(/\\/g, '/').split('/')[0]);
  const first = firstSegments[0];
  return first && firstSegments.every((segment) => segment === first) ? first : '';
}

function safeUploadSegment(value: string) {
  return value
    .replace(/\\/g, '/')
    .split('/')
    .filter(Boolean)
    .pop()
    ?.replace(/[<>:"|?*]/g, '_')
    .replace(/[^A-Za-z0-9._-]+/g, '_')
    .replace(/^_+|_+$/g, '')
    || 'upload';
}

function artifactDisplayPath(artifact: Artifact) {
  return artifact.paths?.[0] || artifact.id;
}

function artifactDisplayName(artifact: Artifact) {
  const displayName = artifact.metadata?.display_name;
  if (typeof displayName === 'string' && displayName.trim()) return displayName;
  return artifact.description || artifact.id;
}

function compareArtifactsByPath(left: Artifact, right: Artifact) {
  return artifactDisplayPath(left).localeCompare(artifactDisplayPath(right));
}

function CapabilityReviewDialog({
  review,
  onApprove,
  onReject,
  onClose,
}: {
  review: ReviewEventPayload;
  onApprove: () => void;
  onReject: () => void;
  onClose: () => void;
}) {
  const capabilityCall = review.capability_call;
  const argsText = capabilityCall ? JSON.stringify(capabilityCall.arguments, null, 2) : '';
  const risk = (review.payload?.risk as string) || 'low';
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Capability review">
      <div className="dag-modal">
        <header className="modal-header">
          <div>
            <div className="modal-title">
              <AlertTriangle size={20} />
              <span>Capability Review</span>
              <span className={`risk-badge risk-${risk}`}>{risk.toUpperCase()}</span>
            </div>
            <p>{capabilityCall?.capability_id || review.message}</p>
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
        <div className="modal-body">
          {capabilityCall ? (
            <div className="capability-section">
              <div className="capability-section-label">Capability</div>
              <p><strong>{capabilityCall.capability_id}</strong></p>
            </div>
          ) : null}
          {argsText ? (
            <div className="capability-section">
              <div className="capability-section-label">Arguments</div>
              <pre>{clipText(argsText, 1200)}</pre>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
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

function ArtifactEditorPanel({
  artifacts,
  onUpsert,
  onDelete,
  onUpload,
  onUploadFiles,
}: {
  artifacts: Record<string, Artifact>;
  onUpsert: (artifact: Artifact, previousId?: string) => void;
  onDelete: (artifactId: string) => void;
  onUpload: (artifactId: string, files: FileList | null) => void;
  onUploadFiles: (files: FileList | null) => void;
}) {
  const artifactItems = Object.values(artifacts).sort(compareArtifactsByPath);
  const uploadedFiles = artifactItems.filter(isUploadedFileArtifact);
  const manualArtifacts = artifactItems.filter((artifact) => !isUploadedFileArtifact(artifact));
  const addArtifact = () => {
    let index = manualArtifacts.length + 1;
    let id = `artifact_${index}`;
    while (artifacts[id]) {
      index += 1;
      id = `artifact_${index}`;
    }
    onUpsert({
      id,
      paths: [`outputs/${id}`],
      description: '',
      required: true,
      metadata: {},
    });
  };

  return (
    <section className="artifact-panel">
      <div className="artifact-panel-head">
        <span>Files</span>
        <div className="artifact-panel-actions">
          <label className="secondary-button compact-button artifact-upload-button" title="Upload files">
            <Upload size={14} />
            Files
            <input
              type="file"
              multiple
              onChange={(event) => {
                onUploadFiles(event.currentTarget.files);
                event.currentTarget.value = '';
              }}
            />
          </label>
          <label className="secondary-button compact-button artifact-upload-button" title="Upload folder">
            <FolderUp size={14} />
            Folder
            <input
              type="file"
              multiple
              {...directoryInputProps}
              onChange={(event) => {
                onUploadFiles(event.currentTarget.files);
                event.currentTarget.value = '';
              }}
            />
          </label>
        </div>
      </div>
      {uploadedFiles.length ? (
        <div className="uploaded-file-list">
          {uploadedFiles.map((artifact) => (
            <div className="uploaded-file-row" key={artifact.id}>
              <div className="uploaded-file-main">
                <strong>{artifactDisplayName(artifact)}</strong>
                <span>{artifactDisplayPath(artifact)}</span>
              </div>
              <button className="icon-button" onClick={() => onDelete(artifact.id)} title="Delete file" type="button">
                <Trash2 size={15} />
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-state compact">No files uploaded.</div>
      )}
      <details className="node-policy-details artifact-advanced">
        <summary>Advanced Artifacts</summary>
        <div className="artifact-panel-head nested">
          <span>Artifact Contracts</span>
          <button className="icon-button" onClick={addArtifact} title="Add artifact" type="button">
            <Plus size={15} />
          </button>
        </div>
        {manualArtifacts.length ? (
          <div className="artifact-list">
            {manualArtifacts.map((artifact) => (
              <div className="artifact-row" key={artifact.id}>
                <div className="artifact-row-grid">
                  <label>
                    ID
                    <input
                      value={artifact.id}
                      onChange={(event) => onUpsert({ ...artifact, id: event.target.value }, artifact.id)}
                    />
                  </label>
                  <label>
                    Paths
                    <input
                      value={(artifact.paths ?? []).join(', ')}
                      onChange={(event) => onUpsert({ ...artifact, paths: splitCsv(event.target.value) }, artifact.id)}
                    />
                  </label>
                </div>
                <label>
                  Description
                  <input
                    value={artifact.description ?? ''}
                    onChange={(event) => onUpsert({ ...artifact, description: event.target.value }, artifact.id)}
                  />
                </label>
                <div className="artifact-row-actions">
                  <label className="checkbox-line">
                    <input
                      type="checkbox"
                      checked={artifact.required ?? true}
                      onChange={(event) => onUpsert({ ...artifact, required: event.target.checked }, artifact.id)}
                    />
                    Required
                  </label>
                  <div className="artifact-action-buttons">
                    <label className="secondary-button compact-button artifact-upload-button" title="Upload files">
                      <Upload size={14} />
                      Files
                      <input
                        type="file"
                        multiple
                        onChange={(event) => {
                          onUpload(artifact.id, event.currentTarget.files);
                          event.currentTarget.value = '';
                        }}
                      />
                    </label>
                    <label className="secondary-button compact-button artifact-upload-button" title="Upload folder">
                      <FolderUp size={14} />
                      Folder
                      <input
                        type="file"
                        multiple
                        {...directoryInputProps}
                        onChange={(event) => {
                          onUpload(artifact.id, event.currentTarget.files);
                          event.currentTarget.value = '';
                        }}
                      />
                    </label>
                    <button className="icon-button" onClick={() => onDelete(artifact.id)} title="Delete artifact" type="button">
                      <Trash2 size={15} />
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="empty-state compact">No advanced artifacts.</div>
        )}
      </details>
    </section>
  );
}

function OrchestrationWorkspace({
  capabilities,
  savedDags,
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
  workspaceRoot,
  onNew,
  onLoad,
  onPatchDag,
  onWorkspaceRootChange,
  onUpsertArtifact,
  onDeleteArtifact,
  onUploadArtifact,
  onUploadFiles,
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
  savedDags: UserDag[];
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
  workspaceRoot: string;
  onNew: () => void;
  onLoad: (spec: UserDag) => void;
  onPatchDag: (patch: Partial<UserDag>) => void;
  onWorkspaceRootChange: (workspaceRoot: string) => void;
  onUpsertArtifact: (artifact: Artifact, previousId?: string) => void;
  onDeleteArtifact: (artifactId: string) => void;
  onUploadArtifact: (artifactId: string, files: FileList | null) => void;
  onUploadFiles: (files: FileList | null) => void;
  onAddNode: (capability?: CapabilityDefinition) => void;
  onPatchNode: (nodeId: string, patch: Partial<DagNode>, edges?: DagEdge[]) => void;
  onDeleteNode: (nodeId?: string) => void;
  onSave: () => void;
  onRun: () => void;
  onNodesChange: (changes: NodeChange[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onConnect: (connection: Connection) => void;
  onSelectNode: (id: string) => void;
}) {
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; nodeId?: string } | null>(null);
  const [contextCapabilityId, setContextCapabilityId] = useState('');
  const [nodeDrawerOpen, setNodeDrawerOpen] = useState(false);
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const selectedNode = dag.nodes.find((node) => node.id === selectedId) ?? dag.nodes[0];
  const runSummary = buildRunDialogSummary(userDagFromRuntimeDag(spec, dag));
  const enabledCapabilities = visibleCapabilitiesForPicker(capabilities);
  const contextCapability = enabledCapabilities.find((capability) => capability.id === contextCapabilityId) ?? enabledCapabilities[0];
  const selectNode = (id: string) => {
    onSelectNode(id);
    setNodeDrawerOpen(true);
  };
  const openCanvasMenu = (event: MouseEvent | React.MouseEvent<Element>) => {
    event.preventDefault();
    setContextMenu({ x: event.clientX, y: event.clientY });
    setContextCapabilityId((current) => current || enabledCapabilities[0]?.id || '');
  };
  const openNodeMenu = (event: React.MouseEvent, node: Node) => {
    event.preventDefault();
    event.stopPropagation();
    selectNode(node.id);
    setContextMenu({ x: event.clientX, y: event.clientY, nodeId: node.id });
    setContextCapabilityId((current) => current || enabledCapabilities[0]?.id || '');
  };
  const addFromContext = () => {
    if (contextCapability) {
      onAddNode(contextCapability);
      setNodeDrawerOpen(true);
    }
    setContextMenu(null);
  };
  const deleteFromContext = () => {
    if (contextMenu?.nodeId) onDeleteNode(contextMenu.nodeId);
    setContextMenu(null);
  };
  const deleteSelectedNode = () => {
    onDeleteNode(selectedNode?.id);
    setNodeDrawerOpen(false);
  };
  return (
    <section className="console-grid orchestration-grid">
      <aside className="console-sidebar">
        <PaneTitle icon={<FileText size={18} />} title="DAGs" />
        <div className="sidebar-actions">
          <button className="secondary-button compact-button" onClick={onNew} type="button">
            <Plus size={16} />
            New
          </button>
          <button className="secondary-button compact-button" onClick={onSave} type="button">
            <Save size={16} />
            Save
          </button>
          <button className="primary-button compact-button" onClick={() => setRunDialogOpen(true)} type="button">
            <Play size={16} />
            Run
          </button>
        </div>
        <div className="spec-meta-form">
          <label>
            Name
            <input value={spec.name} onChange={(event) => onPatchDag({ name: event.target.value })} />
          </label>
          <label>
            Description
            <textarea value={spec.description ?? ''} onChange={(event) => onPatchDag({ description: event.target.value })} />
          </label>
          <label>
            Workspace Root
            <input value={workspaceRoot} onChange={(event) => onWorkspaceRootChange(event.target.value)} />
          </label>
        </div>
        <ArtifactEditorPanel
          artifacts={spec.artifacts ?? {}}
          onUpsert={onUpsertArtifact}
          onDelete={onDeleteArtifact}
          onUpload={onUploadArtifact}
          onUploadFiles={onUploadFiles}
        />
        <div className="resource-list">
          {savedDags.length ? savedDags.map((item) => (
            <button
              key={item.id}
              className={item.id === spec.id ? 'resource-row active' : 'resource-row'}
              type="button"
              onClick={() => onLoad(item)}
            >
              <strong>{item.name || 'Untitled DAG'}</strong>
              <span>{item.nodes.length} nodes</span>
            </button>
          )) : <div className="empty-state compact">No saved DAGs in this process.</div>}
        </div>
      </aside>
      <section className="flow-workbench">
        <div className="workbench-toolbar">
          <div>
            <strong>{spec.name || 'Untitled DAG'}</strong>
            <span>{message || (run ? `Last run: ${run.status}` : 'Draft DAG')}</span>
          </div>
          <select
            onChange={(event) => {
              const capability = enabledCapabilities.find((item) => item.id === event.target.value);
              if (capability) {
                onAddNode(capability);
                setNodeDrawerOpen(true);
              }
              event.currentTarget.value = '';
            }}
            defaultValue=""
            aria-label="Add capability node"
          >
            <option value="" disabled>Add node from capability...</option>
            {enabledCapabilities.map((capability) => (
              <option key={capability.id} value={capability.id}>
                {capabilityDisplayName(capability)}
              </option>
            ))}
          </select>
        </div>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={(_, node) => selectNode(node.id)}
          onPaneClick={() => {
            setContextMenu(null);
            setNodeDrawerOpen(false);
          }}
          onPaneContextMenu={openCanvasMenu}
          onNodeContextMenu={openNodeMenu}
          fitView
          fitViewOptions={{ padding: 0.2 }}
        >
          <Background color="#e2e4ea" gap={20} />
          <MiniMap pannable zoomable nodeColor="#4f6ef7" maskColor="rgba(245,246,248,0.7)" />
          <Controls />
        </ReactFlow>
        {contextMenu ? (
          <div
            className="canvas-context-menu"
            style={{ left: contextMenu.x, top: contextMenu.y }}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="context-menu-title">{contextMenu.nodeId ? `Node: ${contextMenu.nodeId}` : 'Canvas'}</div>
            <label>
              Capability
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
              Add node
            </button>
            {contextMenu.nodeId ? (
              <button className="context-menu-item danger" onClick={deleteFromContext} type="button">
                <Trash2 size={15} />
                Delete node
              </button>
            ) : null}
          </div>
        ) : null}
      </section>
      {nodeDrawerOpen && selectedNode ? (
        <aside className="node-config-drawer" aria-label="Node config">
          <header className="node-config-drawer-head">
            <PaneTitle icon={<SlidersHorizontal size={18} />} title="Node Config" />
            <button className="icon-button" onClick={() => setNodeDrawerOpen(false)} title="Close node config" type="button">
              <X size={18} />
            </button>
          </header>
          <OrchestrationNodeEditor
            node={normalizeNode(selectedNode)}
            dag={dag}
            artifacts={spec.artifacts ?? {}}
            capabilities={capabilities}
            logs={trace.filter((event) => event.node_id === selectedNode.id)}
            onPatch={(patch, nextEdges) => onPatchNode(selectedNode.id, patch, nextEdges)}
            onDelete={deleteSelectedNode}
          />
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
          onStart={onRun}
          onClose={() => setRunDialogOpen(false)}
        />
      ) : null}
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
  onStart,
  onClose,
}: {
  specName: string;
  summary: RunDialogSummary;
  run: DagRun | null;
  timeline: RunTranscriptItem[];
  running: boolean;
  message: string;
  onStart: () => void;
  onClose: () => void;
}) {
  const state = running ? 'running' : run?.status ?? 'ready';
  const hasStarted = running || Boolean(run) || timeline.length > 0;
  const startLabel = run ? 'Run Again' : 'Start Run';
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Run DAG">
      <div className="run-dialog">
        <header className="modal-header">
          <div>
            <div className="modal-title">
              <Play size={20} />
              <span>Run DAG</span>
              <span className={`run-state ${state}`}>{state}</span>
            </div>
            <p>{message || specName || 'Untitled DAG'}</p>
          </div>
          <div className="modal-actions">
            <button className="secondary-button compact-button" onClick={onClose} type="button">
              <X size={16} />
              Close
            </button>
            <button
              className="primary-button"
              onClick={onStart}
              disabled={running || !summary.canRun}
              type="button"
            >
              {running ? <Loader size={17} className="spin" /> : <Play size={17} />}
              {startLabel}
            </button>
          </div>
        </header>
        <div className={hasStarted ? 'run-dialog-body transcript-mode' : 'run-dialog-body'}>
          {hasStarted ? (
            <>
              <details className="run-context-details">
                <summary>Run Context</summary>
                <div className="run-context-body">
                  <RunPreflightPanel summary={summary} />
                </div>
              </details>
              <RunTranscript timeline={timeline} running={running} />
              {run?.workspace_path ? (
                <div className="readonly-note">Workspace: {run.workspace_path}</div>
              ) : null}
            </>
          ) : (
            <RunPreflightPanel summary={summary} />
          )}
        </div>
      </div>
    </div>
  );
}

function RunPreflightPanel({ summary }: { summary: RunDialogSummary }) {
  return (
    <>
      <section className="run-overview-grid">
        <div className="run-stat-card">
          <span>Nodes</span>
          <strong>{summary.nodeCount}</strong>
        </div>
        <div className="run-stat-card">
          <span>Edges</span>
          <strong>{summary.edgeCount}</strong>
        </div>
        <div className="run-stat-card">
          <span>Inputs</span>
          <strong>{summary.inputArtifacts.length}</strong>
        </div>
        <div className="run-stat-card">
          <span>Review</span>
          <strong>{summary.riskyNodes.length}</strong>
        </div>
      </section>
      {summary.issues.length ? (
        <section className="run-section run-issues">
          <h3><AlertTriangle size={15} /> Blocking Issues</h3>
          <div className="run-list">
            {summary.issues.map((issue, index) => (
              <div className="run-list-row" key={`${issue.nodeId ?? 'spec'}-${index}`}>
                <strong>{issue.nodeId ?? 'DAG'}</strong>
                <span>{issue.message}</span>
              </div>
            ))}
          </div>
        </section>
      ) : null}
      <section className="run-artifact-grid">
        <RunArtifactPanel title="Input Files" artifacts={summary.inputArtifacts} empty="No file inputs selected." />
        <RunArtifactPanel title="Outputs" artifacts={summary.outputArtifacts} empty="No output artifacts selected." />
      </section>
      {summary.riskyNodes.length ? (
        <section className="run-section">
          <h3><AlertTriangle size={15} /> Review Nodes</h3>
          <div className="run-risk-list">
            {summary.riskyNodes.map((node) => (
              <div className="run-risk-row" key={node.id}>
                <strong>{node.id}</strong>
                <span>{node.capabilityId || 'Missing capability'}</span>
                <em className={`risk-badge risk-${node.risk}`}>{node.risk}</em>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </>
  );
}

function RunTranscript({
  timeline,
  running,
}: {
  timeline: RunTranscriptItem[];
  running: boolean;
}) {
  const message: ChatMessage = {
    role: 'assistant',
    content: timeline.length ? '' : (running ? 'Running DAG...' : 'Run transcript will appear here.'),
    timeline,
  };
  return (
    <section className="run-transcript">
      <div className="message assistant run-transcript-message">
        <span>Run Transcript</span>
        <MessageTimeline
          message={message}
          loading={running && timeline.length === 0}
          onOpenDag={() => undefined}
        />
      </div>
    </section>
  );
}

function RunArtifactPanel({
  title,
  artifacts,
  empty,
}: {
  title: string;
  artifacts: RunDialogSummary['inputArtifacts'];
  empty: string;
}) {
  return (
    <section className="run-section">
      <h3>{title}</h3>
      {artifacts.length ? (
        <div className="run-list">
          {artifacts.map((artifact) => (
            <div className="run-list-row" key={artifact.id}>
              <strong>{artifact.label}</strong>
              <span>{artifact.path}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-state compact">{empty}</div>
      )}
    </section>
  );
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
    allowed_paths: [],
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
        <span>Arguments</span>
        <button className="secondary-button compact-button" onClick={addField} type="button">
          <Plus size={14} />
          Add field
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
                aria-label="Argument name"
              />
              {field.fixed ? (
                <span className="argument-meta" title={field.description}>
                  {field.required ? 'required' : 'optional'}
                </span>
              ) : null}
            </div>
            <select
              className="argument-type"
              value={type}
              disabled={field.fixed}
              onChange={(event) => updateType(key, event.target.value as ArgumentValueType)}
              aria-label="Argument type"
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
                aria-label="Argument value"
              >
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            ) : (
              <input
                className="argument-value"
                value={formatArgumentValue(itemValue)}
                onChange={(event) => updateValue(key, event.target.value, type)}
                aria-label="Argument value"
              />
            )}
            <button
              className="icon-button"
              onClick={() => removeField(key)}
              disabled={field.fixed}
              title={field.fixed ? 'Schema-defined argument' : 'Remove argument'}
              type="button"
            >
              <Trash2 size={15} />
            </button>
          </div>
        );
      }) : (
        <div className="empty-state compact">No arguments yet. Add a field for this node.</div>
      )}
    </section>
  );
}

function CapabilityDirectory({
  capabilities,
  skills,
  mcpServers,
  onRefresh,
}: {
  capabilities: CapabilityDefinition[];
  skills: SkillSummary[];
  mcpServers: MCPServer[];
  onRefresh: () => Promise<void>;
}) {
  const [activeTab, setActiveTab] = useState<'tools' | 'skills' | 'mcp'>('tools');
  const [query, setQuery] = useState('');
  const [draftCapability, setDraftCapability] = useState<CapabilityDefinition>(defaultCustomCapability);
  const [draftParametersText, setDraftParametersText] = useState(JSON.stringify(defaultCustomCapability.parameters, null, 2));
  const [argumentsText, setArgumentsText] = useState('{"text":"hello"}');
  const [selectedId, setSelectedId] = useState('');
  const [result, setResult] = useState<CapabilityResult | null>(null);
  const [message, setMessage] = useState('');
  const [selectedSkillName, setSelectedSkillName] = useState('');
  const [skillDetail, setSkillDetail] = useState<SkillDetail | null>(null);
  const [skillFileDetail, setSkillFileDetail] = useState<SkillFileDetail | null>(null);
  const [skillMessage, setSkillMessage] = useState('');
  const [skillImport, setSkillImport] = useState({ name: '', description: '', category: '', content: '' });
  const [selectedMcpName, setSelectedMcpName] = useState('');
  const [mcpDraft, setMcpDraft] = useState<{ name: string } & MCPServerConfig>(defaultMcpConfig);
  const [mcpArgsText, setMcpArgsText] = useState('');
  const [mcpEnvText, setMcpEnvText] = useState('');
  const [mcpMessage, setMcpMessage] = useState('');
  const filtered = capabilities.filter((capability) => {
    const haystack = `${capability.id} ${capability.name} ${capability.kind} ${capability.description}`.toLowerCase();
    return haystack.includes(query.toLowerCase());
  });
  const selected = capabilities.find((capability) => capability.id === selectedId) ?? filtered[0];
  const selectedEditable = Boolean(selected && isEditableToolCapability(selected));
  const grouped = capabilityKinds
    .map((kind) => ({ kind, items: filtered.filter((capability) => capability.kind === kind) }))
    .filter((group) => group.items.length);
  const selectedSkill = skills.find((skill) => skillLookupName(skill) === selectedSkillName) ?? skills[0];
  const linkedFileGroups = Object.entries(skillDetail?.linked_files ?? {})
    .filter(([, files]) => files.length);
  const selectedMcp = mcpServers.find((server) => server.name === selectedMcpName) ?? mcpServers[0];

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
      setSelectedId(definition.id);
      setMessage(`Created ${definition.id}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const runTest = async () => {
    if (!selected) return;
    const parsed = parseJsonObject(argumentsText);
    if (!parsed) {
      setMessage('Test arguments must be a JSON object.');
      return;
    }
    setMessage(`Testing ${selected.id}...`);
    try {
      const nextResult = await testCapability(selected.id, parsed);
      setResult(nextResult);
      setMessage(`Test ${nextResult.status}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const toggleCapability = async (enabled: boolean) => {
    if (!selected) return;
    setMessage(enabled ? 'Enabling capability...' : 'Disabling capability...');
    try {
      await setCapabilityEnabled(selected.id, enabled);
      await onRefresh();
      setMessage(`${enabled ? 'Enabled' : 'Disabled'} ${selected.id}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const removeCapability = async () => {
    if (!selected || !isEditableToolCapability(selected)) return;
    setMessage('Deleting tool...');
    try {
      await deleteCapability(selected.id);
      setSelectedId('');
      await onRefresh();
      setMessage(`Deleted ${selected.id}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const openSkill = async (skill: SkillSummary) => {
    const lookup = skillLookupName(skill);
    setSelectedSkillName(lookup);
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
  };

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
      setSelectedSkillName(skillLookupName(detail.skill));
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
      setSelectedSkillName('');
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
        setSelectedSkillName(skillLookupName(detail.skill));
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

  const selectMcpServer = (server: MCPServer) => {
    setSelectedMcpName(server.name);
    setMcpDraft({
      ...defaultMcpConfig,
      name: server.name,
      ...server.config,
    });
    setMcpArgsText((server.config.args ?? []).join('\n'));
    setMcpEnvText(formatEnvText(server.config.env ?? {}));
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
      setSelectedMcpName(payload.name);
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
      setSelectedMcpName('');
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
    <section className="console-grid directory-grid">
      <aside className="console-sidebar">
        <PaneTitle icon={<Wrench size={18} />} title="Capability Workbench" />
        <div className="capability-tabs" role="tablist" aria-label="Capability workbench sections">
          {(['tools', 'skills', 'mcp'] as const).map((tab) => (
            <button key={tab} className={activeTab === tab ? 'active' : ''} onClick={() => setActiveTab(tab)} type="button">
              {tab}
            </button>
          ))}
        </div>
        {activeTab === 'tools' ? (
          <>
            <label className="search-field">
              <Search size={15} />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search capabilities" />
            </label>
            <div className="resource-list">
              {grouped.map((group) => (
                <div key={group.kind} className="resource-group">
                  <h3>{group.kind}</h3>
                  {group.items.map((capability) => (
                    <button
                      key={capability.id}
                      className={selected?.id === capability.id ? 'resource-row active' : 'resource-row'}
                      type="button"
                      onClick={() => setSelectedId(capability.id)}
                    >
                      <strong>{capability.name}</strong>
                      <span>{capability.id}</span>
                    </button>
                  ))}
                </div>
              ))}
            </div>
          </>
        ) : activeTab === 'skills' ? (
          <div className="resource-list">
            {skills.length ? skills.map((skill) => (
              <button
                key={skill.path}
                className={skillLookupName(selectedSkill ?? skill) === skillLookupName(skill) ? 'resource-row active' : 'resource-row'}
                type="button"
                onClick={() => void openSkill(skill)}
              >
                <strong>{skill.name}</strong>
                <span>{skill.category ? `${skill.category} · ${skill.path}` : skill.path}</span>
              </button>
            )) : <div className="empty-state compact">No skills found.</div>}
          </div>
        ) : (
          <div className="resource-list">
            {mcpServers.length ? mcpServers.map((server) => (
              <button
                key={server.name}
                className={selectedMcp?.name === server.name ? 'resource-row active' : 'resource-row'}
                type="button"
                onClick={() => selectMcpServer(server)}
              >
                <strong>{server.name}</strong>
                <span>{server.status} · {server.tools.length} tools · {server.source}</span>
              </button>
            )) : <div className="empty-state compact">No MCP servers configured.</div>}
          </div>
        )}
      </aside>
      <section className="console-detail wide">
        {activeTab === 'tools' ? (
          <>
            <PaneTitle icon={<Database size={18} />} title="Capability Detail" />
            {selected ? (
              <div className="directory-detail">
                <div className="detail-header">
                  <div>
                    <h2>{selected.name}</h2>
                    <p>{selected.id}</p>
                  </div>
                  <span className={`risk-badge risk-${selected.policy.risk}`}>{selected.policy.risk}</span>
                </div>
                <div className="metadata-grid">
                  <span>Kind</span><strong>{selected.kind}</strong>
                  <span>Status</span><strong>{selected.enabled ? 'enabled' : 'disabled'}</strong>
                  <span>Source</span><strong>{selectedEditable ? 'memory tool' : 'backend/config readonly'}</strong>
                </div>
                <p>{selected.description || 'No description.'}</p>
                <div className="two-col">
                  <section className="code-panel">
                    <h3>Parameters</h3>
                    <pre>{JSON.stringify(selected.parameters, null, 2)}</pre>
                  </section>
                  <section className="code-panel">
                    <h3>Config</h3>
                    <pre>{JSON.stringify(selected.config, null, 2)}</pre>
                  </section>
                </div>
                <section className="code-panel">
                  <h3>Test Arguments</h3>
                  <textarea value={argumentsText} onChange={(event) => setArgumentsText(event.target.value)} />
                  <div className="inline-actions">
                    <button className="primary-button compact-button" onClick={runTest} type="button">
                      <Play size={16} />
                      Test
                    </button>
                    <button className="secondary-button compact-button" onClick={() => void toggleCapability(!selected.enabled)} disabled={!selectedEditable} type="button">
                      {selected.enabled ? 'Disable' : 'Enable'}
                    </button>
                    <button className="secondary-button danger-button compact-button" onClick={removeCapability} disabled={!selectedEditable} type="button">
                      Delete
                    </button>
                  </div>
                  {message ? <p className="form-message">{message}</p> : null}
                  {result ? <pre>{JSON.stringify(result, null, 2)}</pre> : null}
                </section>
                {!selectedEditable ? <div className="readonly-note">This capability is provided by backend configuration and is read-only in the MVP.</div> : null}
              </div>
            ) : <div className="empty-state compact">No capabilities loaded.</div>}
          </>
        ) : activeTab === 'skills' ? (
          <>
            <PaneTitle icon={<FileText size={18} />} title="Skill Detail" />
            <div className="directory-detail">
              {skillDetail ? (
                <>
                  <div className="detail-header">
                    <div>
                      <h2>{skillDetail.name}</h2>
                      <p>{skillDetail.category ? `${skillDetail.category}/${skillDetail.name}` : skillDetail.name}</p>
                    </div>
                    <span className="status-badge" data-status={isManagedSkill(skillDetail.skill) ? 'approved' : 'completed'}>
                      {isManagedSkill(skillDetail.skill) ? 'installed' : 'local'}
                    </span>
                  </div>
                  <p>{skillDetail.description || 'No description.'}</p>
                  {skillDetail.skill_dir ? (
                    <div className="metadata-grid">
                      <span>Directory</span><strong>{skillDetail.skill_dir}</strong>
                    </div>
                  ) : null}
                  {linkedFileGroups.length ? (
                    <section className="code-panel">
                      <h3>Linked Files</h3>
                      <div className="linked-file-list">
                        {linkedFileGroups.map(([folder, files]) => (
                          <div key={folder} className="linked-file-group">
                            <strong>{folder}</strong>
                            <div>
                              {files.map((filePath) => (
                                <button
                                  key={filePath}
                                  className={skillFileDetail?.file_path === filePath ? 'secondary-button compact-button active' : 'secondary-button compact-button'}
                                  onClick={() => void openSkillLinkedFile(filePath)}
                                  type="button"
                                >
                                  <FileText size={14} />
                                  {filePath}
                                </button>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    </section>
                  ) : null}
                  <section className="code-panel">
                    <h3>Content</h3>
                    <pre>{skillDetail.content}</pre>
                  </section>
                  {skillFileDetail ? (
                    <section className="code-panel">
                      <h3>{skillFileDetail.file_path}</h3>
                      <pre>{skillFileDetail.content}</pre>
                    </section>
                  ) : null}
                  <section className="code-panel">
                    <h3>Metadata</h3>
                    <pre>{JSON.stringify(skillDetail.metadata, null, 2)}</pre>
                  </section>
                </>
              ) : selectedSkill ? (
                <div className="readonly-note">Select a skill to load its normalized content.</div>
              ) : (
                <div className="empty-state compact">No skill selected.</div>
              )}
              <div className="inline-actions">
                {selectedSkill ? (
                  <button className="secondary-button compact-button" onClick={() => void openSkill(selectedSkill)} type="button">
                    <Search size={16} />
                    View
                  </button>
                ) : null}
                <button className="secondary-button danger-button compact-button" onClick={removeManagedSkill} disabled={!skillDetail || !isManagedSkill(skillDetail.skill)} type="button">
                  Delete installed
                </button>
              </div>
              {skillMessage ? <p className="form-message">{skillMessage}</p> : null}
            </div>
          </>
        ) : (
          <>
            <PaneTitle icon={<Database size={18} />} title="MCP Server Detail" />
            <div className="directory-detail">
              {selectedMcp ? (
                <>
                  <div className="detail-header">
                    <div>
                      <h2>{selectedMcp.name}</h2>
                      <p>{selectedMcp.source}</p>
                    </div>
                    <span className="status-badge" data-status={selectedMcp.status === 'connected' ? 'completed' : selectedMcp.status === 'error' ? 'failed' : 'running'}>
                      {selectedMcp.status}
                    </span>
                  </div>
                  {selectedMcp.error ? <div className="error-banner">{selectedMcp.error}</div> : null}
                  <div className="metadata-grid">
                    <span>Command</span><strong>{selectedMcp.config.command || 'not set'}</strong>
                    <span>Risk</span><strong>{selectedMcp.config.risk ?? 'medium'}</strong>
                    <span>Tools</span><strong>{selectedMcp.tools.length}</strong>
                  </div>
                  <section className="code-panel">
                    <h3>Discovered Tools</h3>
                    <pre>{JSON.stringify(selectedMcp.tools, null, 2)}</pre>
                  </section>
                </>
              ) : <div className="empty-state compact">No MCP server selected.</div>}
              <div className="inline-actions">
                <button className="secondary-button compact-button" onClick={() => void reloadMcp()} type="button">
                  <RefreshCw size={16} />
                  Reload
                </button>
                <button className="secondary-button danger-button compact-button" onClick={removeMcpServer} disabled={!selectedMcp || selectedMcp.source !== 'memory'} type="button">
                  Delete memory server
                </button>
              </div>
              {mcpMessage ? <p className="form-message">{mcpMessage}</p> : null}
            </div>
          </>
        )}
      </section>
      <aside className="console-sidebar">
        {activeTab === 'tools' ? (
          <>
            <PaneTitle icon={<Plus size={18} />} title="New tool" />
            <div className="spec-meta-form">
              <label>
                ID
                <input
                  value={draftCapability.id}
                  onChange={(event) => setDraftCapability((current) => ({ ...current, id: event.target.value, kind: 'tool' }))}
                />
              </label>
              <label>
                Name
                <input
                  value={draftCapability.name}
                  onChange={(event) => setDraftCapability((current) => ({ ...current, name: event.target.value, kind: 'tool' }))}
                />
              </label>
              <label>
                Description
                <textarea
                  value={draftCapability.description}
                  onChange={(event) => setDraftCapability((current) => ({ ...current, description: event.target.value, kind: 'tool' }))}
                />
              </label>
              <div className="two-col">
                <label>
                  Risk
                  <select
                    value={draftCapability.policy.risk}
                    onChange={(event) => setDraftCapability((current) => ({
                      ...current,
                      kind: 'tool',
                      policy: { ...current.policy, risk: event.target.value as RiskLevel },
                    }))}
                  >
                    {riskLevels.map((risk) => <option key={risk} value={risk}>{risk}</option>)}
                  </select>
                </label>
                <label className="checkbox-line">
                  <input
                    type="checkbox"
                    checked={draftCapability.policy.requires_review}
                    onChange={(event) => setDraftCapability((current) => ({
                      ...current,
                      kind: 'tool',
                      policy: { ...current.policy, requires_review: event.target.checked },
                    }))}
                  />
                  Requires review
                </label>
              </div>
              <label>
                Parameters JSON Schema
                <textarea value={draftParametersText} onChange={(event) => setDraftParametersText(event.target.value)} />
              </label>
              <label>
                Template
                <textarea
                  value={String(draftCapability.config.template ?? '')}
                  onChange={(event) => setDraftCapability((current) => ({
                    ...current,
                    kind: 'tool',
                    config: { ...current.config, template: event.target.value },
                  }))}
                />
              </label>
              <button className="primary-button" onClick={runCreate} type="button">
                <Plus size={16} />
                Create tool
              </button>
            </div>
          </>
        ) : activeTab === 'skills' ? (
          <>
            <PaneTitle icon={<FolderUp size={18} />} title="Install skill" />
            <div className="spec-meta-form">
              <label>
                Name
                <input value={skillImport.name} onChange={(event) => setSkillImport((current) => ({ ...current, name: event.target.value }))} />
              </label>
              <label>
                Category
                <input value={skillImport.category} onChange={(event) => setSkillImport((current) => ({ ...current, category: event.target.value }))} />
              </label>
              <label>
                Description
                <textarea value={skillImport.description} onChange={(event) => setSkillImport((current) => ({ ...current, description: event.target.value }))} />
              </label>
              <label>
                SKILL.md
                <textarea value={skillImport.content} onChange={(event) => setSkillImport((current) => ({ ...current, content: event.target.value }))} />
              </label>
              <label>
                Upload
                <input type="file" accept=".md,text/markdown,text/plain,.zip,application/zip" onChange={(event) => void loadSkillFile(event.target.files?.[0])} />
              </label>
              <div className="readonly-note">Markdown and zip installs are stored under the managed local skill root. Zip packages may include references, templates, scripts, and assets.</div>
              <button className="primary-button" onClick={installSkillDraft} type="button">
                <Upload size={16} />
                Install skill
              </button>
            </div>
          </>
        ) : (
          <>
            <PaneTitle icon={<Plus size={18} />} title="Stdio MCP server" />
            <div className="spec-meta-form">
              <label>
                Name
                <input value={mcpDraft.name} onChange={(event) => setMcpDraft((current) => ({ ...current, name: event.target.value }))} />
              </label>
              <label>
                Command
                <input value={mcpDraft.command} onChange={(event) => setMcpDraft((current) => ({ ...current, command: event.target.value }))} />
              </label>
              <label>
                Args
                <textarea value={mcpArgsText} onChange={(event) => setMcpArgsText(event.target.value)} placeholder="One argument per line" />
              </label>
              <label>
                Env
                <textarea value={mcpEnvText} onChange={(event) => setMcpEnvText(event.target.value)} placeholder="KEY=value" />
              </label>
              <div className="two-col">
                <label>
                  Risk
                  <select value={mcpDraft.risk ?? 'medium'} onChange={(event) => setMcpDraft((current) => ({ ...current, risk: event.target.value as RiskLevel }))}>
                    {riskLevels.map((risk) => <option key={risk} value={risk}>{risk}</option>)}
                  </select>
                </label>
                <label className="checkbox-line">
                  <input type="checkbox" checked={mcpDraft.enabled !== false} onChange={(event) => setMcpDraft((current) => ({ ...current, enabled: event.target.checked }))} />
                  Enabled
                </label>
              </div>
              <div className="two-col">
                <label>
                  Connect timeout
                  <input type="number" value={mcpDraft.connect_timeout ?? 30} onChange={(event) => setMcpDraft((current) => ({ ...current, connect_timeout: Number(event.target.value) }))} />
                </label>
                <label>
                  Tool timeout
                  <input type="number" value={mcpDraft.tool_timeout ?? 60} onChange={(event) => setMcpDraft((current) => ({ ...current, tool_timeout: Number(event.target.value) }))} />
                </label>
              </div>
              <button className="primary-button" onClick={saveMcpServer} type="button">
                <Save size={16} />
                Save MCP server
              </button>
            </div>
          </>
        )}
      </aside>
    </section>
  );
}

function chatCapabilityScopeLabel(mode: ChatScopeMode, capabilityCount: number, skillCount: number): string {
  if (mode === 'all') return 'All capabilities';
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

function AgentDirectory({
  profiles,
  warnings,
  selectedId,
  onSelect,
}: {
  profiles: AgentProfile[];
  warnings: ProfileWarning[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  const selected = profiles.find((profile) => profile.id === selectedId) ?? profiles[0];
  return (
    <section className="console-grid directory-grid">
      <aside className="console-sidebar">
        <PaneTitle icon={<UserCog size={18} />} title="Profiles" />
        <div className="resource-list">
          {profiles.length ? profiles.map((profile) => (
            <button
              key={profile.id}
              className={selected?.id === profile.id ? 'resource-row active' : 'resource-row'}
              type="button"
              onClick={() => onSelect(profile.id)}
            >
              <strong>{profile.name}</strong>
              <span>{profile.source} · {profile.description || 'Markdown profile'}</span>
            </button>
          )) : <div className="empty-state compact">No profiles found.</div>}
        </div>
        {warnings.length ? (
          <div className="warning-list">
            {warnings.map((warning) => (
              <p key={warning.name}><strong>{warning.name}</strong>: {warning.error}</p>
            ))}
          </div>
        ) : null}
      </aside>
      <section className="console-detail wide">
        <PaneTitle icon={<Bot size={18} />} title="Agent Profile" />
        {selected ? (
          <div className="directory-detail">
            <div className="detail-header">
              <div>
                <h2>{selected.name}</h2>
                <p>{selected.description || 'Markdown profile'}</p>
              </div>
            </div>
            <div className="metadata-grid">
              <span>File</span><strong>{selected.name}.md</strong>
              <span>Source</span><strong>{selected.source}</strong>
              <span>Characters</span><strong>{selected.content.length}</strong>
            </div>
            <div className="profile-layer-list">
              <section className="code-panel">
                <h3>Prompt</h3>
                <pre>{selected.content || '(empty)'}</pre>
              </section>
            </div>
            <div className="readonly-note">Profiles are read-only in this MVP. Add or edit profile files on disk, then refresh.</div>
          </div>
        ) : <div className="empty-state compact">Select a profile to inspect its prompt.</div>}
      </section>
    </section>
  );
}

function DagReviewDialog({
  dag,
  nodes,
  edges,
  trace,
  selectedNode,
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
  const selectedNodeLogs = selectedNode
    ? trace.filter((event) => event.node_id === selectedNode.id && (!event.dag_id || event.dag_id === dag.dag_id))
    : [];
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="DAG review">
      <div className="dag-modal">
        <header className="modal-header">
          <div>
            <div className="modal-title">
              <GitBranch size={20} />
              <span>DAG Review</span>
              <StatusBadge status={dag.status} />
            </div>
            <p>{dag.task_id || dag.dag_id}</p>
          </div>
          <div className="modal-actions">
            <button className="secondary-button compact-button" onClick={() => onAddNode()} type="button">
              <Plus size={16} />
              Add Node
            </button>
            <button className="secondary-button compact-button" onClick={onReject} disabled={!canConfirm} type="button">
              <X size={16} />
              Reject
            </button>
            <button className="primary-button" onClick={onConfirm} disabled={!canConfirm} type="button">
              <Check size={17} />
              {canConfirm ? 'Confirm & Resume' : 'Completed'}
            </button>
            <button className="icon-button" onClick={onClose} title="Close" type="button">
              <X size={18} />
            </button>
          </div>
        </header>
        <div className="modal-body">
          <section className="modal-flow">
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
            <PaneTitle icon={<SlidersHorizontal size={18} />} title="Node Detail" />
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
    allowed_paths: [],
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
