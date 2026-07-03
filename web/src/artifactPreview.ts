import type { RunArtifactPreviewKind } from './types';

export type ArtifactPreviewMode = 'text' | 'browser' | 'unsupported';
export type BrowserArtifactPreviewKind = 'pdf' | 'docx' | 'xlsx' | 'pptx';

export interface ArtifactPreviewRouteItem {
  previewKind?: RunArtifactPreviewKind | null;
  previewable?: boolean | null;
  previewUrl?: string | null;
  downloadUrl?: string | null;
  onlyOfficeConfigUrl?: string | null;
}

export interface BrowserArtifactPreviewRequest {
  kind: BrowserArtifactPreviewKind;
  source?: Blob | ArrayBuffer;
  onlyOfficeConfigUrl?: string | null;
  fileName?: string;
  maxPdfPages?: number;
  maxSheetRows?: number;
  maxSheetColumns?: number;
  onSaved?: () => void;
  signal?: AbortSignal;
}

export interface ArtifactPreviewRenderHandle {
  destroy: () => void;
}

interface OnlyOfficePreviewConfigResponse {
  document_server_url: string;
  script_url: string;
  config: Record<string, unknown>;
}

interface OnlyOfficeEditor {
  destroyEditor?: () => void;
}

declare global {
  interface Window {
    DocsAPI?: {
      DocEditor: new (containerId: string, config: Record<string, unknown>) => OnlyOfficeEditor;
    };
  }
}

const TEXT_PREVIEW_KINDS = new Set<RunArtifactPreviewKind>(['markdown', 'code', 'text']);
const BROWSER_PREVIEW_KINDS = new Set<RunArtifactPreviewKind>(['pdf', 'docx', 'xlsx', 'pptx']);
const onlyOfficeScriptPromises = new Map<string, Promise<void>>();
const onlyOfficeLoadedScriptUrls = new Set<string>();

export function artifactPreviewMode(kind: RunArtifactPreviewKind | null | undefined): ArtifactPreviewMode {
  if (!kind) return 'unsupported';
  if (TEXT_PREVIEW_KINDS.has(kind)) return 'text';
  if (BROWSER_PREVIEW_KINDS.has(kind)) return 'browser';
  return 'unsupported';
}

export function isBrowserArtifactPreviewKind(
  kind: RunArtifactPreviewKind | null | undefined,
): kind is BrowserArtifactPreviewKind {
  return kind === 'pdf' || kind === 'docx' || kind === 'xlsx' || kind === 'pptx';
}

export function shouldFetchTextArtifactPreview(item: ArtifactPreviewRouteItem | null | undefined): boolean {
  return Boolean(item?.previewable && item.previewUrl && artifactPreviewMode(item.previewKind) === 'text');
}

export function artifactPreviewDownloadUrl(item: ArtifactPreviewRouteItem | null | undefined): string | null {
  return item?.downloadUrl ?? null;
}

export async function renderBrowserArtifactPreview(
  container: HTMLElement,
  request: BrowserArtifactPreviewRequest,
): Promise<ArtifactPreviewRenderHandle> {
  if (request.onlyOfficeConfigUrl) return renderOnlyOfficePreview(container, request);
  if (request.kind === 'pdf') return renderPdfPreview(container, request);
  if (request.kind === 'docx') return renderDocxPreview(container, request);
  if (request.kind === 'xlsx') return renderXlsxPreview(container, request);
  return renderPptxPreview(container, request);
}

async function renderOnlyOfficePreview(
  container: HTMLElement,
  request: BrowserArtifactPreviewRequest,
): Promise<ArtifactPreviewRenderHandle> {
  if (!request.onlyOfficeConfigUrl) throw new Error('此文件缺少 OnlyOffice 预览配置。');
  const response = await fetch(request.onlyOfficeConfigUrl, { signal: request.signal });
  if (!response.ok) throw new Error(await previewResponseError(response));
  const payload = await response.json() as OnlyOfficePreviewConfigResponse;
  if (!payload.script_url || typeof payload.script_url !== 'string') {
    throw new Error('OnlyOffice 预览配置缺少脚本地址。');
  }
  if (!payload.config || typeof payload.config !== 'object') {
    throw new Error('OnlyOffice 预览配置无效。');
  }
  const existingEvents = onlyOfficeEvents(payload.config);
  payload.config = {
    ...payload.config,
    events: {
      ...existingEvents,
      onRequestClose: (event: unknown) => {
        onlyOfficeEvent(existingEvents, 'onRequestClose')?.(event);
        request.onSaved?.();
      },
    },
  };
  await loadOnlyOfficeScript(payload.script_url, request.signal);
  throwIfAborted(request.signal);
  if (!window.DocsAPI?.DocEditor) throw new Error('OnlyOffice Document Server 未加载 DocsAPI。');

  container.replaceChildren();
  const root = document.createElement('div');
  root.className = 'artifact-browser-preview artifact-onlyoffice-preview';
  const editorHost = document.createElement('div');
  const editorId = `onlyoffice-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  editorHost.id = editorId;
  root.append(editorHost);
  container.append(root);
  let editor: OnlyOfficeEditor;
  try {
    editor = new window.DocsAPI.DocEditor(editorId, payload.config);
  } catch (exc) {
    root.remove();
    throw exc;
  }
  return {
    destroy: () => {
      editor.destroyEditor?.();
      root.remove();
    },
  };
}

function onlyOfficeEvents(config: Record<string, unknown>): Record<string, (event: unknown) => void> {
  const events = config.events;
  return events && typeof events === 'object' ? events as Record<string, (event: unknown) => void> : {};
}

function onlyOfficeEvent(events: Record<string, (event: unknown) => void>, name: string): ((event: unknown) => void) | undefined {
  const event = events[name];
  return typeof event === 'function' ? event : undefined;
}

function loadOnlyOfficeScript(scriptUrl: string, signal: AbortSignal | undefined): Promise<void> {
  if (window.DocsAPI?.DocEditor && onlyOfficeLoadedScriptUrls.has(scriptUrl)) return Promise.resolve();
  const existing = onlyOfficeScriptPromises.get(scriptUrl);
  if (existing) return existing;
  const promise = new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Preview rendering was aborted.', 'AbortError'));
      return;
    }
    const script = document.createElement('script');
    const cleanup = () => {
      signal?.removeEventListener('abort', onAbort);
      script.onload = null;
      script.onerror = null;
    };
    const onAbort = () => {
      cleanup();
      reject(new DOMException('Preview rendering was aborted.', 'AbortError'));
    };
    script.src = scriptUrl;
    script.async = true;
    script.onload = () => {
      cleanup();
      onlyOfficeLoadedScriptUrls.add(scriptUrl);
      resolve();
    };
    script.onerror = () => {
      cleanup();
      script.remove();
      reject(new Error('OnlyOffice Document Server 脚本加载失败。'));
    };
    signal?.addEventListener('abort', onAbort, { once: true });
    document.head.append(script);
  });
  onlyOfficeScriptPromises.set(scriptUrl, promise);
  promise.catch(() => onlyOfficeScriptPromises.delete(scriptUrl));
  return promise;
}

async function renderPdfPreview(
  container: HTMLElement,
  request: BrowserArtifactPreviewRequest,
): Promise<ArtifactPreviewRenderHandle> {
  const pdfjs = await import('pdfjs-dist') as any;
  const worker = await import('pdfjs-dist/build/pdf.worker.mjs?url');
  throwIfAborted(request.signal);
  pdfjs.GlobalWorkerOptions.workerSrc = worker.default;

  const data = new Uint8Array(await sourceArrayBuffer(request.source));
  throwIfAborted(request.signal);
  const task = pdfjs.getDocument({ data });
  request.signal?.addEventListener('abort', () => task.destroy?.(), { once: true });
  const pdf = await task.promise;
  throwIfAborted(request.signal);
  const maxPages = request.maxPdfPages ?? 30;
  const pageCount = Math.min(pdf.numPages, maxPages);
  let destroyed = false;
  let root: HTMLDivElement | null = null;

  try {
    throwIfAborted(request.signal);
    container.replaceChildren();
    root = document.createElement('div');
    root.className = 'artifact-browser-preview artifact-pdf-preview';
    container.append(root);

    for (let pageNumber = 1; pageNumber <= pageCount; pageNumber += 1) {
      throwIfAborted(request.signal);
      if (destroyed) break;
      const page = await pdf.getPage(pageNumber);
      throwIfAborted(request.signal);
      const viewport = page.getViewport({ scale: 1.25 });
      const pageShell = document.createElement('section');
      pageShell.className = 'artifact-pdf-page';
      pageShell.setAttribute('aria-label', `PDF page ${pageNumber}`);
      const canvas = document.createElement('canvas');
      const context = canvas.getContext('2d');
      if (!context) throw new Error('浏览器无法创建 PDF 预览画布。');
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;
      pageShell.append(canvas);
      root.append(pageShell);
      const renderTask = page.render({ canvasContext: context, viewport });
      request.signal?.addEventListener('abort', () => renderTask.cancel?.(), { once: true });
      await renderTask.promise;
      throwIfAborted(request.signal);
    }

    if (pdf.numPages > pageCount) {
      root.append(previewNotice(`仅预览前 ${pageCount} 页，共 ${pdf.numPages} 页。`));
    }

    return {
      destroy: () => {
        destroyed = true;
        void pdf.destroy?.();
        root?.remove();
      },
    };
  } catch (exc) {
    if (request.signal?.aborted) {
      void pdf.destroy?.();
      root?.remove();
    }
    throw exc;
  }
}

async function renderDocxPreview(
  container: HTMLElement,
  request: BrowserArtifactPreviewRequest,
): Promise<ArtifactPreviewRenderHandle> {
  const docx = await import('docx-preview') as any;
  const data = await sourceArrayBuffer(request.source);
  throwIfAborted(request.signal);
  container.replaceChildren();
  const root = document.createElement('div');
  root.className = 'artifact-browser-preview artifact-docx-preview';
  container.append(root);
  try {
    throwIfAborted(request.signal);
    await docx.renderAsync(data, root, undefined, {
      breakPages: true,
      className: 'artifact-docx-document',
      ignoreFonts: false,
      ignoreHeight: true,
      ignoreLastRenderedPageBreak: true,
      ignoreWidth: false,
      inWrapper: true,
    });
    throwIfAborted(request.signal);
  } catch (exc) {
    if (request.signal?.aborted) root.remove();
    throw exc;
  }
  return {
    destroy: () => {
      root.remove();
    },
  };
}

async function renderXlsxPreview(
  container: HTMLElement,
  request: BrowserArtifactPreviewRequest,
): Promise<ArtifactPreviewRenderHandle> {
  const { default: readXlsxFile } = await import('read-excel-file/browser');
  const sheets = await readXlsxFile(previewSource(request.source));
  throwIfAborted(request.signal);
  container.replaceChildren();
  const root = document.createElement('div');
  root.className = 'artifact-browser-preview artifact-xlsx-preview';
  const tabList = document.createElement('div');
  tabList.className = 'artifact-xlsx-tabs';
  const tableHost = document.createElement('div');
  tableHost.className = 'artifact-xlsx-table-host';
  root.append(tabList, tableHost);
  container.append(root);

  try {
    if (!sheets.length) {
      tableHost.append(previewNotice('工作簿中没有可预览的工作表。'));
      return {
        destroy: () => {
          root.remove();
        },
      };
    }

    const renderSheet = (sheetIndex: number) => {
      const selectedSheet = sheets[sheetIndex];
      for (const button of tabList.querySelectorAll('button')) {
        button.classList.toggle('active', button.dataset.sheetIndex === String(sheetIndex));
      }
      tableHost.replaceChildren();
      tableHost.append(renderSheetTable(selectedSheet.data as unknown[][], request));
    };

    sheets.forEach((sheet, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.sheetIndex = String(index);
      button.textContent = sheet.sheet;
      button.addEventListener('click', () => renderSheet(index));
      tabList.append(button);
    });
    renderSheet(0);
    throwIfAborted(request.signal);

    return {
      destroy: () => {
        root.remove();
      },
    };
  } catch (exc) {
    if (request.signal?.aborted) root.remove();
    throw exc;
  }
}

function renderSheetTable(rows: unknown[][], request: BrowserArtifactPreviewRequest): HTMLElement {
  const maxRows = request.maxSheetRows ?? 200;
  const maxColumns = request.maxSheetColumns ?? 50;
  const visibleRows = rows.slice(0, maxRows);
  const widestColumnCount = rows.reduce((count, row) => Math.max(count, row.length), 0);
  const table = document.createElement('table');
  table.className = 'artifact-xlsx-table';
  const tbody = document.createElement('tbody');
  table.append(tbody);

  for (const row of visibleRows) {
    const tr = document.createElement('tr');
    for (const cell of row.slice(0, maxColumns)) {
      const td = document.createElement('td');
      td.textContent = formatSheetCell(cell);
      tr.append(td);
    }
    tbody.append(tr);
  }

  const wrapper = document.createElement('div');
  wrapper.className = 'artifact-xlsx-table-wrap';
  wrapper.append(table);
  if (rows.length > visibleRows.length) {
    wrapper.append(previewNotice(`仅预览前 ${visibleRows.length} 行，共 ${rows.length} 行。`));
  }
  if (widestColumnCount > maxColumns) {
    wrapper.append(previewNotice(`仅预览前 ${maxColumns} 列，共 ${widestColumnCount} 列。`));
  }
  return wrapper;
}

function formatSheetCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (value instanceof Date) return value.toLocaleString();
  return String(value);
}

async function renderPptxPreview(
  container: HTMLElement,
  request: BrowserArtifactPreviewRequest,
): Promise<ArtifactPreviewRenderHandle> {
  const [{ createElement }, { createRoot }, { PowerPointViewer }] = await Promise.all([
    import('react'),
    import('react-dom/client'),
    import('pptx-react-viewer'),
    import('pptx-react-viewer/styles.css'),
  ]);
  const data = new Uint8Array(await sourceArrayBuffer(request.source));
  throwIfAborted(request.signal);
  container.replaceChildren();
  const root = document.createElement('div');
  root.className = 'artifact-browser-preview artifact-pptx-preview';
  container.append(root);
  const reactRoot = createRoot(root);

  try {
    throwIfAborted(request.signal);
    reactRoot.render(createElement(PowerPointViewer, {
      canEdit: false,
      content: data,
      filePath: request.fileName,
    }));
    throwIfAborted(request.signal);
  } catch (exc) {
    reactRoot.unmount();
    root.remove();
    throw exc;
  }

  return {
    destroy: () => {
      reactRoot.unmount();
      root.remove();
    },
  };
}

function previewNotice(message: string): HTMLElement {
  const notice = document.createElement('div');
  notice.className = 'artifact-preview-note';
  notice.textContent = message;
  return notice;
}

async function sourceArrayBuffer(source: Blob | ArrayBuffer | undefined): Promise<ArrayBuffer> {
  const value = previewSource(source);
  if (value instanceof Blob) return value.arrayBuffer();
  return value;
}

function previewSource(source: Blob | ArrayBuffer | undefined): Blob | ArrayBuffer {
  if (!source) throw new Error('此文件缺少可用的预览内容。');
  return source;
}

async function previewResponseError(response: Response): Promise<string> {
  try {
    const payload = await response.clone().json();
    if (typeof payload.detail === 'string') return payload.detail;
    if (payload.detail) return JSON.stringify(payload.detail);
  } catch {
    // Fall through to plain-text response bodies.
  }
  const text = await response.text().catch(() => '');
  return text || `加载预览失败（HTTP ${response.status}）。`;
}

function throwIfAborted(signal: AbortSignal | undefined) {
  if (!signal?.aborted) return;
  throw new DOMException('Preview rendering was aborted.', 'AbortError');
}
