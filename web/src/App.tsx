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
  Bot,
  Check,
  CircleStop,
  GitBranch,
  Plus,
  Send,
  SlidersHorizontal,
  Trash2,
  Wrench,
  X,
} from 'lucide-react';
import { resumeDag, streamTask } from './api';
import type { BoundaryMode, Dag, DagEdge, DagNode, ReviewLevel, RiskLevel, ToolStreamEvent, TraceEvent } from './types';

const riskTone: Record<RiskLevel, string> = {
  low: 'bg-emerald-100 text-emerald-800 border-emerald-300',
  medium: 'bg-amber-100 text-amber-900 border-amber-300',
  high: 'bg-rose-100 text-rose-800 border-rose-300',
};

const riskLevels: RiskLevel[] = ['low', 'medium', 'high'];
const boundaryModes: BoundaryMode[] = ['read_only', 'write_limited', 'full'];
const reviewLevels: ReviewLevel[] = ['balanced', 'fast', 'careful', 'manual'];
const emptyDag: Dag = {
  dag_id: 'dag_empty',
  task_id: '',
  version: 1,
  status: 'draft',
  nodes: [],
  edges: [],
};

function normalizeNode(node: DagNode): DagNode {
  return {
    ...node,
    kind: node.kind ?? (node.tool ? 'tool' : 'agent'),
    tool: node.tool ?? null,
    args: node.args ?? {},
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
  | { type: 'tool'; event: ToolStreamEvent };

type RuntimeMode = 'auto' | 'direct' | 'dag';

function graphFromDag(dag: Dag): { nodes: Node[]; edges: Edge[] } {
  const depths = nodeDepths(dag);
  const laneCounts = new Map<number, number>();
  const nodes = dag.nodes.map((rawItem) => {
    const item = normalizeNode(rawItem);
    const depth = depths.get(item.id) ?? 0;
    const lane = laneCounts.get(depth) ?? 0;
    laneCounts.set(depth, lane + 1);
    return {
      id: item.id,
      position: { x: 80 + depth * 300, y: 70 + lane * 170 },
      data: {
        label: (
          <div className="dag-node">
            <div className="dag-node-top">
              <span title={item.title}>{item.title}</span>
              <span className={`risk-pill ${riskTone[item.risk]}`}>{item.risk}</span>
            </div>
            <p title={item.goal}>{item.goal}</p>
            <div className="dag-node-tools" title={item.tools.join(', ')}>
              {item.kind === 'tool' && item.tool
                ? `${item.tool} ${JSON.stringify(item.args)}`
                : item.tools.length
                  ? item.tools.join(', ')
                  : 'agent'}
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
    style: { stroke: '#44736f', strokeWidth: 2 },
  }));
  return { nodes, edges };
}

export function App() {
  const [dag, setDag] = useState<Dag>(emptyDag);
  const [selectedId, setSelectedId] = useState<string>('');
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'assistant',
      content: '输入任务后，我会优先通过顶层 AgentLoop 完成规划、执行、重试和最终回答。Auto 模式会在需要编排时生成并执行 DAG。',
    },
  ]);
  const [draft, setDraft] = useState('');
  const [mode, setMode] = useState<RuntimeMode>('auto');
  const [reviewLevel, setReviewLevel] = useState<ReviewLevel>('balanced');
  const [streaming, setStreaming] = useState(false);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const tokenQueueRef = useRef<string[]>([]);
  const tokenTimerRef = useRef<number | null>(null);
  const tokenDrainResolversRef = useRef<Array<() => void>>([]);

  const selectedNode = dag.nodes.find((node) => node.id === selectedId) ?? dag.nodes[0];
  const graph = useMemo(() => graphFromDag(dag), [dag]);
  const [nodes, setNodes] = useState<Node[]>(graph.nodes);
  const [edges, setEdges] = useState<Edge[]>(graph.edges);

  useEffect(() => {
    const element = messageListRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
  }, [messages, streaming]);

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
    tokenQueueRef.current.push(content);
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
    Boolean(pendingReview) || ['review_required', 'paused_for_replan', 'paused_for_permission'].includes(nextDag.status);

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

  const appendToolMessage = (event: ToolStreamEvent) => {
    flushQueuedTokensNow();
    updateLastAssistantText((message) => ({
      ...message,
      toolEvents: [...(message.toolEvents ?? []), event],
      timeline: [...(message.timeline ?? []), { type: 'tool', event }],
    }));
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
          title: 'New Node',
          goal: 'Describe the next action.',
          kind: 'tool',
          tool: current.nodes[0]?.tool ?? null,
          args: {},
          agent: null,
          tools: current.nodes[0]?.tool ? [current.nodes[0].tool] : [],
          skills: [],
          boundary: {
            mode: 'read_only',
            allowed_paths: [],
            forbidden_tools: [],
            allowed_commands: [],
            forbidden_commands: [],
          },
          risk: 'low',
          risk_reason: 'User added node.',
          expected_output: 'Result.',
          max_steps: 1,
          timeout_seconds: 30,
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
    appendTrace({ type: 'model', label: 'agent_loop_started', detail: `HarnessRuntime mode=${mode}.`, status: 'running' });

    try {
      await streamTask(prompt, mode, reviewLevel, {
        onStatus: (status) => appendTrace({ type: 'model', label: status, detail: 'Top AgentLoop request accepted.', status: 'running' }),
        onDag: (nextDag) => {
          flushQueuedTokensNow();
          syncDag(nextDag);
          attachDagToLastAssistant(nextDag);
          setReviewOpen(true);
        },
          onTrace: appendRuntimeTrace,
        onTool: appendToolMessage,
        onToken: enqueueAssistantToken,
        onDone: (payload) => {
          flushQueuedTokensNow();
          if (payload.dag) {
            syncDag(payload.dag);
            attachDagToLastAssistant(payload.dag);
            if (shouldOpenDagReview(payload.dag, payload.pending_review)) setReviewOpen(true);
            appendTrace({ type: 'dag', label: 'dag_generated', detail: `Generated ${payload.dag.nodes.length} node(s).`, status: 'completed' });
          } else {
            updateLastAssistantText((message) => ({
              ...message,
              content: message.content || payload.message_markdown,
              timeline: ensureTextTimeline(message.timeline, payload.message_markdown),
            }));
            appendTrace({ type: 'model', label: 'agent_loop_completed', detail: 'Top AgentLoop returned a direct answer.', status: 'completed' });
          }
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

  const confirmDag = async () => {
    if (!dag.task_id || streaming) return;
    setError(null);
    setReviewOpen(false);
    tokenQueueRef.current = [];
    stopTokenTimer();
    setStreaming(true);
    setMessages((items) => [
      ...items,
      { role: 'assistant', kind: 'text', content: '' },
    ]);
    appendTrace({ type: 'dag', label: 'dag_confirmed', detail: `Resuming task ${dag.task_id}.`, status: 'running' });

    try {
      await resumeDag(dag.task_id, dag, reviewLevel, {
        onStatus: (status) => appendTrace({ type: 'model', label: status, detail: 'Top AgentLoop resumed from DAG review.', status: 'running' }),
        onDag: (nextDag) => {
          syncDag(nextDag);
          attachDagToLastAssistant(nextDag);
        },
          onTrace: appendRuntimeTrace,
        onTool: appendToolMessage,
        onToken: enqueueAssistantToken,
        onDone: (payload) => {
          flushQueuedTokensNow();
          if (payload.dag) {
            syncDag(payload.dag);
            attachDagToLastAssistant(payload.dag);
            if (shouldOpenDagReview(payload.dag, payload.pending_review)) setReviewOpen(true);
          }
          updateLastAssistantText((message) => ({
            ...message,
            content: message.content || payload.message_markdown,
            timeline: ensureTextTimeline(message.timeline, payload.message_markdown),
          }));
          appendTrace({ type: 'model', label: 'agent_loop_completed', detail: 'Top AgentLoop summarized the DAG result.', status: 'completed' });
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
            {(['auto', 'dag', 'direct'] as RuntimeMode[]).map((item) => (
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
                {message.traceSnapshot?.length ? <TraceQueue trace={message.traceSnapshot} /> : null}
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
          onPatchNode={patchSelected}
          onAddNode={addNode}
          onDeleteNode={deleteSelected}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onSelectNode={setSelectedId}
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
          <ToolEventCard key={`${item.event.tool_call_id}-${item.event.type}-${index}`} event={item.event} />
        ) : item.type === 'dag' ? (
          <DagSummaryCard
            key={`${item.dag.task_id || item.dag.dag_id}-${index}`}
            dag={item.dag}
            onOpen={() => onOpenDag(item.dag, message.traceSnapshot)}
          />
        ) : item.content ? (
          <MessageContent key={`text-${index}`} content={item.content} />
        ) : null,
      )}
      {!message.content && loading ? <MessageContent content="..." /> : null}
    </div>
  );
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
        ) : (
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
    items[items.length - 1] = { ...last, content: `${last.content}${content}` };
  } else {
    items.push({ type: 'text', content });
  }
  return items;
}

function ensureTextTimeline(
  timeline: MessageTimelineItem[] | undefined,
  content: string,
): MessageTimelineItem[] {
  if (!content) return timeline ?? [];
  if ((timeline ?? []).some((item) => item.type === 'text' && item.content.trim())) {
    return timeline ?? [];
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
    (item) => item.type === 'dag' && (item.dag.task_id || item.dag.dag_id) === dagKey,
  );
  if (existingIndex !== -1) {
    items[existingIndex] = { type: 'dag', dag };
    return items;
  }
  const last = items[items.length - 1];
  if (last?.type === 'dag') {
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
  return <span className="status-badge">{status}</span>;
}

function DagSummaryCard({
  dag,
  onOpen,
}: {
  dag: Dag;
  onOpen: () => void;
}) {
  const riskyNodes = dag.nodes.filter((node) => node.risk !== 'low').length;
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
        <span>open flow</span>
      </div>
    </button>
  );
}

function ToolEventCard({ event }: { event: ToolStreamEvent }) {
  const isCall = event.type === 'tool_call';
  const isError = event.type === 'tool_error';
  const detail = isCall ? formatToolArguments(event.arguments) : event.content || '';
  return (
    <details className={`tool-event-card ${event.type}`}>
      <summary className="tool-event-head">
        <Wrench size={14} />
        <strong>{event.name}</strong>
        <span>{isCall ? 'calling' : isError ? 'failed' : 'result'}</span>
      </summary>
      {detail ? <pre>{clipText(detail, 1200)}</pre> : null}
    </details>
  );
}

function TraceQueue({ trace }: { trace: TraceEvent[] }) {
  return (
    <div className="message-trace">
      <div className="message-trace-title">
        <Wrench size={14} />
        <span>Trace</span>
      </div>
      <div className="message-trace-list">
        {trace.map((event) => (
          <div key={event.id} className={`message-trace-row ${event.status}`}>
            <span>{event.label}</span>
            <em>{event.status}</em>
          </div>
        ))}
      </div>
    </div>
  );
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

function DagReviewDialog({
  dag,
  nodes,
  edges,
  trace,
  selectedNode,
  onClose,
  onConfirm,
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
  onPatchNode: (patch: Partial<DagNode>, edges?: DagEdge[]) => void;
  onAddNode: () => void;
  onDeleteNode: () => void;
  onNodesChange: (changes: NodeChange[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onSelectNode: (id: string) => void;
}) {
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
            <button className="primary-button" onClick={onConfirm} disabled={!dag.nodes.length} type="button">
              <Check size={17} />
              Confirm & Resume
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
              <Background color="#d4dad5" gap={18} />
              <MiniMap pannable zoomable nodeColor="#44736f" maskColor="rgba(247,248,245,.68)" />
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
  return (
    <div className="node-editor">
      <label>
        Title
        <input value={node.title} onChange={(event) => onPatch({ title: event.target.value })} />
      </label>
      <label>
        Goal
        <textarea value={node.goal} onChange={(event) => onPatch({ goal: event.target.value })} />
      </label>
      <div className="two-col">
        <label>
          Kind
          <select value={node.kind} onChange={(event) => onPatch({ kind: event.target.value as DagNode['kind'] })}>
            <option value="tool">tool</option>
            <option value="agent">agent</option>
          </select>
        </label>
        <label>
          Risk
          <select value={node.risk} onChange={(event) => onPatch({ risk: event.target.value as RiskLevel })}>
            {riskLevels.map((risk) => (
              <option key={risk} value={risk}>
                {risk}
              </option>
            ))}
          </select>
        </label>
      </div>
      {node.kind === 'tool' ? (
        <>
          <label>
            Tool
            <input
              value={node.tool ?? ''}
              onChange={(event) =>
                onPatch({
                  tool: event.target.value || null,
                  tools: event.target.value ? [event.target.value] : [],
                })
              }
            />
          </label>
          <label>
            Args JSON
            <textarea
              value={JSON.stringify(node.args ?? {}, null, 2)}
              onChange={(event) => {
                const parsed = parseJsonObject(event.target.value);
                if (parsed) onPatch({ args: parsed });
              }}
            />
          </label>
        </>
      ) : null}
      <div className="two-col">
        <label>
          Boundary
          <select
            value={node.boundary.mode}
            onChange={(event) =>
              onPatch({ boundary: { ...node.boundary, mode: event.target.value as BoundaryMode } })
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
          Status
          <input value={node.status ?? 'planned'} disabled />
        </label>
      </div>
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
      <label>
        Tools
        <input value={node.tools.join(', ')} onChange={(event) => onPatch({ tools: splitCsv(event.target.value) })} />
      </label>
      <label>
        Allowed Paths
        <input
          value={node.boundary.allowed_paths.join(', ')}
          onChange={(event) =>
            onPatch({ boundary: { ...node.boundary, allowed_paths: splitCsv(event.target.value) } })
          }
        />
      </label>
      <label>
        Allowed Commands
        <input
          value={node.boundary.allowed_commands.join(', ')}
          onChange={(event) =>
            onPatch({ boundary: { ...node.boundary, allowed_commands: splitCsv(event.target.value) } })
          }
        />
      </label>
      <label>
        Expected Output
        <textarea value={node.expected_output} onChange={(event) => onPatch({ expected_output: event.target.value })} />
      </label>
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
