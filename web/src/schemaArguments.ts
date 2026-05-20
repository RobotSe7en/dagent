export type ArgumentValueType = 'string' | 'number' | 'boolean' | 'json';

type JsonSchemaObject = Record<string, unknown>;

export interface SchemaArgumentField {
  key: string;
  value: unknown;
  valueType: ArgumentValueType;
  fixed: boolean;
  required: boolean;
  description: string;
}

export function visibleCapabilitiesForPicker<T extends { id: string; enabled?: boolean }>(
  capabilities: T[],
): T[] {
  const enabled = capabilities.filter((capability) => capability.enabled !== false);
  const ids = new Set(enabled.map((capability) => capability.id));
  return enabled.filter((capability) => {
    if (capability.id === 'file.read' && ids.has('tool.read_file')) return false;
    if (capability.id === 'file.write' && ids.has('tool.write_file')) return false;
    return true;
  });
}

export function ensureSchemaArguments(
  value: Record<string, unknown>,
  parameters?: Record<string, unknown>,
): Record<string, unknown> {
  const next = { ...value };
  for (const [key, schema] of Object.entries(schemaProperties(parameters))) {
    if (!Object.prototype.hasOwnProperty.call(next, key)) {
      next[key] = defaultValueForSchema(schema);
    }
  }
  return next;
}

export function resetSchemaArguments(
  value: Record<string, unknown>,
  nextParameters?: Record<string, unknown>,
  _previousParameters?: Record<string, unknown>,
): Record<string, unknown> {
  const nextProperties = schemaProperties(nextParameters);
  const hasSchema = Object.keys(nextProperties).length > 0;
  if (!hasSchema) return {};

  const next: Record<string, unknown> = {};
  for (const key of Object.keys(nextProperties)) {
    if (Object.prototype.hasOwnProperty.call(value, key)) {
      next[key] = value[key];
    }
  }
  return ensureSchemaArguments(next, nextParameters);
}

export function buildSchemaArgumentFields(
  value: Record<string, unknown>,
  parameters?: Record<string, unknown>,
): SchemaArgumentField[] {
  const properties = schemaProperties(parameters);
  const required = schemaRequired(parameters);
  const normalizedValue = ensureSchemaArguments(value, parameters);
  const fields: SchemaArgumentField[] = [];

  for (const [key, schema] of Object.entries(properties)) {
    fields.push({
      key,
      value: normalizedValue[key],
      valueType: valueTypeForSchema(schema, normalizedValue[key]),
      fixed: true,
      required: required.has(key),
      description: schemaDescription(schema),
    });
  }

  for (const [key, itemValue] of Object.entries(normalizedValue)) {
    if (Object.prototype.hasOwnProperty.call(properties, key)) continue;
    fields.push({
      key,
      value: itemValue,
      valueType: argumentValueType(itemValue),
      fixed: false,
      required: false,
      description: '',
    });
  }

  return fields;
}

export function argumentValueType(value: unknown): ArgumentValueType {
  if (typeof value === 'number') return 'number';
  if (typeof value === 'boolean') return 'boolean';
  if (typeof value === 'string') return 'string';
  return 'json';
}

export function formatArgumentValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value ?? null);
}

export function parseArgumentValue(rawValue: string, type: ArgumentValueType, previous: unknown): unknown {
  if (type === 'string') return rawValue;
  if (type === 'number') {
    if (rawValue.trim() === '') return 0;
    const parsed = Number(rawValue);
    return Number.isFinite(parsed) ? parsed : previous;
  }
  if (type === 'boolean') return rawValue === 'true';
  try {
    return JSON.parse(rawValue);
  } catch {
    return previous;
  }
}

export function coerceArgumentValue(value: unknown, type: ArgumentValueType): unknown {
  if (type === 'string') return typeof value === 'string' ? value : formatArgumentValue(value);
  if (type === 'number') return typeof value === 'number' ? value : Number(value) || 0;
  if (type === 'boolean') return typeof value === 'boolean' ? value : Boolean(value);
  if (typeof value === 'string') {
    try {
      return JSON.parse(value);
    } catch {
      return value;
    }
  }
  return value ?? null;
}

function schemaProperties(parameters?: Record<string, unknown>): Record<string, JsonSchemaObject> {
  const properties = parameters?.properties;
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) return {};

  const result: Record<string, JsonSchemaObject> = {};
  for (const [key, schema] of Object.entries(properties as Record<string, unknown>)) {
    if (schema && typeof schema === 'object' && !Array.isArray(schema)) {
      result[key] = schema as JsonSchemaObject;
    } else {
      result[key] = {};
    }
  }
  return result;
}

function schemaRequired(parameters?: Record<string, unknown>): Set<string> {
  const required = parameters?.required;
  if (!Array.isArray(required)) return new Set();
  return new Set(required.filter((item): item is string => typeof item === 'string'));
}

function defaultValueForSchema(schema: JsonSchemaObject): unknown {
  if (Object.prototype.hasOwnProperty.call(schema, 'default')) return schema.default;
  const type = schemaType(schema);
  if (type === 'number' || type === 'integer') return 0;
  if (type === 'boolean') return false;
  if (type === 'array') return [];
  if (type === 'object') return {};
  return '';
}

function valueTypeForSchema(schema: JsonSchemaObject, value: unknown): ArgumentValueType {
  const type = schemaType(schema);
  if (type === 'number' || type === 'integer') return 'number';
  if (type === 'boolean') return 'boolean';
  if (type === 'object' || type === 'array') return 'json';
  return argumentValueType(value);
}

function schemaType(schema: JsonSchemaObject): string {
  const rawType = schema.type;
  if (Array.isArray(rawType)) {
    const concreteType = rawType.find((item) => item !== 'null');
    return typeof concreteType === 'string' ? concreteType : '';
  }
  return typeof rawType === 'string' ? rawType : '';
}

function schemaDescription(schema: JsonSchemaObject): string {
  return typeof schema.description === 'string' ? schema.description : '';
}
