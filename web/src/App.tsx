import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type React from 'react';
import ReactMarkdown from 'react-markdown';
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
  Play,
  Save,
  Send,
  SlidersHorizontal,
  Wrench,
  X,
} from 'lucide-react';
import { approveDag as approveDagApi, executeDag as executeDagApi, mapTrace, saveDag, streamTask } from './api';
import type { BoundaryMode, Dag, DagNode, RiskLevel, ToolStreamEvent, TraceEvent } from './types';

const riskTone: Record<RiskLevel, string> = {
  low: 'bg-emerald-100 text-emerald-800 border-emerald-300',
  medium: 'bg-amber-100 text-amber-900 border-amber-300',
  high: 'bg-rose-100 text-rose-800 border-rose-300',
};

const boundaryModes: BoundaryMode[] = ['read_only', 'write_limited', 'full'];
const riskLevels: RiskLevel[] = ['low', 'medium', 'high'];
const emptyDag: Dag = {
  dag_id: 'dag_empty',
  task_id: '',
  version: 1,
  status: 'draft',
  nodes: [],
  edges: [],
};

interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  kind?: 'text' | 'tool';
  toolEvent?: ToolStreamEvent;
  dagSnapshot?: Dag;
  traceSnapshot?: TraceEvent[];
}

type RuntimeMode = 'auto' | 'direct' | 'dag_creator';

function graphFromDag(dag: Dag): { nodes: Node[]; edges: Edge[] } {
  const depths = nodeDepths(dag);
  const laneCounts = new Map<number, number>();
  const nodes = dag.nodes.map((item) => {
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
              {item.tools.length ? item.tools.join(', ') : 'no tools'}
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
      content: '输入任务后我会先尝试直接回答。需要复杂编排时，Auto 模式会生成可审阅 DAG；DAG 模式会直接进入规划。',
    },
  ]);
  const [draft, setDraft] = useState('');
  const [mode, setMode] = useState<RuntimeMode>('auto');
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

  const updateDag = useCallback(
    (updater: (current: Dag) => Dag) => {
      const next = updater(dag);
      syncDag(next);
    },
    [dag, syncDag],
  );

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
      traceSnapshot: message.traceSnapshot,
    }));
  };

  const appendTrace = (event: Omit<TraceEvent, 'id' | 'timestamp'>): TraceEvent => {
    const timestamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const nextEvent = { ...event, id: crypto.randomUUID(), timestamp };
    setTrace((items) => [...items, nextEvent]);
    return nextEvent;
  };

  const appendToolMessage = (event: ToolStreamEvent) => {
    flushQueuedTokensNow();
    setMessages((items) => [
      ...items,
      {
        role: 'assistant',
        kind: 'tool',
        content: '',
        toolEvent: event,
      },
    ]);
  };

  const onNodesChange = useCallback((changes: NodeChange[]) => setNodes((nds) => applyNodeChanges(changes, nds)), []);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => setEdges((eds) => applyEdgeChanges(changes, eds)), []);

  const patchSelected = (patch: Partial<DagNode>) => {
    if (!selectedNode) return;
    updateDag((current) => ({
      ...current,
      nodes: current.nodes.map((node) => (node.id === selectedNode.id ? { ...node, ...patch } : node)),
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
    setMessages((items) => [...items, { role: 'user', kind: 'text', content: prompt }]);
    appendTrace({ type: 'model', label: 'agent_loop_started', detail: `HarnessRuntime mode=${mode}.`, status: 'running' });

    try {
      await streamTask(prompt, mode, {
        onStatus: (status) => appendTrace({ type: 'model', label: status, detail: 'Top AgentLoop request accepted.', status: 'running' }),
        onDag: (nextDag) => {
          flushQueuedTokensNow();
          syncDag(nextDag);
          attachDagToLastAssistant(nextDag);
          setReviewOpen(true);
        },
        onTrace: (event) => {
          setTrace((items) => [...items, event]);
        },
        onTool: appendToolMessage,
        onToken: enqueueAssistantToken,
        onDone: (payload) => {
          flushQueuedTokensNow();
          if (payload.dag) {
            syncDag(payload.dag);
            attachDagToLastAssistant(payload.dag);
            setReviewOpen(true);
            appendTrace({ type: 'dag', label: 'dag_generated', detail: `Generated ${payload.dag.nodes.length} node(s).`, status: 'completed' });
          } else {
            appendTrace({ type: 'model', label: 'agent_loop_completed', detail: 'Top AgentLoop returned a direct answer.', status: 'completed' });
          }
        },
        onError: (message) => {
          setError(message);
          appendTrace({ type: 'model', label: 'dag_creator_failed', detail: message, status: 'failed' });
        },
      });
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      appendTrace({ type: 'model', label: 'dag_creator_failed', detail: message, status: 'failed' });
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

  const approveDag = async () => {
    if (!dag.task_id) return;
    setError(null);
    try {
      const nextDag = await approveDagApi(dag.task_id);
      syncDag(nextDag);
      attachDagToLastAssistant(nextDag);
      appendTrace({ type: 'dag', label: 'dag_approved', detail: 'Reviewer approved the current DAG.', status: 'completed' });
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const saveCurrentDag = async () => {
    if (!dag.task_id) return;
    setError(null);
    try {
      const nextDag = await saveDag(dag.task_id, dag);
      syncDag(nextDag);
      attachDagToLastAssistant(nextDag);
      appendTrace({ type: 'dag', label: 'dag_saved', detail: 'Saved edited DAG and reran validation/risk checks.', status: 'completed' });
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const executeDag = async () => {
    if (!dag.task_id) return;
    setError(null);
    const runningDag: Dag = { ...dag, status: 'running' };
    syncDag(runningDag);
    attachDagToLastAssistant(runningDag);
    appendTrace({ type: 'dag', label: 'dag_started', detail: 'Executor started approved DAG.', status: 'running' });
    try {
      const response = await executeDagApi(dag.task_id);
      syncDag(response.dag);
      const mappedTrace = response.result.traces.map(mapTrace);
      setTrace(mappedTrace);
      setMessages((items) => [
        ...items,
        {
          role: 'assistant',
          content: response.message_markdown,
          dagSnapshot: response.dag,
          traceSnapshot: mappedTrace,
        },
      ]);
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      appendTrace({ type: 'dag', label: 'dag_failed', detail: message, status: 'failed' });
      syncDag({ ...dag, status: dag.status === 'running' ? 'review_required' : dag.status });
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
            {(['auto', 'direct', 'dag_creator'] as RuntimeMode[]).map((item) => (
              <button
                key={item}
                className={mode === item ? 'active' : ''}
                onClick={() => setMode(item)}
                type="button"
              >
                {item === 'dag_creator' ? 'DAG' : item}
              </button>
            ))}
          </div>
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
                  <MessageContent content={message.content || (streaming ? '...' : '')} />
                )}
                {message.dagSnapshot?.nodes.length ? (
                  <DagSummaryCard
                    dag={message.dagSnapshot}
                    onOpen={() => {
                      syncDag(message.dagSnapshot!);
                      setReviewOpen(true);
                    }}
                  />
                ) : null}
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
          selectedNode={selectedNode}
          onClose={() => setReviewOpen(false)}
          onApprove={approveDag}
          onExecute={executeDag}
          onSave={saveCurrentDag}
          onPatchNode={patchSelected}
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

function MessageContent({ content }: { content: string }) {
  const parts = useMemo(() => splitThinking(content), [content]);
  return (
    <div className="markdown-body">
      {parts.map((part, index) =>
        part.type === 'think' ? (
          <details key={`${part.type}-${index}`} className="think-block" open={!part.closed}>
            <summary>Thinking</summary>
            <ReactMarkdown>{part.content || '...'}</ReactMarkdown>
          </details>
        ) : (
          <ReactMarkdown key={`${part.type}-${index}`}>{part.content}</ReactMarkdown>
        ),
      )}
    </div>
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

function DagReviewDialog({
  dag,
  nodes,
  edges,
  selectedNode,
  onClose,
  onApprove,
  onExecute,
  onSave,
  onPatchNode,
  onNodesChange,
  onEdgesChange,
  onSelectNode,
}: {
  dag: Dag;
  nodes: Node[];
  edges: Edge[];
  selectedNode?: DagNode;
  onClose: () => void;
  onApprove: () => void;
  onExecute: () => void;
  onSave: () => void;
  onPatchNode: (patch: Partial<DagNode>) => void;
  onNodesChange: (changes: NodeChange[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onSelectNode: (id: string) => void;
}) {
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
            <button className="secondary-button compact-button" onClick={onApprove} disabled={!dag.task_id} type="button">
              <Check size={16} />
              Approve
            </button>
            <button className="primary-button" onClick={onExecute} disabled={dag.status !== 'approved'} type="button">
              <Play size={17} />
              Execute
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
              <NodeEditor node={selectedNode} onPatch={onPatchNode} onSave={onSave} />
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
  onPatch,
  onSave,
}: {
  node: DagNode;
  onPatch: (patch: Partial<DagNode>) => void;
  onSave: () => void;
}) {
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
          Risk
          <select value={node.risk} onChange={(event) => onPatch({ risk: event.target.value as RiskLevel })}>
            {riskLevels.map((risk) => (
              <option key={risk} value={risk}>
                {risk}
              </option>
            ))}
          </select>
        </label>
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
      </div>
      <label>
        Tools
        <input
          value={node.tools.join(', ')}
          onChange={(event) => onPatch({ tools: splitCsv(event.target.value) })}
        />
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
        Expected Output
        <textarea value={node.expected_output} onChange={(event) => onPatch({ expected_output: event.target.value })} />
      </label>
      <button className="secondary-button" onClick={onSave} type="button">
        <Save size={16} />
        Save Draft
      </button>
    </div>
  );
}

function splitCsv(value: string) {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
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
