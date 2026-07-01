let fallbackCounter = 0;

export function createUiId(prefix = 'ui'): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }

  fallbackCounter += 1;
  const timestamp = Date.now().toString(36);
  const random = Math.random().toString(36).slice(2, 10);
  return `${prefix}_${timestamp}_${fallbackCounter.toString(36)}_${random}`;
}
