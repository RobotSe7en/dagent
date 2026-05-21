import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

async function importTypeScript(relativePath) {
  const sourceUrl = new URL(relativePath, import.meta.url);
  const source = await readFile(sourceUrl, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const dataUrl = `data:text/javascript;base64,${Buffer.from(output).toString('base64')}`;
  return import(dataUrl);
}

const {
  buildSchemaArgumentFields,
  ensureSchemaArguments,
  resetSchemaArguments,
  visibleCapabilitiesForPicker,
} = await importTypeScript('../src/schemaArguments.ts');
const { pruneEdgesToNodeIds } = await importTypeScript('../src/dagEdges.ts');
const {
  artifactPlaceholder,
  createUploadedFileArtifacts,
  isUploadedFileArtifact,
  removeArtifactBinding,
  uploadFormFilename,
  upsertArtifact,
} = await importTypeScript('../src/dagArtifacts.ts');

test('ensureSchemaArguments adds schema-backed defaults and keeps extras', () => {
  const parameters = {
    type: 'object',
    properties: {
      command: { type: 'string' },
      cwd: { type: 'string', default: '.' },
      timeout_seconds: { type: 'integer', default: 30 },
    },
    required: ['command'],
  };

  assert.deepEqual(ensureSchemaArguments({ debug: true }, parameters), {
    debug: true,
    command: '',
    cwd: '.',
    timeout_seconds: 30,
  });
});

test('buildSchemaArgumentFields marks schema fields as fixed before extra fields', () => {
  const parameters = {
    type: 'object',
    properties: {
      path: { type: 'string', description: 'File path to read.' },
      pattern: { type: 'string' },
    },
    required: ['path'],
  };

  const fields = buildSchemaArgumentFields({ pattern: 'DAG', extra: 1 }, parameters);

  assert.deepEqual(fields.map((field) => field.key), ['path', 'pattern', 'extra']);
  assert.equal(fields[0].fixed, true);
  assert.equal(fields[0].required, true);
  assert.equal(fields[0].description, 'File path to read.');
  assert.equal(fields[0].value, '');
  assert.equal(fields[1].value, 'DAG');
  assert.equal(fields[2].fixed, false);
  assert.equal(fields[2].valueType, 'number');
});

test('visibleCapabilitiesForPicker hides lower-level file duplicates when tool equivalents exist', () => {
  const capabilities = [
    { id: 'file.read', name: 'file_read', kind: 'file', enabled: true },
    { id: 'tool.read_file', name: 'read_file', kind: 'tool', enabled: true },
    { id: 'file.write', name: 'file_write', kind: 'file', enabled: true },
    { id: 'tool.write_file', name: 'write_file', kind: 'tool', enabled: true },
    { id: 'agent.conversation', name: 'conversation', kind: 'agent', enabled: true },
  ];

  assert.deepEqual(
    visibleCapabilitiesForPicker(capabilities).map((capability) => capability.id),
    ['tool.read_file', 'tool.write_file', 'agent.conversation'],
  );
});

test('resetSchemaArguments drops arguments from the previous capability schema', () => {
  const readFileParameters = {
    type: 'object',
    properties: {
      path: { type: 'string' },
    },
    required: ['path'],
  };
  const grepParameters = {
    type: 'object',
    properties: {
      path: { type: 'string' },
      pattern: { type: 'string' },
    },
    required: ['path', 'pattern'],
  };

  assert.deepEqual(
    resetSchemaArguments({ path: 'README.md', content: 'old content' }, grepParameters, readFileParameters),
    { path: 'README.md', pattern: '' },
  );
});

test('pruneEdgesToNodeIds removes edges that reference filtered nodes', () => {
  const edges = [
    { source: 'start', target: 'a', reason: 'internal start' },
    { source: 'a', target: 'b', reason: 'valid dependency' },
    { source: 'b', target: 'missing', reason: 'stale target' },
  ];

  assert.deepEqual(pruneEdgesToNodeIds(edges, new Set(['a', 'b'])), [
    { source: 'a', target: 'b', reason: 'valid dependency' },
  ]);
});

test('upsertArtifact stores artifacts under their id and normalizes paths', () => {
  assert.deepEqual(
    upsertArtifact(
      {},
      {
        id: 'report',
        paths: [' outputs/report.md ', '', 'outputs/assets/'],
        description: 'Generated report',
        required: true,
      },
    ),
    {
      report: {
        id: 'report',
        paths: ['outputs/report.md', 'outputs/assets/'],
        description: 'Generated report',
        required: true,
        metadata: {},
      },
    },
  );
});

test('removeArtifactBinding deletes artifacts and node input/output references', () => {
  const spec = {
    id: 'example',
    name: 'Example',
    artifacts: {
      source: { id: 'source', paths: ['uploads/source.md'] },
      report: { id: 'report', paths: ['outputs/report.md'] },
    },
    nodes: [
      { id: 'read', payload: { type: 'capability', invocation: { capability_id: 'tool.echo', kind: 'tool', arguments: {} } }, inputs: ['source'], outputs: ['report'] },
      { id: 'review', payload: { type: 'capability', invocation: { capability_id: 'tool.echo', kind: 'tool', arguments: {} } }, inputs: ['report'] },
    ],
    edges: [],
  };

  assert.deepEqual(removeArtifactBinding(spec, 'report'), {
    ...spec,
    artifacts: {
      source: { id: 'source', paths: ['uploads/source.md'] },
    },
    nodes: [
      { ...spec.nodes[0], inputs: ['source'], outputs: [] },
      { ...spec.nodes[1], inputs: [] },
    ],
  });
});

test('artifactPlaceholder builds executor artifact placeholder syntax', () => {
  assert.equal(artifactPlaceholder('report'), '{{artifact.report.path}}');
});

test('createUploadedFileArtifacts creates hidden file artifacts with workspace paths', () => {
  const result = createUploadedFileArtifacts(
    [
      { name: 'notes.md', relativePath: '' },
      { name: 'data.csv', relativePath: 'dataset/data.csv' },
    ],
    {
      artifacts: {
        upload_inputs_upload_notes_md: {
          id: 'upload_inputs_upload_notes_md',
          paths: ['inputs/upload/old-notes.md'],
        },
      },
      uploadRoot: 'inputs/upload',
    },
  );

  assert.deepEqual(result.uploads.map((item) => ({
    artifactId: item.artifact.id,
    sourceName: item.source.name,
    sourceRelativePath: item.source.relativePath,
    path: item.artifact.paths[0],
    hidden: item.artifact.metadata.hidden,
    source: item.artifact.metadata.source,
    kind: item.artifact.metadata.kind,
  })), [
    {
      artifactId: 'upload_inputs_upload_notes_md_2',
      sourceName: 'notes.md',
      sourceRelativePath: '',
      path: 'inputs/upload/notes.md',
      hidden: true,
      source: 'upload',
      kind: 'file',
    },
    {
      artifactId: 'upload_inputs_upload_dataset_data_csv',
      sourceName: 'data.csv',
      sourceRelativePath: 'dataset/data.csv',
      path: 'inputs/upload/dataset/data.csv',
      hidden: true,
      source: 'upload',
      kind: 'file',
    },
  ]);
  assert.equal(isUploadedFileArtifact(result.uploads[0].artifact), true);
  assert.equal(isUploadedFileArtifact({ id: 'manual', paths: ['outputs/report.md'] }), false);
});

test('uploadFormFilename can upload a generated artifact with only the file basename', () => {
  const file = { name: 'data.csv', webkitRelativePath: 'dataset/data.csv' };

  assert.equal(uploadFormFilename(file, { preserveRelativePath: false }), 'data.csv');
  assert.equal(uploadFormFilename(file, { preserveRelativePath: true }), 'dataset/data.csv');
});
