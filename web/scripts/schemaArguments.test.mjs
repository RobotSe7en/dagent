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
