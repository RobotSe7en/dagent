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
  UserCog,
  Wrench,
  X,
} from 'lucide-react';
import {
  createCapability,
  deleteCapability,
  getValidationStatus,
  listCapabilities,
  listDagSpecs,
  listProfiles,
  resetSession,
  resumeCapabilityReview,
  resumeDagReview,
  runDagSpecStream,
  saveDagSpec,
  setCapabilityEnabled,
  setValidationEnabled as apiSetValidation,
  streamTask,
  testCapability,
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
  DagSpec,
  ProfileWarning,
  ReviewEventPayload,
  ValidationFeedbackEvent,
  ReviewLevel,
  RiskLevel,
  CapabilityStreamEvent,
  TraceLogEvent,
  WorkspaceKey,
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

const riskClass: Record<RiskLevel, string> = {
  low: 'risk-low',
  medium: 'risk-medium',
  high: 'risk-high',
};

const riskLevels: RiskLevel[] = ['low', 'medium', 'high'];
const boundaryModes: BoundaryMode[] = ['read_only', 'write_limited', 'full'];
const reviewLevels: ReviewLevel[] = ['fast', 'careful'];
const capabilityKinds: CapabilityKind[] = ['tool', 'mcp', 'skill', 'shell', 'custom_tool', 'agent', 'memory', 'file'];
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
  id: 'custom_tool.example',
  name: 'example',
  kind: 'custom_tool',
  description: '',
  parameters: {
    type: 'object',
    properties: {},
  },
  policy: defaultCapabilityPolicy,
  config: {
    template: 'result:{text}',
  },
  enabled: true,
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

function createEmptyDagSpec(): DagSpec {
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

function dagFromSpec(spec: DagSpec): Dag {
  return {
    dag_id: spec.id,
    task_id: spec.id,
    version: spec.version ?? 1,
    status: 'draft',
    nodes: (spec.nodes ?? []).map(normalizeNode),
    edges: spec.edges ?? [],
  };
}

function specFromDag(spec: DagSpec, dag: Dag): DagSpec {
  const nodes = dag.nodes.filter(isCapabilityNode).map((node) => ({
    ...normalizeNode(node),
    status: 'planned' as const,
  }));
  const nodeIds = new Set(nodes.map((node) => node.id));
  return {
    ...spec,
    version: spec.version ?? 1,
    nodes,
    edges: pruneEdgesToNodeIds(dag.edges, nodeIds),
  };
}

function validateDagSpecDraft(spec: DagSpec): string | null {
  if (!spec.id.trim()) return 'DAGSpec id is required.';
  if (!/^[A-Za-z][A-Za-z0-9_-]*$/.test(spec.id)) return 'DAGSpec id must start with a letter and use letters, numbers, _ or -.';
  if (!spec.name.trim()) return 'DAGSpec name is required.';
  const nodeIds = new Set<string>();
  for (const node of spec.nodes) {
    if (!node.id.trim()) return 'Every node needs an id.';
    if (!/^[A-Za-z][A-Za-z0-9_-]*$/.test(node.id)) return `Node '${node.id}' has an invalid id.`;
    if (nodeIds.has(node.id)) return `Node '${node.id}' is duplicated.`;
    nodeIds.add(node.id);
    if (!isCapabilityNode(node) || !node.payload.invocation.capability_id) return `Node '${node.id}' needs a capability.`;
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

type RuntimeMode = 'auto' | 'tool' | 'dag';

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
      content: 'Enter a task, and I will either use tools directly or create and execute a DAG plan when orchestration is useful. Auto mode chooses for you.',
    },
  ]);
  const [draft, setDraft] = useState('');
  const [mode, setMode] = useState<RuntimeMode>('auto');
  const [reviewLevel, setReviewLevel] = useState<ReviewLevel>('fast');
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
  const [consoleLoading, setConsoleLoading] = useState(false);
  const [consoleError, setConsoleError] = useState<string | null>(null);
  const [dagSpecs, setDagSpecs] = useState<DagSpec[]>([]);
  const [editorSpec, setEditorSpec] = useState<DagSpec>(() => createEmptyDagSpec());
  const [editorDag, setEditorDag] = useState<Dag>(() => dagFromSpec(editorSpec));
  const [editorSelectedId, setEditorSelectedId] = useState('');
  const [editorTrace, setEditorTrace] = useState<TraceLogEvent[]>([]);
  const [editorRun, setEditorRun] = useState<DagRun | null>(null);
  const [editorMessage, setEditorMessage] = useState('');
  const [editorRunning, setEditorRunning] = useState(false);
  const [profiles, setProfiles] = useState<AgentProfile[]>([]);
  const [profileWarnings, setProfileWarnings] = useState<ProfileWarning[]>([]);
  const [selectedProfileName, setSelectedProfileName] = useState('');

  const selectedNode = dag.nodes.find((node) => node.id === selectedId) ?? dag.nodes[0];
  const graph = useMemo(() => graphFromDag(dag), [dag]);
  const [nodes, setNodes] = useState<Node[]>(graph.nodes);
  const [edges, setEdges] = useState<Edge[]>(graph.edges);
  const editorGraph = useMemo(() => graphFromDag(editorDag), [editorDag]);
  const [editorNodes, setEditorNodes] = useState<Node[]>(editorGraph.nodes);
  const [editorEdges, setEditorEdges] = useState<Edge[]>(editorGraph.edges);

  const refreshConsoleData = useCallback(async () => {
    setConsoleLoading(true);
    setConsoleError(null);
    try {
      const [nextCapabilities, nextSpecs, nextProfiles] = await Promise.all([
        listCapabilities(),
        listDagSpecs(),
        listProfiles(),
      ]);
      setCapabilities(nextCapabilities);
      setDagSpecs(nextSpecs);
      setProfiles(nextProfiles.profiles);
      setProfileWarnings(nextProfiles.warnings);
      setSelectedProfileName((current) => current || nextProfiles.profiles[0]?.name || '');
    } catch (exc) {
      setConsoleError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setConsoleLoading(false);
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

  const setEditorSpecAndDag = useCallback((spec: DagSpec) => {
    const normalizedSpec = {
      ...spec,
      version: spec.version ?? 1,
      description: spec.description ?? '',
      input_schema: spec.input_schema ?? {},
      artifacts: spec.artifacts ?? {},
      nodes: (spec.nodes ?? []).map(normalizeNode),
      edges: spec.edges ?? [],
      metadata: spec.metadata ?? {},
    };
    setEditorSpec(normalizedSpec);
    syncEditorDag(dagFromSpec(normalizedSpec));
    setEditorTrace([]);
    setEditorRun(null);
    setEditorMessage('');
  }, [syncEditorDag]);

  const patchEditorSpec = (patch: Partial<DagSpec>) => {
    setEditorSpec((current) => ({
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
    setEditorSpec((current) => specFromDag(current, nextDag));
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
    if (event.type === 'capability_result' && event.content?.startsWith('[PENDING_REVIEW]')) return;
    flushQueuedTokensNow();
    updateLastAssistantText((message) => {
      const capabilityEvents = [...(message.capabilityEvents ?? []), event];
      const timeline = [...(message.timeline ?? [])];
      if (event.type === 'capability_result' || event.type === 'capability_error') {
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
      setEditorSpec((current) => specFromDag(current, { ...editorDag, edges: nextDagEdges }));
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

  const newEditorSpec = () => {
    setEditorSpecAndDag(createEmptyDagSpec());
  };

  const loadEditorSpec = (spec: DagSpec) => {
    setEditorSpecAndDag(spec);
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

  const persistEditorSpec = async (): Promise<boolean> => {
    const spec = specFromDag(editorSpec, editorDag);
    const validation = validateDagSpecDraft(spec);
    if (validation) {
      setEditorMessage(validation);
      return false;
    }
    setEditorMessage('Saving DAGSpec...');
    try {
      const saved = await saveDagSpec(spec);
      setEditorSpecAndDag(saved);
      await refreshConsoleData();
      setEditorMessage(`Saved ${saved.id}.`);
      return true;
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
      return false;
    }
  };

  const runEditorSpec = async () => {
    if (editorRunning) return;
    const spec = specFromDag(editorSpec, editorDag);
    const saved = await persistEditorSpec();
    if (!saved) return;
    const validation = validateDagSpecDraft(spec);
    if (validation) return;
    setEditorRunning(true);
    setEditorTrace([]);
    setEditorRun(null);
    setEditorMessage(`Running ${spec.id}...`);
    try {
      await runDagSpecStream(spec.id, {
        onStatus: (status) => {
          setEditorTrace((items) => [
            ...items,
            {
              id: crypto.randomUUID(),
              type: 'model',
              label: status,
              detail: 'DAGSpec run event.',
              status: 'running',
              timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
            },
          ]);
        },
        onTrace: (event) => {
          setEditorTrace((items) => [...items, event]);
        },
        onCapability: (event) => {
          setEditorTrace((items) => [
            ...items,
            {
              id: `${event.invocation_id}-${event.type}-${items.length}`,
              type: 'capability',
              label: event.capability_id,
              detail: event.content || JSON.stringify(event.arguments),
              status: event.type === 'capability_error' ? 'failed' : event.type === 'capability_result' ? 'completed' : 'running',
              timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
            },
          ]);
        },
        onDone: (payload) => {
          setEditorRun(payload.dag_run);
          syncEditorDag(payload.dag_run.dag);
          setEditorMessage(`Run ${payload.dag_run.status}.`);
        },
        onError: (message) => {
          setEditorMessage(message);
        },
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
    stopTokenTimer();
    setStreaming(true);
    setMessages((items) => [
      ...items,
      { role: 'user', kind: 'text', content: prompt },
      { role: 'assistant', kind: 'text', content: '' },
    ]);
    appendTrace({ type: 'model', label: 'runtime_started', detail: `HarnessRuntime mode=${mode}.`, status: 'running' });

    try {
      await streamTask(prompt, mode, reviewLevel, {
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
          flushQueuedTokensNow();
          if (payload.dag) {
            syncDag(payload.dag);
            attachDagToLastAssistant(payload.dag);
            if (shouldOpenDagReview(payload.dag, payload.pending_review)) setReviewOpen(true);
            appendTrace({ type: 'dag', label: 'dag_generated', detail: `Generated ${payload.dag.nodes.length} node(s).`, status: 'completed' });
          }
          handlePendingReview(payload.pending_review);
          enqueueFinalAnswer(payload.final_answer);
          appendTrace({
            type: 'model',
            label: 'runtime_completed',
            detail: payload.dag ? 'DAG loop completed the request.' : 'Capability loop completed the request.',
            status: payload.status === 'failed' ? 'failed' : 'completed',
          });
        },
        onError: (message) => {
          setError(message);
          appendTrace({ type: 'model', label: 'dag_agent_failed', detail: message, status: 'failed' });
        },
      });
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
          flushQueuedTokensNow();
          if (payload.dag) {
            syncDag(payload.dag);
            attachDagToLastAssistant(payload.dag);
            if (shouldOpenDagReview(payload.dag, payload.pending_review)) setReviewOpen(true);
          }
          handlePendingReview(payload.pending_review);
          enqueueFinalAnswer(payload.final_answer);
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
          handlePendingReview(payload.pending_review);
          enqueueFinalAnswer(payload.final_answer);
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
      content: 'Enter a task, and I will either use tools directly or create and execute a DAG plan when orchestration is useful. Auto mode chooses for you.',
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
        <div className="top-actions">
          {activeWorkspace === 'chat' ? (
            <>
              <div className="mode-switch" aria-label="Runtime mode">
                {(['auto', 'dag', 'tool'] as RuntimeMode[]).map((item) => (
                  <button
                    key={item}
                    className={mode === item ? 'active' : ''}
                    onClick={() => setMode(item)}
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
              {dag.nodes.length ? (
                <>
                  <StatusBadge status={dag.status} />
                  <button className="secondary-button compact-button" onClick={() => setReviewOpen(true)} type="button">
                    <GitBranch size={16} />
                    Review DAG
                  </button>
                </>
              ) : null}
            </>
          ) : (
            <button className="secondary-button compact-button" onClick={() => void refreshConsoleData()} disabled={consoleLoading} type="button">
              <RefreshCw size={16} />
              Refresh
            </button>
          )}
        </div>
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
                <button className="icon-button" onClick={newChat} disabled={streaming} title="New chat" type="button">
                  <MessageSquarePlus size={18} />
                </button>
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
            dagSpecs={dagSpecs}
            spec={editorSpec}
            dag={editorDag}
            nodes={editorNodes}
            edges={editorEdges}
            selectedId={editorSelectedId}
            trace={editorTrace}
            run={editorRun}
            message={editorMessage}
            running={editorRunning}
            onNew={newEditorSpec}
            onLoad={loadEditorSpec}
            onPatchSpec={patchEditorSpec}
            onAddNode={addEditorNode}
            onPatchNode={patchEditorNode}
            onDeleteNode={deleteEditorNode}
            onSave={() => void persistEditorSpec()}
            onRun={() => void runEditorSpec()}
            onNodesChange={onEditorNodesChange}
            onEdgesChange={onEditorEdgesChange}
            onConnect={onEditorConnect}
            onSelectNode={setEditorSelectedId}
          />
        ) : activeWorkspace === 'tools' ? (
          <CapabilityDirectory
            capabilities={capabilities}
            onRefresh={refreshConsoleData}
          />
        ) : (
          <AgentDirectory
            profiles={profiles}
            warnings={profileWarnings}
            selectedName={selectedProfileName}
            onSelect={setSelectedProfileName}
          />
        )}
      </main>

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
  const passed = event.type === 'validation_passed' || event.passed === true;
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
  const resultContent = result?.content || (event.type !== 'capability_call' ? event.content || '' : '');
  const isError = result?.type === 'capability_error' || event.type === 'capability_error';
  const isExitError = !isError && hasNonZeroExitCode(resultContent);
  const showError = isError || isExitError;
  const statusLabel = result
    ? (isError ? 'failed' : isExitError ? 'error' : 'done')
    : (event.type === 'capability_call' ? 'running' : event.type === 'capability_error' ? 'failed' : 'done');
  const argsText = formatCapabilityArguments(event.arguments);
  return (
    <details className={`capability-event-card ${showError ? 'capability_error' : event.type}`}>
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
    if (item.type === 'capability' && item.event.invocation_id === invocationId && item.event.type === 'capability_call') {
      return i;
    }
  }
  return -1;
}

function formatCapabilityArguments(value: Record<string, unknown>) {
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

function OrchestrationWorkspace({
  capabilities,
  dagSpecs,
  spec,
  dag,
  nodes,
  edges,
  selectedId,
  trace,
  run,
  message,
  running,
  onNew,
  onLoad,
  onPatchSpec,
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
  dagSpecs: DagSpec[];
  spec: DagSpec;
  dag: Dag;
  nodes: Node[];
  edges: Edge[];
  selectedId: string;
  trace: TraceLogEvent[];
  run: DagRun | null;
  message: string;
  running: boolean;
  onNew: () => void;
  onLoad: (spec: DagSpec) => void;
  onPatchSpec: (patch: Partial<DagSpec>) => void;
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
  const selectedNode = dag.nodes.find((node) => node.id === selectedId) ?? dag.nodes[0];
  const enabledCapabilities = visibleCapabilitiesForPicker(capabilities);
  const contextCapability = enabledCapabilities.find((capability) => capability.id === contextCapabilityId) ?? enabledCapabilities[0];
  const openCanvasMenu = (event: MouseEvent | React.MouseEvent<Element>) => {
    event.preventDefault();
    setContextMenu({ x: event.clientX, y: event.clientY });
    setContextCapabilityId((current) => current || enabledCapabilities[0]?.id || '');
  };
  const openNodeMenu = (event: React.MouseEvent, node: Node) => {
    event.preventDefault();
    event.stopPropagation();
    onSelectNode(node.id);
    setContextMenu({ x: event.clientX, y: event.clientY, nodeId: node.id });
    setContextCapabilityId((current) => current || enabledCapabilities[0]?.id || '');
  };
  const addFromContext = () => {
    if (contextCapability) onAddNode(contextCapability);
    setContextMenu(null);
  };
  const deleteFromContext = () => {
    if (contextMenu?.nodeId) onDeleteNode(contextMenu.nodeId);
    setContextMenu(null);
  };
  return (
    <section className="console-grid orchestration-grid">
      <aside className="console-sidebar">
        <PaneTitle icon={<FileText size={18} />} title="DAGSpecs" />
        <div className="sidebar-actions">
          <button className="secondary-button compact-button" onClick={onNew} type="button">
            <Plus size={16} />
            New
          </button>
          <button className="secondary-button compact-button" onClick={onSave} type="button">
            <Save size={16} />
            Save
          </button>
          <button className="primary-button compact-button" onClick={onRun} disabled={running} type="button">
            <Play size={16} />
            Run
          </button>
        </div>
        <div className="spec-meta-form">
          <label>
            ID
            <input value={spec.id} onChange={(event) => onPatchSpec({ id: event.target.value })} />
          </label>
          <label>
            Name
            <input value={spec.name} onChange={(event) => onPatchSpec({ name: event.target.value })} />
          </label>
          <label>
            Description
            <textarea value={spec.description ?? ''} onChange={(event) => onPatchSpec({ description: event.target.value })} />
          </label>
        </div>
        <div className="resource-list">
          {dagSpecs.length ? dagSpecs.map((item) => (
            <button
              key={item.id}
              className={item.id === spec.id ? 'resource-row active' : 'resource-row'}
              type="button"
              onClick={() => onLoad(item)}
            >
              <strong>{item.name || item.id}</strong>
              <span>{item.nodes.length} nodes</span>
            </button>
          )) : <div className="empty-state compact">No saved DAGSpecs in this process.</div>}
        </div>
      </aside>
      <section className="flow-workbench">
        <div className="workbench-toolbar">
          <div>
            <strong>{spec.name || spec.id}</strong>
            <span>{message || (run ? `Last run: ${run.status}` : 'Draft DAGSpec')}</span>
          </div>
          <select
            onChange={(event) => {
              const capability = enabledCapabilities.find((item) => item.id === event.target.value);
              if (capability) onAddNode(capability);
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
          onNodeClick={(_, node) => onSelectNode(node.id)}
          onPaneClick={() => setContextMenu(null)}
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
      <aside className="console-detail">
        <PaneTitle icon={<SlidersHorizontal size={18} />} title="Node Config" />
        {selectedNode ? (
          <OrchestrationNodeEditor
            node={normalizeNode(selectedNode)}
            dag={dag}
            capabilities={capabilities}
            logs={trace.filter((event) => event.node_id === selectedNode.id)}
            onPatch={(patch, nextEdges) => onPatchNode(selectedNode.id, patch, nextEdges)}
            onDelete={onDeleteNode}
          />
        ) : (
          <div className="empty-state compact">
            Select a node or add one from the capability list.
          </div>
        )}
      </aside>
    </section>
  );
}

function OrchestrationNodeEditor({
  node,
  dag,
  capabilities,
  logs,
  onPatch,
  onDelete,
}: {
  node: DagNode;
  dag: Dag;
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
  const patchInvocation = (patch: Partial<typeof invocation>) =>
    onPatch({ payload: { type: 'capability', invocation: { ...invocation, ...patch } } });
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
            <input
              value={(boundary.allowed_paths ?? []).join(', ')}
              onChange={(event) => patchInvocation({ boundary: { ...boundary, allowed_paths: splitCsv(event.target.value) } })}
            />
          </label>
        </div>
        <label>
          Allowed Commands
          <input
            value={(boundary.allowed_commands ?? []).join(', ')}
            onChange={(event) => patchInvocation({ boundary: { ...boundary, allowed_commands: splitCsv(event.target.value) } })}
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
  onRefresh,
}: {
  capabilities: CapabilityDefinition[];
  onRefresh: () => Promise<void>;
}) {
  const [query, setQuery] = useState('');
  const [draftCapability, setDraftCapability] = useState<CapabilityDefinition>(defaultCustomCapability);
  const [argumentsText, setArgumentsText] = useState('{"text":"hello"}');
  const [selectedId, setSelectedId] = useState('');
  const [result, setResult] = useState<CapabilityResult | null>(null);
  const [message, setMessage] = useState('');
  const filtered = capabilities.filter((capability) => {
    const haystack = `${capability.id} ${capability.name} ${capability.kind} ${capability.description}`.toLowerCase();
    return haystack.includes(query.toLowerCase());
  });
  const selected = capabilities.find((capability) => capability.id === selectedId) ?? filtered[0];
  const grouped = capabilityKinds
    .map((kind) => ({ kind, items: filtered.filter((capability) => capability.kind === kind) }))
    .filter((group) => group.items.length);

  const runCreate = async () => {
    setMessage('Creating custom tool...');
    try {
      await createCapability(draftCapability);
      await onRefresh();
      setMessage(`Created ${draftCapability.id}.`);
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
    if (!selected || selected.kind !== 'custom_tool') return;
    setMessage('Deleting custom tool...');
    try {
      await deleteCapability(selected.id);
      setSelectedId('');
      await onRefresh();
      setMessage(`Deleted ${selected.id}.`);
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  return (
    <section className="console-grid directory-grid">
      <aside className="console-sidebar">
        <PaneTitle icon={<Wrench size={18} />} title="Capabilities" />
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
      </aside>
      <section className="console-detail wide">
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
              <span>Source</span><strong>{selected.kind === 'custom_tool' ? 'custom editable' : 'backend/config readonly'}</strong>
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
                <button className="secondary-button compact-button" onClick={() => void toggleCapability(!selected.enabled)} disabled={selected.kind !== 'custom_tool'} type="button">
                  {selected.enabled ? 'Disable' : 'Enable'}
                </button>
                <button className="secondary-button danger-button compact-button" onClick={removeCapability} disabled={selected.kind !== 'custom_tool'} type="button">
                  Delete
                </button>
              </div>
              {message ? <p className="form-message">{message}</p> : null}
              {result ? <pre>{JSON.stringify(result, null, 2)}</pre> : null}
            </section>
            {selected.kind !== 'custom_tool' ? <div className="readonly-note">This capability is provided by backend configuration and is read-only in the MVP.</div> : null}
          </div>
        ) : <div className="empty-state compact">No capabilities loaded.</div>}
      </section>
      <aside className="console-sidebar">
        <PaneTitle icon={<Plus size={18} />} title="New custom_tool" />
        <div className="spec-meta-form">
          <label>
            ID
            <input
              value={draftCapability.id}
              onChange={(event) => setDraftCapability((current) => ({ ...current, id: event.target.value }))}
            />
          </label>
          <label>
            Name
            <input
              value={draftCapability.name}
              onChange={(event) => setDraftCapability((current) => ({ ...current, name: event.target.value }))}
            />
          </label>
          <label>
            Description
            <textarea
              value={draftCapability.description}
              onChange={(event) => setDraftCapability((current) => ({ ...current, description: event.target.value }))}
            />
          </label>
          <label>
            Template
            <textarea
              value={String(draftCapability.config.template ?? '')}
              onChange={(event) => setDraftCapability((current) => ({
                ...current,
                config: { ...current.config, template: event.target.value },
              }))}
            />
          </label>
          <button className="primary-button" onClick={runCreate} type="button">
            <Plus size={16} />
            Create custom_tool
          </button>
        </div>
      </aside>
    </section>
  );
}

function AgentDirectory({
  profiles,
  warnings,
  selectedName,
  onSelect,
}: {
  profiles: AgentProfile[];
  warnings: ProfileWarning[];
  selectedName: string;
  onSelect: (name: string) => void;
}) {
  const selected = profiles.find((profile) => profile.name === selectedName) ?? profiles[0];
  return (
    <section className="console-grid directory-grid">
      <aside className="console-sidebar">
        <PaneTitle icon={<UserCog size={18} />} title="Profiles" />
        <div className="resource-list">
          {profiles.length ? profiles.map((profile) => (
            <button
              key={profile.name}
              className={selected?.name === profile.name ? 'resource-row active' : 'resource-row'}
              type="button"
              onClick={() => onSelect(profile.name)}
            >
              <strong>{profile.name}</strong>
              <span>{profile.role}</span>
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
                <p>{selected.description || selected.role}</p>
              </div>
              <span className="risk-badge risk-low">{selected.output_format}</span>
            </div>
            <div className="metadata-grid">
              <span>Role</span><strong>{selected.role}</strong>
              <span>Layers</span><strong>{selected.layers.length}</strong>
              <span>Memory file</span><strong>{selected.memory_file || 'none'}</strong>
            </div>
            <div className="profile-layer-list">
              {selected.layers.map((layer) => (
                <section key={layer} className="code-panel">
                  <h3>{layer}</h3>
                  <pre>{selected.layer_contents[layer] || '(empty)'}</pre>
                </section>
              ))}
              <section className="code-panel">
                <h3>Memory</h3>
                <pre>{selected.memory || '(empty)'}</pre>
              </section>
            </div>
            <div className="readonly-note">Profiles are read-only in this MVP. Add or edit profile files on disk, then refresh.</div>
          </div>
        ) : <div className="empty-state compact">Select a profile to inspect prompt layers.</div>}
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
            <input
              value={(boundary.allowed_paths ?? []).join(', ')}
              onChange={(event) =>
                patchInvocation({ boundary: { ...boundary, allowed_paths: splitCsv(event.target.value) } })
              }
            />
          </label>
        </div>
        <label>
          Allowed Commands
          <input
            value={(boundary.allowed_commands ?? []).join(', ')}
            onChange={(event) =>
              patchInvocation({ boundary: { ...boundary, allowed_commands: splitCsv(event.target.value) } })
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
