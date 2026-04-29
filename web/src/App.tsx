import { useCallback, useMemo, useRef, useState } from 'react';
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
import { initialDag, initialTrace } from './mock';
import type { BoundaryMode, Dag, DagNode, RiskLevel, TraceEvent } from './types';

const riskTone: Record<RiskLevel, string> = {
  low: 'bg-emerald-100 text-emerald-800 border-emerald-300',
  medium: 'bg-amber-100 text-amber-900 border-amber-300',
  high: 'bg-rose-100 text-rose-800 border-rose-300',
};

const boundaryModes: BoundaryMode[] = ['read_only', 'write_limited', 'full'];
const riskLevels: RiskLevel[] = ['low', 'medium', 'high'];

function graphFromDag(dag: Dag): { nodes: Node[]; edges: Edge[] } {
  const nodes = dag.nodes.map((item, index) => ({
    id: item.id,
    position: { x: 80 + (index % 2) * 330, y: 80 + index * 125 },
    data: {
      label: (
        <div className="dag-node">
          <div className="dag-node-top">
            <span>{item.title}</span>
            <span className={`risk-pill ${riskTone[item.risk]}`}>{item.risk}</span>
          </div>
          <p>{item.goal}</p>
          <div className="dag-node-tools">
            {item.tools.length ? item.tools.join(' · ') : 'no tools'}
          </div>
        </div>
      ),
    },
    type: 'default',
  }));
  const edges = dag.edges.map((edge) => ({
    id: `${edge.source}-${edge.target}`,
    source: edge.source,
    target: edge.target,
    label: edge.reason,
    animated: dag.status === 'running',
    style: { stroke: '#5e7f67', strokeWidth: 2 },
  }));
  return { nodes, edges };
}

export function App() {
  const [dag, setDag] = useState<Dag>(initialDag);
  const [selectedId, setSelectedId] = useState(initialDag.nodes[1].id);
  const [messages, setMessages] = useState([
    { role: 'user', content: 'Summarize what dagent can do today.' },
    { role: 'assistant', content: 'I generated a reviewable DAG and found one medium-risk inspection node.' },
  ]);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [trace, setTrace] = useState<TraceEvent[]>(initialTrace);
  const streamTimer = useRef<number | null>(null);

  const selectedNode = dag.nodes.find((node) => node.id === selectedId) ?? dag.nodes[0];
  const graph = useMemo(() => graphFromDag(dag), [dag]);
  const [nodes, setNodes] = useState<Node[]>(graph.nodes);
  const [edges, setEdges] = useState<Edge[]>(graph.edges);

  const syncGraph = useCallback((nextDag: Dag) => {
    const nextGraph = graphFromDag(nextDag);
    setNodes(nextGraph.nodes);
    setEdges(nextGraph.edges);
  }, []);

  const updateDag = useCallback(
    (updater: (current: Dag) => Dag) => {
      setDag((current) => {
        const next = updater(current);
        syncGraph(next);
        return next;
      });
    },
    [syncGraph],
  );

  const onNodesChange = useCallback((changes: NodeChange[]) => setNodes((nds) => applyNodeChanges(changes, nds)), []);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => setEdges((eds) => applyEdgeChanges(changes, eds)), []);

  const patchSelected = (patch: Partial<DagNode>) => {
    updateDag((current) => ({
      ...current,
      nodes: current.nodes.map((node) => (node.id === selectedNode.id ? { ...node, ...patch } : node)),
    }));
  };

  const appendTrace = (event: Omit<TraceEvent, 'id' | 'timestamp'>) => {
    const timestamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    setTrace((items) => [...items, { ...event, id: crypto.randomUUID(), timestamp }]);
  };

  const runStream = () => {
    if (!draft.trim() || streaming) return;
    const prompt = draft.trim();
    setDraft('');
    setMessages((items) => [...items, { role: 'user', content: prompt }, { role: 'assistant', content: '' }]);
    setStreaming(true);
    appendTrace({ type: 'model', label: 'planner_stream', detail: 'MiniMax-M2.1 started streaming a DAG draft.', status: 'running' });
    const text = 'Planner produced a DAG, Executor promoted broad file access to medium risk, and the plan is waiting for review.';
    let index = 0;
    streamTimer.current = window.setInterval(() => {
      index += 3;
      setMessages((items) => {
        const copy = [...items];
        copy[copy.length - 1] = { role: 'assistant', content: text.slice(0, index) };
        return copy;
      });
      if (index >= text.length && streamTimer.current) {
        window.clearInterval(streamTimer.current);
        streamTimer.current = null;
        setStreaming(false);
        appendTrace({ type: 'model', label: 'planner_stream', detail: 'Assistant stream completed.', status: 'completed' });
      }
    }, 35);
  };

  const stopStream = () => {
    if (streamTimer.current) window.clearInterval(streamTimer.current);
    streamTimer.current = null;
    setStreaming(false);
    appendTrace({ type: 'model', label: 'interrupted', detail: 'Streaming response interrupted by reviewer.', status: 'failed' });
  };

  const approveDag = () => {
    updateDag((current) => ({ ...current, status: 'approved' }));
    appendTrace({ type: 'dag', label: 'dag_approved', detail: 'Reviewer approved the current DAG.', status: 'completed' });
  };

  const executeDag = () => {
    updateDag((current) => ({ ...current, status: 'running' }));
    appendTrace({ type: 'dag', label: 'dag_started', detail: 'Executor started approved DAG.', status: 'running' });
    window.setTimeout(() => {
      updateDag((current) => ({ ...current, status: 'completed' }));
      appendTrace({ type: 'dag', label: 'dag_completed', detail: 'All nodes completed.', status: 'completed' });
    }, 900);
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
          <StatusBadge status={dag.status} />
          <button className="icon-button" onClick={approveDag} title="Approve DAG">
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
          <div className="message-list">
            {messages.map((message, index) => (
              <div key={`${message.role}-${index}`} className={`message ${message.role}`}>
                <span>{message.role}</span>
                <p>{message.content || (streaming ? '...' : '')}</p>
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
            <ReactFlow
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onNodeClick={(_, node) => setSelectedId(node.id)}
              fitView
            >
              <Background color="#d9ded7" gap={18} />
              <MiniMap pannable zoomable nodeColor="#5e7f67" maskColor="rgba(247,248,245,.65)" />
              <Controls />
            </ReactFlow>
          </div>
        </section>

        <aside className="side-pane">
          <PaneTitle icon={<SlidersHorizontal size={18} />} title="Node Detail" />
          <NodeEditor node={selectedNode} onPatch={patchSelected} />
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

function NodeEditor({ node, onPatch }: { node: DagNode; onPatch: (patch: Partial<DagNode>) => void }) {
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
      <button className="secondary-button">
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
        {trace.map((event) => (
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
        ))}
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

