import type { RunArtifactPreviewKind } from './types';

export type ArtifactPreviewMode = 'text' | 'browser' | 'unsupported';
export type BrowserArtifactPreviewKind = 'pdf' | 'docx' | 'xlsx';

export interface ArtifactPreviewRouteItem {
  previewKind?: RunArtifactPreviewKind | null;
  previewable?: boolean | null;
  previewUrl?: string | null;
  downloadUrl?: string | null;
}

export interface BrowserArtifactPreviewRequest {
  kind: BrowserArtifactPreviewKind;
  source: Blob | ArrayBuffer;
  fileName?: string;
  maxPdfPages?: number;
  maxSheetRows?: number;
  maxSheetColumns?: number;
}

export interface ArtifactPreviewRenderHandle {
  destroy: () => void;
}

const TEXT_PREVIEW_KINDS = new Set<RunArtifactPreviewKind>(['markdown', 'code', 'text']);
const BROWSER_PREVIEW_KINDS = new Set<RunArtifactPreviewKind>(['pdf', 'docx', 'xlsx']);

export function artifactPreviewMode(kind: RunArtifactPreviewKind | null | undefined): ArtifactPreviewMode {
  if (!kind) return 'unsupported';
  if (TEXT_PREVIEW_KINDS.has(kind)) return 'text';
  if (BROWSER_PREVIEW_KINDS.has(kind)) return 'browser';
  return 'unsupported';
}

export function isBrowserArtifactPreviewKind(
  kind: RunArtifactPreviewKind | null | undefined,
): kind is BrowserArtifactPreviewKind {
  return kind === 'pdf' || kind === 'docx' || kind === 'xlsx';
}

export function shouldFetchTextArtifactPreview(item: ArtifactPreviewRouteItem | null | undefined): boolean {
  return Boolean(item?.previewable && item.previewUrl && artifactPreviewMode(item.previewKind) === 'text');
}

export function artifactPreviewDownloadUrl(item: ArtifactPreviewRouteItem | null | undefined): string | null {
  if (!item?.previewable || artifactPreviewMode(item.previewKind) !== 'browser') return null;
  return item.downloadUrl ?? null;
}

export async function renderBrowserArtifactPreview(
  container: HTMLElement,
  request: BrowserArtifactPreviewRequest,
): Promise<ArtifactPreviewRenderHandle> {
  if (request.kind === 'pdf') return renderPdfPreview(container, request);
  if (request.kind === 'docx') return renderDocxPreview(container, request);
  return renderXlsxPreview(container, request);
}

async function renderPdfPreview(
  container: HTMLElement,
  request: BrowserArtifactPreviewRequest,
): Promise<ArtifactPreviewRenderHandle> {
  const pdfjs = await import('pdfjs-dist') as any;
  const worker = await import('pdfjs-dist/build/pdf.worker.mjs?url');
  pdfjs.GlobalWorkerOptions.workerSrc = worker.default;

  const data = new Uint8Array(await sourceArrayBuffer(request.source));
  const task = pdfjs.getDocument({ data });
  const pdf = await task.promise;
  const maxPages = request.maxPdfPages ?? 30;
  const pageCount = Math.min(pdf.numPages, maxPages);
  let destroyed = false;

  container.replaceChildren();
  const root = document.createElement('div');
  root.className = 'artifact-browser-preview artifact-pdf-preview';
  container.append(root);

  for (let pageNumber = 1; pageNumber <= pageCount; pageNumber += 1) {
    if (destroyed) break;
    const page = await pdf.getPage(pageNumber);
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
    await page.render({ canvasContext: context, viewport }).promise;
  }

  if (pdf.numPages > pageCount) {
    root.append(previewNotice(`仅预览前 ${pageCount} 页，共 ${pdf.numPages} 页。`));
  }

  return {
    destroy: () => {
      destroyed = true;
      void pdf.destroy?.();
      container.replaceChildren();
    },
  };
}

async function renderDocxPreview(
  container: HTMLElement,
  request: BrowserArtifactPreviewRequest,
): Promise<ArtifactPreviewRenderHandle> {
  const docx = await import('docx-preview') as any;
  const data = await sourceArrayBuffer(request.source);
  container.replaceChildren();
  const root = document.createElement('div');
  root.className = 'artifact-browser-preview artifact-docx-preview';
  container.append(root);
  await docx.renderAsync(data, root, undefined, {
    breakPages: true,
    className: 'artifact-docx-document',
    ignoreFonts: false,
    ignoreHeight: true,
    ignoreLastRenderedPageBreak: true,
    ignoreWidth: false,
    inWrapper: true,
  });
  return {
    destroy: () => {
      container.replaceChildren();
    },
  };
}

async function renderXlsxPreview(
  container: HTMLElement,
  request: BrowserArtifactPreviewRequest,
): Promise<ArtifactPreviewRenderHandle> {
  const { default: readXlsxFile } = await import('read-excel-file/browser');
  const sheets = await readXlsxFile(request.source);
  container.replaceChildren();
  const root = document.createElement('div');
  root.className = 'artifact-browser-preview artifact-xlsx-preview';
  const tabList = document.createElement('div');
  tabList.className = 'artifact-xlsx-tabs';
  tabList.setAttribute('role', 'tablist');
  const tableHost = document.createElement('div');
  tableHost.className = 'artifact-xlsx-table-host';
  root.append(tabList, tableHost);
  container.append(root);

  if (!sheets.length) {
    tableHost.append(previewNotice('工作簿中没有可预览的工作表。'));
    return {
      destroy: () => {
        container.replaceChildren();
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

  return {
    destroy: () => {
      container.replaceChildren();
    },
  };
}

function renderSheetTable(rows: unknown[][], request: BrowserArtifactPreviewRequest): HTMLElement {
  const maxRows = request.maxSheetRows ?? 200;
  const maxColumns = request.maxSheetColumns ?? 50;
  const visibleRows = rows.slice(0, maxRows);
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
  return wrapper;
}

function formatSheetCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (value instanceof Date) return value.toLocaleString();
  return String(value);
}

function previewNotice(message: string): HTMLElement {
  const notice = document.createElement('div');
  notice.className = 'artifact-preview-note';
  notice.textContent = message;
  return notice;
}

async function sourceArrayBuffer(source: Blob | ArrayBuffer): Promise<ArrayBuffer> {
  if (source instanceof Blob) return source.arrayBuffer();
  return source;
}
