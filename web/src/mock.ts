import type { Dag, TraceEvent } from './types';

export const initialDag: Dag = {
  dag_id: 'dag_review_001',
  task_id: 'task_ui_demo',
  version: 1,
  status: 'review_required',
  nodes: [
    {
      id: 'plan_request',
      invocation: {
        tool_name: 'dag_start',
        arguments: {},
        boundary: {
          mode: 'read_only',
          allowed_paths: [],
          allowed_commands: [],
        },
        risk: 'low',
      },
    },
    {
      id: 'inspect_project',
      invocation: {
        tool_name: 'grep',
        arguments: { pattern: 'DAG', path: '.' },
        boundary: {
          mode: 'read_only',
          allowed_paths: ['./'],
          allowed_commands: [],
        },
        risk: 'medium',
      },
    },
    {
      id: 'summarize_result',
      invocation: {
        tool_name: 'read_file',
        arguments: { path: 'README.md' },
        boundary: {
          mode: 'read_only',
          allowed_paths: [],
          allowed_commands: [],
        },
        risk: 'low',
      },
    },
  ],
  edges: [
    { source: 'plan_request', target: 'inspect_project', reason: 'Need plan before inspection.' },
    { source: 'inspect_project', target: 'summarize_result', reason: 'Need facts before summary.' },
  ],
};

export const initialTrace: TraceEvent[] = [
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
    type: 'tool',
    label: 'read_file',
    detail: 'README.md returned 5.8 KB.',
    status: 'completed',
    timestamp: '10:33:02',
  },
];
