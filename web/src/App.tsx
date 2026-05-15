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
  applyEdgeChanges,
  applyNodeChanges,
  type EdgeChange,
  type NodeChange,
} from '@xyflow/react';
import {
  AlertTriangle,
  Bot,
  Check,
  CircleStop,
  GitBranch,
  Loader,
  MessageSquarePlus,
  Plus,
  Send,
  SlidersHorizontal,
  Trash2,
  Wrench,
  X,
} from 'lucide-react';
import { getValidationStatus, setValidationEnabled as apiSetValidation, resetSession, resumeDagReview, resumeToolReview, streamTask } from './api';
import type { BoundaryMode, Dag, DagEdge, DagNode, ReviewEventPayload, CapabilityKind, ValidationFeedbackEvent, ReviewLevel, RiskLevel, ToolCallPayload, ToolStreamEvent, TraceEvent } from './types';

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

function normalizeNode(node: DagNode): DagNode {
  const invocation = node.invocation;
  return {
    ...node,
    invocation: {
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
    },
    status: node.status ?? 'planned',
  };
}

interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  kind?: 'text' | 'tool';
  toolEvent?: ToolStreamEvent;
  toolEvents?: ToolStreamEvent[];
  timeline?: MessageTimelineItem[];
  dagSnapshot?: Dag;
  traceSnapshot?: TraceEvent[];
}

type MessageTimelineItem =
  | { type: 'text'; content: string }
  | { type: 'dag'; dag: Dag }
  | { type: 'tool'; event: ToolStreamEvent; result?: ToolStreamEvent }
  | { type: 'validation'; event: ValidationFeedbackEvent }
  | { type: 'validating' };

type RuntimeMode = 'auto' | 'tool' | 'dag';

function graphFromDag(dag: Dag): { nodes: Node[]; edges: Edge[] } {
  const depths = nodeDepths(dag);
  const laneCounts = new Map<number, number>();
  const nodes = dag.nodes.map((rawItem) => {
    const item = normalizeNode(rawItem);
    const risk = item.invocation.risk ?? 'low';
    const status = item.status ?? 'planned';
    const depth = depths.get(item.id) ?? 0;
    const lane = laneCounts.get(depth) ?? 0;
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
              title={item.invocation.capability_id ? JSON.stringify(item.invocation.arguments) : ''}
            >
              {item.invocation.capability_id
                ? `${item.invocation.capability_id} ${JSON.stringify(item.invocation.arguments)}`
                : 'capability not set'}
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
  const [dag, setDag] = useState<Dag>(emptyDag);
  const [selectedId, setSelectedId] = useState<string>('');
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'assistant',
      content: '输入任务后，我会通过 Tool 模式直接使用工具，或在需要编排时通过 DAG 模式生成并执行计划。Auto 模式会自动选择。',
    },
  ]);
  const [draft, setDraft] = useState('');
  const [mode, setMode] = useState<RuntimeMode>('auto');
  const [reviewLevel, setReviewLevel] = useState<ReviewLevel>('fast');
  const [streaming, setStreaming] = useState(false);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [validationEnabled, setValidationEnabled] = useState(false);
  const [validationPending, setValidationPending] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [dagReview, setDagReview] = useState<ReviewEventPayload | null>(null);
  const [toolReview, setToolReview] = useState<ReviewEventPayload | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const validationRequestIdRef = useRef(0);
  const tokenQueueRef = useRef<string[]>([]);
  const tokenTimerRef = useRef<number | null>(null);
  const tokenDrainResolversRef = useRef<Array<() => void>>([]);

  const selectedNode = dag.nodes.find((node) => node.id === selectedId) ?? dag.nodes[0];
  const graph = useMemo(() => graphFromDag(dag), [dag]);
  const [nodes, setNodes] = useState<Node[]>(graph.nodes);
  const [edges, setEdges] = useState<Edge[]>(graph.edges);

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
    if (pendingReview.kind === 'tool_review') {
      setToolReview(pendingReview as ReviewEventPayload);
      return;
    }
    setDagReview(pendingReview);
  };

  const appendTrace = (event: Omit<TraceEvent, 'id' | 'timestamp'>): TraceEvent => {
    const timestamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const nextEvent = { ...event, id: crypto.randomUUID(), timestamp };
    setTrace((items) => [...items, nextEvent]);
    return nextEvent;
  };

  const appendRuntimeTrace = (event: TraceEvent) => {
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

  const appendToolMessage = (event: ToolStreamEvent) => {
    if (event.type === 'tool_result' && event.content?.startsWith('[PENDING_REVIEW]')) return;
    flushQueuedTokensNow();
    updateLastAssistantText((message) => {
      const toolEvents = [...(message.toolEvents ?? []), event];
      const timeline = [...(message.timeline ?? [])];
      if (event.type === 'tool_result' || event.type === 'tool_error') {
        const idx = findMatchingToolCall(timeline, event.tool_call_id);
        if (idx !== -1) {
          const item = timeline[idx] as { type: 'tool'; event: ToolStreamEvent; result?: ToolStreamEvent };
          timeline[idx] = { ...item, result: event };
          return { ...message, toolEvents, timeline };
        }
      }
      timeline.push({ type: 'tool', event });
      return { ...message, toolEvents, timeline };
    });
  };

  const onNodesChange = useCallback((changes: NodeChange[]) => setNodes((nds) => applyNodeChanges(changes, nds)), []);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => setEdges((eds) => applyEdgeChanges(changes, eds)), []);

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
    updateDag((current) => ({
      ...current,
      status: 'draft',
      nodes: [
        ...current.nodes,
        normalizeNode({
          id,
          invocation: {
            capability_id: current.nodes[0]?.invocation.capability_id ?? '',
            kind: current.nodes[0]?.invocation.kind ?? 'tool',
            arguments: {},
            boundary: {
              mode: 'read_only',
              allowed_paths: [],
              allowed_commands: [],
            },
            risk: 'low',
          },
          status: 'planned',
        }),
      ],
    }));
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
        onTool: appendToolMessage,
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
            detail: payload.dag ? 'DAGAgentLoop completed the request.' : 'ToolAgentLoop completed the request.',
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
        onTool: appendToolMessage,
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
          appendTrace({ type: 'model', label: 'runtime_completed', detail: 'DAGAgentLoop completed the request.', status: 'completed' });
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

  const confirmToolReview = async (approved: boolean) => {
    if (!toolReview || streaming) return;
    setToolReview(null);
    setError(null);
    tokenQueueRef.current = [];
    stopTokenTimer();
    setStreaming(true);
    setMessages((items) => [
      ...items,
      { role: 'assistant', kind: 'text', content: '' },
    ]);
    appendTrace({ type: 'model', label: 'tool_review_resumed', detail: `Tool review ${approved ? 'approved' : 'rejected'}.`, status: 'running' });

    try {
      await resumeToolReview(toolReview.review_id, approved, {
        onStatus: (status) => appendTrace({ type: 'model', label: status, detail: 'ToolAgentLoop resumed from tool review.', status: 'running' }),
        onToken: enqueueAssistantToken,
        onRetry: appendValidationFeedback,
        onValidating: appendValidating,
        onDone: (payload) => {
          flushQueuedTokensNow();
          handlePendingReview(payload.pending_review);
          enqueueFinalAnswer(payload.final_answer);
          appendTrace({ type: 'model', label: 'runtime_completed', detail: 'ToolAgentLoop completed the request.', status: 'completed' });
        },
        onError: (message) => {
          setError(message);
          appendTrace({ type: 'model', label: 'tool_review_failed', detail: message, status: 'failed' });
        },
      });
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      appendTrace({ type: 'model', label: 'tool_review_failed', detail: message, status: 'failed' });
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
      setToolReview(null);
    } catch (exc) {
      setValidationError(exc instanceof Error ? exc.message : String(exc));
    }
    setMessages([{
      role: 'assistant',
      content: '输入任务后，我会通过 Tool 模式直接使用工具，或在需要编排时通过 DAG 模式生成并执行计划。Auto 模式会自动选择。',
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
        <div className="top-actions">
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
        </div>
      </header>

      <main className="workspace">
        <section className="chat-pane">
          <PaneTitle icon={<Bot size={18} />} title="Conversation" />
          {error ? <div className="error-banner">{error}</div> : null}
          <div className="message-list" ref={messageListRef}>
            {messages.map((message, index) => (
              <div key={`${message.role}-${index}`} className={`message ${message.role} ${message.kind ?? 'text'}`}>
                <span>{message.kind === 'tool' ? 'tool' : message.role}</span>
                {message.kind === 'tool' && message.toolEvent ? (
                  <ToolEventCard event={message.toolEvent} />
                ) : (
                  <MessageTimeline
                    message={message}
                    loading={streaming}
                    onOpenDag={(snapshot, snapshotTrace) => {
                      syncDag(snapshot);
                      if (snapshotTrace) setTrace(snapshotTrace);
                      setReviewOpen(true);
                    }}
                  />
                )}
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

      {toolReview ? (
        <ToolReviewDialog
          review={toolReview}
          onApprove={() => confirmToolReview(true)}
          onReject={() => confirmToolReview(false)}
          onClose={() => setToolReview(null)}
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
  onOpenDag: (dag: Dag, trace?: TraceEvent[]) => void;
}) {
  if (!message.timeline?.length) {
    return <MessageContent content={message.content || (loading ? '...' : '')} />;
  }

  return (
    <div className="message-timeline">
      {message.timeline.map((item, index) =>
        item.type === 'tool' ? (
          <ToolEventCard key={`${item.event.tool_call_id}-${index}`} event={item.event} result={item.result} />
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
  const riskyNodes = dag.nodes.filter((node) => node.invocation.risk !== 'low').length;
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
    <details className="tool-event-card">
      <summary className="tool-event-head">
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
    <details className={`tool-event-card ${passed ? 'validation-passed' : 'validation-feedback'}`}>
      <summary className="tool-event-head">
        {passed ? <Check size={14} /> : <AlertTriangle size={14} />}
        <strong>Validation {passed ? 'Passed' : 'Feedback'}</strong>
        <span>{passed ? 'passed' : 'retry'}</span>
      </summary>
      {event.summary ? (
        <div className="tool-section">
          <div className="tool-section-label">Summary</div>
          <p>{event.summary}</p>
        </div>
      ) : null}
      {!passed && event.issues.length ? (
        <div className="tool-section">
          <div className="tool-section-label">Issues</div>
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
        <div className="tool-section">
          <div className="tool-section-label">Feedback to Agent</div>
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

function ToolEventCard({ event, result }: { event: ToolStreamEvent; result?: ToolStreamEvent }) {
  const resultContent = result?.content || (event.type !== 'tool_call' ? event.content || '' : '');
  const isError = result?.type === 'tool_error' || event.type === 'tool_error';
  const isExitError = !isError && hasNonZeroExitCode(resultContent);
  const showError = isError || isExitError;
  const statusLabel = result
    ? (isError ? 'failed' : isExitError ? 'error' : 'done')
    : (event.type === 'tool_call' ? 'running' : event.type === 'tool_error' ? 'failed' : 'done');
  const argsText = formatToolArguments(event.arguments);
  return (
    <details className={`tool-event-card ${showError ? 'tool_error' : event.type}`}>
      <summary className="tool-event-head">
        <Wrench size={14} />
        <strong>{event.name}</strong>
        <span>{statusLabel}</span>
      </summary>
      {argsText ? (
        <div className="tool-section">
          <div className="tool-section-label">Args</div>
          <pre>{clipText(argsText, 800)}</pre>
        </div>
      ) : null}
      {resultContent ? (
        <div className="tool-section">
          <div className="tool-section-label">{showError ? 'Error' : 'Result'}</div>
          <pre>{clipText(resultContent, 1200)}</pre>
        </div>
      ) : null}
    </details>
  );
}

function findMatchingToolCall(timeline: MessageTimelineItem[], toolCallId: string): number {
  for (let i = timeline.length - 1; i >= 0; i--) {
    const item = timeline[i];
    if (item.type === 'tool' && item.event.tool_call_id === toolCallId && item.event.type === 'tool_call') {
      return i;
    }
  }
  return -1;
}

function formatToolArguments(value: Record<string, unknown>) {
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

function ToolReviewDialog({
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
  const toolCall = review.tool_call;
  const argsText = toolCall ? JSON.stringify(toolCall.arguments, null, 2) : '';
  const risk = (review.payload?.risk as string) || 'low';
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Tool review">
      <div className="dag-modal">
        <header className="modal-header">
          <div>
            <div className="modal-title">
              <AlertTriangle size={20} />
              <span>Tool Review</span>
              <span className={`risk-badge risk-${risk}`}>{risk.toUpperCase()}</span>
            </div>
            <p>{toolCall?.name || review.message}</p>
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
          {toolCall ? (
            <div className="tool-section">
              <div className="tool-section-label">Tool</div>
              <p><strong>{toolCall.name}</strong></p>
            </div>
          ) : null}
          {argsText ? (
            <div className="tool-section">
              <div className="tool-section-label">Arguments</div>
              <pre>{clipText(argsText, 1200)}</pre>
            </div>
          ) : null}
        </div>
      </div>
    </div>
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
  trace: TraceEvent[];
  selectedNode?: DagNode;
  onClose: () => void;
  onConfirm: () => void;
  onReject: () => void;
  onPatchNode: (patch: Partial<DagNode>, edges?: DagEdge[]) => void;
  onAddNode: () => void;
  onDeleteNode: () => void;
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
            <button className="secondary-button compact-button" onClick={onAddNode} type="button">
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
                onDelete={onDeleteNode}
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
  logs: TraceEvent[];
  onPatch: (patch: Partial<DagNode>, edges?: DagEdge[]) => void;
  onDelete: () => void;
}) {
  const dependsOn = dag.edges.filter((edge) => edge.target === node.id).map((edge) => edge.source);
  const invocation = node.invocation;
  const boundary = invocation.boundary ?? {
    mode: 'read_only' as BoundaryMode,
    allowed_paths: [],
    allowed_commands: [],
  };
  const patchInvocation = (patch: Partial<typeof invocation>) =>
    onPatch({ invocation: { ...invocation, ...patch } });
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
      <button className="secondary-button danger-button" onClick={onDelete} type="button">
        <Trash2 size={16} />
        Delete Node
      </button>
      <NodeExecutionLog logs={logs} />
    </div>
  );
}

function NodeExecutionLog({ logs }: { logs: TraceEvent[] }) {
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
