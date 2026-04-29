import { useCallback, useMemo, useState } from 'react';
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
  ShieldAlert,
  SlidersHorizontal,
  Sparkles,
  Wrench,
} from 'lucide-react';
import { approveDag as approveDagApi, executeDag as executeDagApi, mapTrace, saveDag, streamTask } from './api';
import type { BoundaryMode, Dag, DagNode, RiskLevel, TraceEvent } from './types';

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
              {item.tools.length ? item.tools.join(' · ') : 'no tools'}
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
      content: '输入一个任务，我会先进入 HarnessRuntime 的顶层 AgentLoop；只有 Auto 模式下模型调用 dag_creator，或 DAG 模式强制规划时，才会生成 DAG。',
    },
  ]);
  const [draft, setDraft] = useState('');
  const [mode, setMode] = useState<RuntimeMode>('auto');
  const [streaming, setStreaming] = useState(false);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [error, setError] = useState<string | null>(null);

  const selectedNode = dag.nodes.find((node) => node.id === selectedId) ?? dag.nodes[0];
  const graph = useMemo(() => graphFromDag(dag), [dag]);
  const [nodes, setNodes] = useState<Node[]>(graph.nodes);
  const [edges, setEdges] = useState<Edge[]>(graph.edges);

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

  const onNodesChange = useCallback((changes: NodeChange[]) => setNodes((nds) => applyNodeChanges(changes, nds)), []);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => setEdges((eds) => applyEdgeChanges(changes, eds)), []);

  const patchSelected = (patch: Partial<DagNode>) => {
    if (!selectedNode) return;
    updateDag((current) => ({
      ...current,
      nodes: current.nodes.map((node) => (node.id === selectedNode.id ? { ...node, ...patch } : node)),
    }));
  };

  const appendTrace = (event: Omit<TraceEvent, 'id' | 'timestamp'>) => {
    const timestamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    setTrace((items) => [...items, { ...event, id: crypto.randomUUID(), timestamp }]);
  };

  const runStream = async () => {
    if (!draft.trim() || streaming) return;
    const prompt = draft.trim();
    setDraft('');
    setError(null);
    setTrace([]);
    setStreaming(true);
    setMessages((items) => [...items, { role: 'user', content: prompt }, { role: 'assistant', content: '' }]);
    appendTrace({ type: 'model', label: 'agent_loop_started', detail: `HarnessRuntime mode=${mode}.`, status: 'running' });

    try {
      await streamTask(prompt, mode, {
        onStatus: (status) => appendTrace({ type: 'model', label: status, detail: 'Top AgentLoop request accepted.', status: 'running' }),
        onDag: (nextDag) => syncDag(nextDag),
        onTrace: (event) => setTrace((items) => [...items, event]),
        onToken: (content) => {
          setMessages((items) => {
            const copy = [...items];
            const last = copy[copy.length - 1];
            copy[copy.length - 1] = { ...last, content: `${last.content}${content}` };
            return copy;
          });
        },
        onDone: (payload) => {
          if (payload.dag) {
            syncDag(payload.dag);
            appendTrace({ type: 'dag', label: 'dag_generated', detail: `Generated ${payload.dag.nodes.length} node(s).`, status: 'completed' });
          } else {
            appendTrace({ type: 'model', label: 'agent_loop_completed', detail: 'Top AgentLoop returned a direct answer.', status: 'completed' });
          }
        },
        onError: (message) => {
          setError(message);
          appendTrace({ type: 'model', label: 'planner_failed', detail: message, status: 'failed' });
        },
      });
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      appendTrace({ type: 'model', label: 'planner_failed', detail: message, status: 'failed' });
    } finally {
      setStreaming(false);
    }
  };

  const stopStream = () => {
    setStreaming(false);
    appendTrace({ type: 'model', label: 'interrupted', detail: 'The current UI stream was interrupted.', status: 'failed' });
  };

  const approveDag = async () => {
    if (!dag.task_id) return;
    setError(null);
    try {
      const nextDag = await approveDagApi(dag.task_id);
      syncDag(nextDag);
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
      appendTrace({ type: 'dag', label: 'dag_saved', detail: 'Saved edited DAG and reran validation/risk checks.', status: 'completed' });
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const executeDag = async () => {
    if (!dag.task_id) return;
    setError(null);
    syncDag({ ...dag, status: 'running' });
    appendTrace({ type: 'dag', label: 'dag_started', detail: 'Executor started approved DAG.', status: 'running' });
    try {
      const response = await executeDagApi(dag.task_id);
      syncDag(response.dag);
      setTrace(response.result.traces.map(mapTrace));
      setMessages((items) => [...items, { role: 'assistant', content: response.message_markdown }]);
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
          <StatusBadge status={dag.status} />
          <button className="icon-button" onClick={approveDag} disabled={!dag.task_id} title="Approve DAG">
            <Check size={18} />
          </button>
          <button className="primary-button" onClick={executeDag} disabled={dag.status !== 'approved'}>
            <Play size={17} />
            Execute
          </button>
        </div>
      </header>

      <main className="workspace">
        <section className="chat-pane">
          <PaneTitle icon={<Bot size={18} />} title="Conversation" />
          {error ? <div className="error-banner">{error}</div> : null}
          <div className="message-list">
            {messages.map((message, index) => (
              <div key={`${message.role}-${index}`} className={`message ${message.role}`}>
                <span>{message.role}</span>
                <div className="markdown-body">
                  <ReactMarkdown>{message.content || (streaming ? '...' : '')}</ReactMarkdown>
                </div>
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
              <button className="icon-button" onClick={stopStream} disabled={!streaming} title="Stop stream">
                <CircleStop size={18} />
              </button>
              <button className="primary-button" onClick={runStream} disabled={streaming}>
                <Send size={17} />
                Send
              </button>
            </div>
          </div>
        </section>

        <section className="dag-pane">
          <PaneTitle icon={<GitBranch size={18} />} title="DAG Review" />
          <div className="flow-wrap">
            {dag.nodes.length ? (
              <ReactFlow
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onNodeClick={(_, node) => setSelectedId(node.id)}
                fitView
                fitViewOptions={{ padding: 0.2 }}
              >
                <Background color="#d4dad5" gap={18} />
                <MiniMap pannable zoomable nodeColor="#44736f" maskColor="rgba(247,248,245,.68)" />
                <Controls />
              </ReactFlow>
            ) : (
              <div className="empty-state">No DAG yet</div>
            )}
          </div>
        </section>

        <aside className="side-pane">
          <PaneTitle icon={<SlidersHorizontal size={18} />} title="Node Detail" />
          {selectedNode ? (
            <NodeEditor node={selectedNode} onPatch={patchSelected} onSave={saveCurrentDag} />
          ) : (
            <div className="empty-state compact">Generate a DAG to inspect node details.</div>
          )}
          <TraceList trace={trace} />
        </aside>
      </main>
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

function StatusBadge({ status }: { status: Dag['status'] }) {
  return <span className="status-badge">{status}</span>;
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
      <button className="secondary-button" onClick={onSave}>
        <Save size={16} />
        Save Draft
      </button>
    </div>
  );
}

function TraceList({ trace }: { trace: TraceEvent[] }) {
  return (
    <div className="trace-panel">
      <PaneTitle icon={<Wrench size={18} />} title="Trace" />
      <div className="trace-list">
        {trace.length ? (
          trace.map((event) => (
            <div key={event.id} className={`trace-row ${event.status}`}>
              <div className="trace-icon">{event.type === 'tool' ? <Wrench size={15} /> : event.type === 'dag' ? <GitBranch size={15} /> : event.type === 'node' ? <ShieldAlert size={15} /> : <Sparkles size={15} />}</div>
              <div>
                <div className="trace-meta">
                  <span>{event.label}</span>
                  <time>{event.timestamp}</time>
                </div>
                <p>{event.detail}</p>
              </div>
            </div>
          ))
        ) : (
          <div className="empty-state compact">Trace events will appear after planning or execution.</div>
        )}
      </div>
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
