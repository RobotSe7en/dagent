import type { Dag, TraceLogEvent } from './types';

export const initialDag: Dag = {
  dag_id: 'dag_review_001',
  task_id: 'task_ui_demo',
  version: 1,
  status: 'review_required',
  nodes: [
    {
      id: 'start',
      payload: { type: 'start' },
    },
    {
      id: 'inspect_project',
      payload: {
        type: 'capability',
        invocation: {
          capability_id: 'tool.grep',
          kind: 'tool',
          arguments: { pattern: 'DAG', path: '.' },
          boundary: {
            mode: 'read_only',
            allowed_paths: ['./'],
            allowed_commands: [],
          },
          risk: 'medium',
        },
      },
    },
    {
      id: 'summarize_result',
      payload: {
        type: 'capability',
        invocation: {
          capability_id: 'tool.read_file',
          kind: 'tool',
          arguments: { path: 'README.md' },
          boundary: {
            mode: 'read_only',
            allowed_paths: ['.'],
            allowed_commands: [],
          },
          risk: 'low',
        },
      },
    },
  ],
  edges: [
    { source: 'start', target: 'inspect_project', reason: 'Need plan before inspection.' },
    { source: 'inspect_project', target: 'summarize_result', reason: 'Need facts before summary.' },
  ],
};

export const initialTrace: TraceLogEvent[] = [
  {
    id: 't1',
    type: 'dag',
    label: 'dag_started',
    detail: 'Created review_required DAG.',
    status: 'completed',
    timestamp: '10:32:14',
  },
  {
    id: 't2',
    type: 'node',
    label: 'risk_override',
    detail: 'inspect_project promoted to medium because allowed_paths=["./"].',
    status: 'completed',
    timestamp: '10:32:16',
  },
  {
    id: 't3',
    type: 'capability',
    label: 'read_file',
    detail: 'README.md returned 5.8 KB.',
    status: 'completed',
    timestamp: '10:33:02',
  },
];
