import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';
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

async function importTypeScriptModule(entryRelativePath, relativePaths) {
  const tempDir = await mkdtemp(path.join(tmpdir(), 'dagent-web-test-'));
  for (const relativePath of relativePaths) {
    const sourceUrl = new URL(relativePath, import.meta.url);
    const source = await readFile(sourceUrl, 'utf8');
    let output = ts.transpileModule(source, {
      compilerOptions: {
        module: ts.ModuleKind.ES2022,
        target: ts.ScriptTarget.ES2022,
      },
    }).outputText;
    output = output
      .replace(/from '(\.\/[^']+)'/g, "from '$1.js'")
      .replace(/import\.meta\.env\.VITE_API_BASE/g, 'undefined');
    const outputPath = path.join(tempDir, relativePath.replace(/^\.\.\//, '').replace(/\.ts$/, '.js'));
    await mkdir(path.dirname(outputPath), { recursive: true });
    await writeFile(outputPath, output, 'utf8');
  }
  const entryPath = path.join(tempDir, entryRelativePath.replace(/^\.\.\//, '').replace(/\.ts$/, '.js'));
  try {
    return await import(pathToFileURL(entryPath).href);
  } finally {
    await rm(tempDir, { recursive: true, force: true });
  }
}

const {
  buildSchemaArgumentFields,
  ensureSchemaArguments,
  resetSchemaArguments,
  visibleCapabilitiesForPicker,
} = await importTypeScript('../src/schemaArguments.ts');
const {
  capabilityDisplayName,
  buildMcpManagementTree,
  buildToolManagementTree,
  cleanWorkspaceKeyDraft,
  isValidCapabilityId,
  visibleToolManagementCapabilities,
} = await importTypeScript('../src/capabilityContracts.ts');
const {
  chatScopeRequestFields,
  pruneSelectedAgentIds,
} = await importTypeScript('../src/agentScope.ts');
const {
  canvasCenterNodePosition,
} = await importTypeScript('../src/canvasPositions.ts');
const {
  nextExpandedSkillNames,
  nextMcpResourceSelection,
  resolveSelectedMcpToolId,
} = await importTypeScript('../src/sidebarState.ts');
const { pruneEdgesToNodeIds } = await importTypeScript('../src/dagEdges.ts');
const {
  artifactPathExpr,
  createUploadedFileArtifacts,
  isUploadedFileArtifact,
  removeArtifactBinding,
  uploadFormFilename,
  updateArtifactBinding,
  upsertArtifact,
} = await importTypeScript('../src/dagArtifacts.ts');
const {
  bindingLabel,
  buildVariableCatalog,
  collectNodeOutputRefs,
  isValueBinding,
  makeArtifactBinding,
  makeGraphInputBinding,
  makeNodeOutputBinding,
  removeNodeOutputRefs,
  rewriteNodeOutputRefs,
  wouldCreateCycle,
} = await importTypeScript('../src/valueBindings.ts');
const {
  appendRunTranscriptCapability,
  appendRunTranscriptTraceEvent,
  appendRunTranscriptToken,
  buildRunDialogSummary,
  runTranscriptFromTraceEvents,
} = await importTypeScript('../src/orchestrationRun.ts');
const {
  appendCapabilityReviewDecisionTimeline,
  appendValidatingTimeline,
  appendValidationTimeline,
  appendReasoningTimeline,
  appendTextTimeline,
  collapsedProcessTimelineParts,
  closeReasoningTimeline,
  isProcessTimelineItem,
  processTimelineSummary,
  shouldCollapseProcessTimeline,
  upsertDagMessageTimeline,
} = await importTypeScript('../src/chatTimeline.ts');
const {
  responseDeltaPayload,
  runStartedPayload,
} = await importTypeScript('../src/streamProtocol.ts');
const {
  shouldApplyPythonToolDiscoveryResult,
  pythonToolDiscoverySourceKey,
} = await importTypeScript('../src/pythonToolDiscovery.ts');
const {
  artifactPreviewDownloadUrl,
  artifactPreviewMode,
  shouldFetchTextArtifactPreview,
} = await importTypeScript('../src/artifactPreview.ts');

function runEvent(eventId, type, data) {
  return runEventInStream(eventId, 'stream_1', eventId, type, data);
}

function runEventInStream(eventId, streamId, streamSeq, type, data) {
  const payloadData = type.startsWith('response.')
    ? { response_id: 'response_1', model_step: null, ...data }
    : data;
  return {
    run_id: 'run_1',
    event_id: eventId,
    stream_id: streamId,
    stream_seq: streamSeq,
    event_type: type,
    payload: {
      type,
      data: payloadData,
      sequence: streamSeq,
      run_id: 'run_1',
    },
    created_at: eventId,
  };
}

test('ui id generation falls back when crypto.randomUUID is unavailable', async () => {
  const { createUiId } = await importTypeScript('../src/uiIds.ts');
  const originalCrypto = globalThis.crypto;
  Object.defineProperty(globalThis, 'crypto', {
    configurable: true,
    value: {},
  });
  try {
    const first = createUiId('trace');
    const second = createUiId('trace');

    assert.match(first, /^trace_/);
    assert.match(second, /^trace_/);
    assert.notEqual(first, second);
  } finally {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: originalCrypto,
    });
  }
});

test('chat workbench CSS removes legacy centered workspace layout', async () => {
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const workspaceRuleCount = (css.match(/^\.workspace\s*\{/gm) ?? []).length;

  assert.equal(workspaceRuleCount, 1);
  assert.equal(css.includes('place-items: stretch center'), false);
  assert.match(css, /\.workspace-sidebar\[data-collapsed="true"\]\s+\.sidebar-nav button\s*\{[^}]*width:\s*41px;[^}]*height:\s*41px;/s);
});

test('chat workbench ports the design shell without mock run data', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');

  assert.match(appSource, /const workspaceItems[\s\S]*\{ key: 'chat', label: '智能工作台'/);
  assert.match(appSource, /\{ key: 'orchestration', label: '智能体编排'/);
  assert.match(appSource, /\{ key: 'tools', label: '能力管理', icon: <Blocks size=\{16\} \/> \}/);
  assert.match(appSource, /\{ key: 'agents', label: '智能体管理', icon: <BotMessageSquare size=\{16\} \/> \}/);
  assert.match(appSource, /\{ key: 'system', label: '系统管理'/);
  assert.match(appSource, /streamTask\(prompt, target, reviewLevel/);
  assert.match(appSource, /buildWorkbenchArtifacts\(\{[\s\S]*runFiles: runArtifactFiles/);
  assert.match(appSource, /function DesignEmptyConversation/);
  assert.match(appSource, /function DesignWorkspacePlaceholder/);
  assert.match(appSource, /className="brand-logo-expand"/);
  assert.match(appSource, /className="user-avatar"/);
  assert.match(appSource, /className="reasoning-summary"/);
  assert.match(appSource, /className="timeline-chevron"/);
  assert.match(appSource, /upsertDagMessageTimeline/);
  assert.match(appSource, /appendValidatingTimeline/);
  assert.match(appSource, /appendValidationTimeline/);
  assert.match(appSource, /function ValidationCard/);
  assert.match(appSource, /function CapabilityEventCard/);
  assert.match(appSource, /function CapabilityCodeBlock/);
  assert.match(appSource, /className="capability-code-block"/);
  assert.doesNotMatch(appSource, /function ValidatingIndicator|function ValidationFeedbackCard/);
  assert.match(appSource, /'打开审查' : '查看流程'/);
  assert.match(appSource, /const \[artifactPanelOpen, setArtifactPanelOpen\] = useState\(false\);/);
  assert.match(appSource, /const artifactDrawerOpen = artifactPanelOpen;/);
  assert.match(appSource, /const chatWorkspaceProps: Parameters<typeof ChatWorkspace>\[0\] = \{[\s\S]*artifactPanelOpen: artifactDrawerOpen/);
  assert.match(appSource, /onToggleArtifacts: \(\) => setArtifactPanelOpen\(\(value\) => !value\)/);
  assert.match(appSource, /function UploadPicker/);
  assert.match(appSource, /<UploadPicker[\s\S]*variant="composer"[\s\S]*onUploadFiles=\{onUploadFiles\}/);
  assert.match(appSource, /<UploadPicker[\s\S]*variant="sidebar"[\s\S]*onUploadFiles=\{onUploadFiles\}/);
  const sidebarBrandSource = appSource.match(/<div className="sidebar-brand-row">[\s\S]*?\n      <\/div>\n\n      <div className="sidebar-label">/)?.[0] ?? '';
  assert.ok(sidebarBrandSource, 'sidebar brand row should exist');
  assert.doesNotMatch(sidebarBrandSource, /sidebar-collapse-button/);
  assert.doesNotMatch(appSource, /sidebar-collapse-row/);
  assert.match(appSource, /<div className="sidebar-user">[\s\S]*<button[\s\S]*className="sidebar-user-avatar"[\s\S]*onClick=\{collapsed \? onToggleCollapsed : undefined\}[\s\S]*title=\{collapsed \? '展开侧栏' : 'RobotSe7en'\}[\s\S]*<span className="sidebar-user-initials">RX<\/span>[\s\S]*<ChevronRight className="sidebar-user-expand" size=\{15\} \/>[\s\S]*<strong>RobotSe7en<\/strong>[\s\S]*<button className="sidebar-collapse-button" onClick=\{onToggleCollapsed\} title="收起 \/ 展开" type="button">[\s\S]*<ChevronLeft size=\{15\} \/>/);
  assert.doesNotMatch(appSource, /workspaceRootLabel|workspace-root-chip/);
  assert.match(appSource, /pendingChatUploads/);
  assert.match(appSource, /onUploadFiles: queueChatUploads/);
  assert.doesNotMatch(appSource, /const canOpen = artifacts\.length > 0|disabled=\{!canOpen\}|暂无产物/);
  assert.doesNotMatch(appSource, /composer-hint|⌘ \+ Enter 发送|当前模式/);
  assert.doesNotMatch(appSource, /designPreviewDag|designPreviewArtifacts|DesignPreviewConversation|designHistoryRows/);
  assert.doesNotMatch(appSource, /task_ui_demo|grep_results\.txt|orchestration_map\.json|14 matches across 6 files/);
  assert.doesNotMatch(appSource, /open review|view flow/);
  assert.match(appSource, /activeWorkspace === 'orchestration' && orchestrationMode === 'dynamic' \? \([\s\S]*<DynamicOrchestrationWorkspace/);
  assert.match(appSource, /activeWorkspace === 'orchestration' && orchestrationMode === 'static' \? \([\s\S]*<OrchestrationWorkspace/);
  assert.match(appSource, /activeWorkspace === 'tools' \? \([\s\S]*<CapabilityDirectory/);
  assert.doesNotMatch(appSource, /task_ui_demo|grep_results\.txt|orchestration_map\.json|14 matches across 6 files/);

  assert.match(css, /\.design-empty-conversation/);
  assert.match(css, /\.app-shell\s*\{[^}]*grid-template-columns:\s*230px minmax\(0, 1fr\);/s);
  assert.match(css, /\.app-shell\.nav-collapsed\s*\{[^}]*grid-template-columns:\s*60px minmax\(0, 1fr\);/s);
  assert.match(css, /\.brand-logo-expand/);
  assert.match(css, /\.workspace-sidebar\[data-collapsed="true"\] \.brand-mark:hover \.brand-logo-expand/);
  assert.doesNotMatch(css, /\.sidebar-collapse-row\b/);
  assert.doesNotMatch(css, /workspace-root-chip/);
  assert.match(css, /\.sidebar-user \.sidebar-collapse-button\s*\{[^}]*margin-left:\s*auto;/s);
  assert.match(css, /\.sidebar-user-avatar\s*\{[^}]*display:\s*grid;[^}]*place-items:\s*center;/s);
  assert.match(css, /\.sidebar-user-initials,\s*\.sidebar-user-expand\s*\{[^}]*grid-area:\s*1 \/ 1;/s);
  assert.match(css, /\.sidebar-user-expand\s*\{[^}]*display:\s*none;/s);
  assert.match(css, /\.workspace-sidebar\[data-collapsed="true"\] \.sidebar-user-avatar\s*\{[^}]*cursor:\s*pointer;/s);
  assert.match(css, /\.workspace-sidebar\[data-collapsed="true"\] \.sidebar-user-avatar:hover \.sidebar-user-initials\s*\{[^}]*display:\s*none;/s);
  assert.match(css, /\.workspace-sidebar\[data-collapsed="true"\] \.sidebar-user-avatar:hover \.sidebar-user-expand\s*\{[^}]*display:\s*block;/s);
  assert.match(css, /\.workspace-sidebar\[data-collapsed="true"\] \.sidebar-user \.sidebar-collapse-button\s*\{[^}]*display:\s*none;/s);
  assert.match(css, /\.workspace-sidebar\[data-collapsed="true"\] \.sidebar-user > span/s);
  assert.doesNotMatch(css, /\.workspace-sidebar\[data-collapsed="true"\] \.sidebar-user span\s*\{[^}]*display:\s*none;/s);
  assert.match(css, /\.user-avatar/);
  assert.match(css, /\.user-row\s*\{[^}]*--user-avatar-offset:\s*var\(--chat-user-avatar-safe-space,\s*43px\);[^}]*justify-content:\s*flex-end;/s);
  assert.match(css, /\.user-avatar\s*\{[^}]*margin-right:\s*calc\(var\(--user-avatar-offset\) \* -1\);/s);
  assert.match(css, /@media \(max-width: 760px\) \{[\s\S]*\.user-row\s*\{[^}]*--user-avatar-offset:\s*0px;/s);
  assert.match(css, /\.design-workspace-placeholder/);
  assert.match(css, /\.capability-event-card\[open\]\s*\{[^}]*max-height:\s*min\(760px,\s*78vh\);[^}]*overflow-y:\s*auto;/s);
  assert.match(css, /\.capability-code-block/);
  assert.match(css, /\.chat-workspace\s*\{[^}]*--chat-content-max-width:\s*1040px;/s);
  assert.match(css, /\.chat-workspace\.without-artifacts\s*\{[^}]*--chat-content-max-width:\s*1280px;/s);
  assert.match(css, /\.chat-workspace\.with-artifacts\s*\{[^}]*--chat-user-avatar-safe-space:\s*43px;/s);
  assert.match(css, /\.chat-main\s*\{[^}]*height:\s*100%;[^}]*display:\s*grid;[^}]*grid-template-rows:\s*minmax\(0,\s*1fr\) auto;/s);
  assert.match(css, /\.conversation-frame\s*\{[^}]*box-sizing:\s*border-box;[^}]*width:\s*min\(var\(--chat-content-max-width\), calc\(100% - 56px\)\);[^}]*padding:\s*28px var\(--chat-user-avatar-safe-space,\s*0px\) 36px 0;/s);
  assert.match(css, /\.composer-card\s*\{[^}]*width:\s*min\(var\(--chat-content-max-width\), calc\(100% - 56px\)\);/s);
  assert.match(css, /\.composer-card textarea\s*\{[^}]*resize:\s*none;/s);
  assert.match(css, /\.sidebar-history-head button svg\s*\{[^}]*display:\s*block;/s);
  assert.match(css, /\.validation-card\s*\{[^}]*background:\s*#fff;/s);
  assert.match(css, /\.validation-card \.timeline-section p\s*\{[^}]*font-size:\s*13px;[^}]*line-height:\s*1\.65;/s);
  assert.match(css, /button:focus:not\(:focus-visible\)\s*\{[^}]*outline:\s*none;/s);
  assert.match(css, /button:focus-visible\s*\{[^}]*outline:\s*2px solid rgba\(91, 91, 214, 0\.42\);/s);
});

test('chat message frames keep user and assistant treatment aligned', async () => {
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');

  assert.match(css, /\.user-bubble\s*\{[^}]*border:\s*1px solid rgba\(103, 103, 220, 0\.22\);[^}]*border-radius:\s*14px 14px 6px 14px;[^}]*background:\s*#6767dc;[^}]*color:\s*#fff;[^}]*box-shadow:\s*none;/s);
  assert.match(css, /\.assistant-row\s*\{[^}]*align-items:\s*flex-start;/s);
  assert.match(css, /\.assistant-turn-frame\s*\{[^}]*flex:\s*0 1 960px;[^}]*max-width:\s*min\(100%, 960px\);[^}]*border-radius:\s*14px 14px 14px 6px;[^}]*box-shadow:\s*none;/s);
  assert.match(css, /\.assistant-turn-frame :is\(\.process-summary-body, \.message-timeline\.live-process-timeline, \.message-timeline\.completed-process-timeline\)\s*\{[^}]*gap:\s*4px;[^}]*padding:\s*4px 6px 6px;/s);
  assert.doesNotMatch(css, /\.assistant-turn-frame\s*\{[^}]*flex:\s*1;/s);
});

test('composer uses the reserved upload button for pending attachments', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const chatWorkspaceSource = appSource.match(/function ChatWorkspace[\s\S]*?\nfunction DesignEmptyConversation/)?.[0] ?? '';

  assert.ok(chatWorkspaceSource, 'ChatWorkspace function should exist');
  assert.doesNotMatch(chatWorkspaceSource, /onNewChat/);
  assert.doesNotMatch(chatWorkspaceSource, /title="新建会话"/);
  assert.doesNotMatch(chatWorkspaceSource, /暂未接入/);
  assert.match(chatWorkspaceSource, /<UploadPicker/);
  assert.match(chatWorkspaceSource, /onUploadFiles/);
  assert.match(chatWorkspaceSource, /pendingUploads/);
  assert.match(chatWorkspaceSource, /onRemoveUpload/);
});

test('composer summarizes large pending upload batches instead of listing every file', async () => {
  const {
    buildPendingUploadGroups,
    visiblePendingUploadGroups,
  } = await importTypeScript('../src/dagArtifacts.ts');
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const chatWorkspaceSource = appSource.match(/function ChatWorkspace[\s\S]*?\nfunction DesignEmptyConversation/)?.[0] ?? '';

  assert.equal(typeof buildPendingUploadGroups, 'function');
  assert.equal(typeof visiblePendingUploadGroups, 'function');

  const groups = buildPendingUploadGroups([
    { name: 'guide.md', webkitRelativePath: 'docs/guide.md', size: 100 },
    { name: 'api.md', webkitRelativePath: 'docs/reference/api.md', size: 300 },
    { name: 'readme.md', size: 50 },
    { name: 'notes.txt', size: 25 },
    { name: 'draft.txt', size: 20 },
    { name: 'extra.txt', size: 10 },
  ]);

  assert.deepEqual(groups.map((group) => ({
    key: group.key,
    label: group.label,
    fileCount: group.fileCount,
    size: group.size,
    indexes: group.indexes,
  })), [
    { key: 'folder:docs', label: 'docs', fileCount: 2, size: 400, indexes: [0, 1] },
    { key: 'file:readme.md:2', label: 'readme.md', fileCount: 1, size: 50, indexes: [2] },
    { key: 'file:notes.txt:3', label: 'notes.txt', fileCount: 1, size: 25, indexes: [3] },
    { key: 'file:draft.txt:4', label: 'draft.txt', fileCount: 1, size: 20, indexes: [4] },
    { key: 'file:extra.txt:5', label: 'extra.txt', fileCount: 1, size: 10, indexes: [5] },
  ]);
  assert.deepEqual(visiblePendingUploadGroups(groups, false, 3), {
    groups: groups.slice(0, 3),
    hiddenCount: 2,
  });

  assert.match(chatWorkspaceSource, /buildPendingUploadGroups\(pendingUploads\)/);
  assert.match(chatWorkspaceSource, /visiblePendingUploadGroups\(pendingUploadGroups, pendingUploadsExpanded/);
  assert.doesNotMatch(chatWorkspaceSource, /pendingUploads\.map\(\(file, index\)/);
  assert.match(css, /\.pending-upload-list\s*\{[^}]*max-height:\s*180px;[^}]*overflow-y:\s*auto;/s);
});

test('composer keeps shared compact controls in the run settings menu', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const chatWorkspaceSource = appSource.match(/function ChatWorkspace[\s\S]*?\nfunction DesignEmptyConversation/)?.[0] ?? '';

  assert.match(chatWorkspaceSource, /const targetOptions: Array<\{ value: ChatTarget; label: string; shortLabel: string; icon: React\.ReactNode \}> = \[/);
  assert.match(chatWorkspaceSource, /<div className="mode-switch" aria-label="运行模式">[\s\S]*title=\{option\.label\}[\s\S]*aria-label=\{option\.label\}[\s\S]*\{option\.icon\}[\s\S]*<span>\{option\.shortLabel\}<\/span>/);
  assert.doesNotMatch(chatWorkspaceSource, />\s*\{item\}\s*<\/button>/);
  assert.match(chatWorkspaceSource, /const reviewLevelLabels: Record<ReviewLevel, string> = \{ fast: '快速审核', careful: '谨慎审核' \};/);
  assert.doesNotMatch(chatWorkspaceSource, /<label className="review-control">/);
  assert.match(chatWorkspaceSource, /<div className="run-settings-row">[\s\S]*<strong>审核等级<\/strong>[\s\S]*<em>\{reviewLevelLabels\[reviewLevel\]\}<\/em>[\s\S]*className="run-settings-select"[\s\S]*aria-label="审核等级"/);
  assert.match(chatWorkspaceSource, /\{reviewLevelLabels\[level\]\}/);
  assert.doesNotMatch(chatWorkspaceSource, /<span>审核等级<\/span>/);
  assert.doesNotMatch(chatWorkspaceSource, /\{level\} review/);

  assert.match(chatWorkspaceSource, /<details className=\{`run-settings-menu \$\{validationEnabled \|\| validationError \|\| chatScopeLabel !== '全部能力' \? 'active' : ''\}`\}>/);
  assert.match(chatWorkspaceSource, /<summary[\s\S]*title=\{`运行设置 · \$\{validationEnabled \? '验证开启' : validationError \? '验证错误' : '验证关闭'\} · \$\{chatScopeLabel\}`\}[\s\S]*<SlidersHorizontal size=\{15\} \/>/);
  assert.match(chatWorkspaceSource, /<div className="run-settings-popover">[\s\S]*className=\{`validation-toggle \$\{validationEnabled \? 'active' : ''\} \$\{validationError \? 'error' : ''\}`\}[\s\S]*className="secondary-button compact-button scope-button"/);

  assert.doesNotMatch(css, /\.review-control\b/);
  assert.match(css, /\.run-settings-select\s*\{[^}]*min-height:\s*32px;/s);
  assert.match(css, /\.run-settings-menu\s*\{[^}]*position:\s*relative;/s);
  assert.match(css, /\.run-settings-popover\s*\{[^}]*position:\s*absolute;[^}]*left:\s*50%;[^}]*right:\s*auto;[^}]*bottom:\s*calc\(100% \+ 8px\);[^}]*width:\s*300px;[^}]*transform:\s*translateX\(-50%\);/s);
  assert.match(css, /\.composer-toolbar\s*\{[^}]*flex-wrap:\s*nowrap;/s);
  assert.match(css, /\.composer-toolbar \.mode-switch button\s*\{[^}]*min-width:\s*50px;[^}]*padding:\s*0 7px;/s);
  assert.match(css, /\.composer-toolbar \.mode-switch button span\s*\{[^}]*font-size:\s*12px;/s);
});

test('collapsed artifact rail still reserves safe space for user avatars', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const chatWorkspaceSource = appSource.match(/function ChatWorkspace[\s\S]*?\nfunction ProjectDetailWorkspace/)?.[0] ?? '';

  assert.ok(chatWorkspaceSource, 'ChatWorkspace function should exist');
  assert.match(chatWorkspaceSource, /<ArtifactPanel[\s\S]*open=\{artifactPanelOpen\}/);
  assert.match(chatWorkspaceSource, /className=\{`chat-workspace \$\{artifactPanelOpen \? 'with-artifacts' : 'without-artifacts'\}`\}/);
  assert.match(css, /\.artifact-rail\s*\{[^}]*width:\s*48px;[^}]*border-left:\s*1px solid #e7e8ec;/s);
  assert.match(css, /\.chat-workspace\.without-artifacts\s*\{[^}]*--chat-user-avatar-safe-space:\s*43px;/s);
  assert.match(css, /@media \(max-width: 760px\) \{[\s\S]*\.chat-workspace\.without-artifacts\s*\{[^}]*--chat-user-avatar-safe-space:\s*0px;/s);
});

test('upload picker keeps one visible button while supporting files and folders', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const pickerSource = appSource.match(/function UploadPicker[\s\S]*?\nfunction PanelResizeHandle/)?.[0] ?? '';

  assert.ok(pickerSource, 'UploadPicker function should exist');
  assert.match(pickerSource, /const detailsRef = useRef<HTMLDetailsElement \| null>\(null\);/);
  assert.match(pickerSource, /useEffect\(\(\) => \{[\s\S]*if \(disabled\) \{[\s\S]*detailsRef\.current\?\.removeAttribute\('open'\);[\s\S]*\}[\s\S]*\}, \[disabled\]\);/);
  assert.match(pickerSource, /<details[\s\S]*ref=\{detailsRef\}/);
  assert.match(pickerSource, /const onSummaryClick = \(event: React\.MouseEvent<HTMLElement>\) => \{[\s\S]*if \(disabled\) \{[\s\S]*event\.preventDefault\(\);/);
  assert.match(pickerSource, /<summary[\s\S]*<Upload size=\{iconSize\} \/>/);
  assert.match(pickerSource, /aria-disabled=\{disabled\}/);
  assert.match(pickerSource, /onClick=\{onSummaryClick\}/);
  assert.match(pickerSource, /上传文件/);
  assert.match(pickerSource, /上传文件夹/);
  assert.match(pickerSource, /type="file"[\s\S]*multiple/);
  assert.match(pickerSource, /webkitdirectory/);
  assert.match(css, /\.composer-card\s*\{[^}]*overflow:\s*visible;/s);
  assert.match(css, /\.composer-upload-picker \.upload-picker-menu\s*\{[^}]*top:\s*auto;[^}]*bottom:\s*calc\(100% \+ 6px\);/s);
});

test('streamTask sends smart workbench uploads as multipart payload files', async () => {
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');

  assert.match(apiSource, /uploads\?: File\[\]/);
  assert.match(apiSource, /new FormData\(\)/);
  assert.match(apiSource, /form\.append\('payload', JSON\.stringify\(body\)\)/);
  assert.match(apiSource, /form\.append\('files', file, uploadFormFilename\(file\)\)/);
});

test('capability helpers use display names and dotted ids', () => {
  const capability = {
    id: 'agent.helper',
    name: 'helper',
    display_name: 'Helper',
    kind: 'agent',
    description: 'Summarizes delegated work.',
    parameters: {},
    output_schema: {},
    policy: { risk: 'medium', requires_review: false, sandbox_required: true, network: false, secrets: [] },
    config: {},
    enabled: true,
  };

  assert.equal(capabilityDisplayName(capability), 'Helper');
  assert.equal(capabilityDisplayName({ id: 'tool.search', name: 'Search tool', display_name: '' }), 'Search tool');
  assert.equal(capabilityDisplayName({ id: 'tool.search', name: '', display_name: '  ' }), 'tool.search');
  assert.equal(isValidCapabilityId('tool.search'), true);
  assert.equal(isValidCapabilityId('mcp.remote_docs.lookup'), true);
  assert.equal(isValidCapabilityId('mcp.remote.docs.lookup'), true);
  assert.equal(isValidCapabilityId('agent.bad-name'), false);
  assert.equal(isValidCapabilityId(' search'), false);
  assert.equal(cleanWorkspaceKeyDraft('helper-agent'), 'helper_agent');
});

test('tool management capability list only includes tool capabilities', () => {
  const capabilities = [
    {
      id: 'tool.search',
      name: 'Search',
      kind: 'tool',
      description: '',
    },
    {
      id: 'mcp.docs.lookup',
      name: 'Lookup',
      kind: 'mcp',
      description: '',
    },
    {
      id: 'memory.read',
      name: 'Read memory',
      kind: 'memory',
      description: '',
    },
    {
      id: 'agent.helper',
      name: 'Helper',
      kind: 'agent',
      description: '',
    },
  ];

  assert.deepEqual(
    visibleToolManagementCapabilities(capabilities, ''),
    [capabilities[0]],
  );
  assert.deepEqual(
    visibleToolManagementCapabilities(capabilities, 'search'),
    [capabilities[0]],
  );
});

test('tool management tree groups built-in and custom tools by source', () => {
  const capabilities = [
    {
      id: 'tool.read_file',
      name: 'tool_read_file',
      display_name: 'Read file',
      kind: 'tool',
      description: 'Read a file.',
      config: { tool_name: 'read_file' },
    },
    {
      id: 'tool.search',
      name: 'tool_search',
      display_name: 'Search',
      kind: 'tool',
      description: 'Search docs.',
      config: {},
    },
    {
      id: 'tool.render',
      name: 'tool_render',
      display_name: 'Render',
      kind: 'tool',
      description: 'Render report.',
      config: {},
    },
    {
      id: 'tool.template',
      name: 'tool_template',
      display_name: 'Template',
      kind: 'tool',
      description: 'Manual template.',
      config: { template: 'hello' },
    },
    {
      id: 'mcp.docs.lookup',
      name: 'mcp_docs_lookup',
      display_name: 'Lookup',
      kind: 'mcp',
      description: 'MCP tool.',
      config: {},
    },
  ];
  const pythonTools = [
    {
      id: 'docs_tools',
      source: 'path',
      path: '/tmp/docs_tools.py',
      module: null,
      names: ['search'],
      enabled: true,
      status: 'loaded',
      capabilities: ['tool.search'],
    },
    {
      id: 'render_tools',
      source: 'managed',
      path: '/Users/olivia/.dagent/python-tools/render_tools.py',
      module: null,
      names: ['render'],
      enabled: false,
      status: 'disabled',
      capabilities: ['tool.render'],
    },
  ];

  const tree = buildToolManagementTree(capabilities, pythonTools, '');

  assert.deepEqual(tree.builtin.items.map((item) => item.capability.id), ['tool.read_file']);
  assert.deepEqual(tree.pythonSources.map((source) => source.source.id), ['docs_tools', 'render_tools']);
  assert.deepEqual(tree.pythonSources[0].items.map((item) => item.capability.id), ['tool.search']);
  assert.deepEqual(tree.pythonSources[1].items.map((item) => item.capability.id), ['tool.render']);
  assert.deepEqual(tree.manual.items.map((item) => item.capability.id), ['tool.template']);
  assert.equal(
    [...tree.builtin.items, ...tree.manual.items, ...tree.pythonSources.flatMap((source) => source.items)]
      .some((item) => item.capability.id === 'mcp.docs.lookup'),
    false,
  );
  assert.deepEqual(
    buildToolManagementTree(capabilities, pythonTools, 'render').pythonSources.map((source) => source.source.id),
    ['render_tools'],
  );
});

test('mcp management tree filters child tools without showing siblings', () => {
  const servers = [
    {
      name: 'docs',
      source: 'user',
      config: { command: 'uvx docs-server' },
      status: 'connected',
      tools: [
        {
          id: 'mcp.docs.lookup',
          name: 'lookup',
          display_name: 'Lookup',
          kind: 'mcp',
          description: 'Lookup docs.',
        },
        {
          id: 'mcp.docs.search',
          name: 'search',
          display_name: 'Search',
          kind: 'mcp',
          description: 'Search docs.',
        },
      ],
    },
    {
      name: 'files',
      source: 'user',
      config: { command: 'uvx files-server' },
      status: 'connected',
      tools: [
        {
          id: 'mcp.files.read',
          name: 'read',
          display_name: 'Read',
          kind: 'mcp',
          description: 'Read files.',
        },
      ],
    },
  ];

  const toolMatchedTree = buildMcpManagementTree(servers, 'lookup');

  assert.deepEqual(toolMatchedTree.map((group) => group.server.name), ['docs']);
  assert.deepEqual(toolMatchedTree[0].tools.map((tool) => tool.id), ['mcp.docs.lookup']);
  assert.deepEqual(
    buildMcpManagementTree(servers, 'docs')[0].tools.map((tool) => tool.id),
    ['mcp.docs.lookup', 'mcp.docs.search'],
  );
});

test('python tool discovery ignores stale results and later manual edits', () => {
  const request = {
    requestId: 3,
    sourceKey: pythonToolDiscoverySourceKey('path', '/tmp/tools.py'),
    namesEditedAtStart: 7,
  };

  assert.equal(
    shouldApplyPythonToolDiscoveryResult(
      { requestId: 3, sourceKey: 'path:/tmp/tools.py', namesEditedAt: 7 },
      request,
    ),
    true,
  );
  assert.equal(
    shouldApplyPythonToolDiscoveryResult(
      { requestId: 4, sourceKey: 'path:/tmp/tools.py', namesEditedAt: 7 },
      request,
    ),
    false,
  );
  assert.equal(
    shouldApplyPythonToolDiscoveryResult(
      { requestId: 3, sourceKey: 'path:/tmp/new-tools.py', namesEditedAt: 7 },
      request,
    ),
    false,
  );
  assert.equal(
    shouldApplyPythonToolDiscoveryResult(
      { requestId: 3, sourceKey: 'path:/tmp/tools.py', namesEditedAt: 8 },
      request,
    ),
    false,
  );
});

test('chat scope request fields keep agent delegation separate from capabilities', () => {
  assert.deepEqual(chatScopeRequestFields(undefined), {});
  assert.deepEqual(chatScopeRequestFields({
    capabilityIds: ['tool.echo'],
    skills: ['writing/brief'],
    agentScope: 'selected',
    agentIds: ['agent.helper'],
  }), {
    capability_ids: ['tool.echo'],
    skills: ['writing/brief'],
    agent_scope: 'selected',
    agent_ids: ['agent.helper'],
  });
  assert.deepEqual(chatScopeRequestFields({
    capabilityIds: null,
    skills: [],
    agentScope: 'registered',
    agentIds: [],
  }), {
    capability_ids: null,
    skills: [],
    agent_scope: 'registered',
  });
  assert.throws(
    () => chatScopeRequestFields({
      capabilityIds: ['agent.helper'],
      skills: [],
      agentScope: 'none',
      agentIds: [],
    }),
    /Agent capabilities must use agentScope/,
  );
  assert.deepEqual(
    pruneSelectedAgentIds(['agent.keep', 'agent.drop'], [{ id: 'agent.keep' }, { id: 'agent.other' }]),
    ['agent.keep'],
  );
});

test('tools workspace renders capability display names as primary labels', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.match(appSource, /<span>\{capabilityDisplayName\(capability\)\}<\/span>/);
  assert.doesNotMatch(appSource, /<span>\{capability\.id\}<\/span>/);
  assert.match(appSource, /<strong>\{selectedTool \? capabilityDisplayName\(selectedTool\) : '工具'\}<\/strong>/);
  assert.doesNotMatch(appSource, /<strong>\{selectedTool\?\.id \?\? '工具'\}<\/strong>/);
  assert.doesNotMatch(appSource, /<option key=\{capability\.id\} value=\{capability\.id\}>\s*\{capability\.id\}\s*<\/option>/);
});

test('python tool save and toggle do not report success for errored sources', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const saveSource = appSource.match(/const savePythonTool = async[\s\S]*?\n  const togglePythonToolSource/)?.[0] ?? '';
  const toggleSource = appSource.match(/const togglePythonToolSource = async[\s\S]*?\n  const removePythonToolSource/)?.[0] ?? '';

  assert.ok(saveSource, 'savePythonTool function should exist');
  assert.ok(toggleSource, 'togglePythonToolSource function should exist');
  assert.match(saveSource, /saved\.status === 'error'/);
  assert.match(toggleSource, /updated\.status === 'error'/);
  assert.doesNotMatch(saveSource, /setPythonToolMessage\(`Saved \$\{saved\.id\}\.`\);/);
  assert.doesNotMatch(toggleSource, /setMessage\(`\$\{enabled \? 'Enabled' : 'Disabled'\} \$\{source\.id\}\.`\);/);
});

test('canvas center node position uses the live canvas center without hard-coded fallback', () => {
  const canvasElement = {
    getBoundingClientRect: () => ({ left: 100, top: 50, width: 800, height: 400 }),
  };
  const flowInstance = {
    screenToFlowPosition: (point) => ({ x: point.x + 10, y: point.y + 20 }),
  };

  assert.deepEqual(canvasCenterNodePosition(flowInstance, canvasElement), { x: 414, y: 238 });
  assert.deepEqual(canvasCenterNodePosition(null, canvasElement), { x: 304, y: 168 });
  assert.deepEqual(canvasCenterNodePosition(flowInstance, null), { x: 0, y: 0 });
});

test('sidebar skill expansion opens previously expanded hidden skills when they are reselected', () => {
  const hiddenExpanded = nextExpandedSkillNames(new Set(['research', 'writer']), 'research', false);
  assert.deepEqual([...hiddenExpanded].sort(), ['research', 'writer']);

  const selectedClosed = nextExpandedSkillNames(new Set(), 'research', true);
  assert.deepEqual([...selectedClosed], ['research']);

  const selectedOpen = nextExpandedSkillNames(new Set(['research']), 'research', true);
  assert.deepEqual([...selectedOpen], []);
});

test('mcp sidebar selection distinguishes servers from child tools', () => {
  assert.deepEqual(
    nextMcpResourceSelection('docs', 'mcp.docs.lookup'),
    { name: 'docs', toolId: 'mcp.docs.lookup' },
  );
  assert.deepEqual(
    nextMcpResourceSelection('docs', null),
    { name: 'docs', toolId: '' },
  );
  assert.equal(resolveSelectedMcpToolId('mcp.docs.lookup', ['mcp.docs.lookup', 'mcp.docs.search']), 'mcp.docs.lookup');
  assert.equal(resolveSelectedMcpToolId('mcp.docs.lookup', ['mcp.docs.search']), '');
});

test('api helpers send agent preset and chat scope request bodies', async () => {
  const { createAgent, listRunEvents, streamTask, updateAgent } = await importTypeScriptModule('../src/api.ts', [
    '../src/agentScope.ts',
    '../src/api.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return {
      ok: true,
      body: new ReadableStream({
        start(controller) {
          controller.close();
        },
      }),
      json: async () => ({
        ...(String(url).includes('/runs/run_abc/events') ? { events: [{ event_id: 8, event_type: 'run.finished' }] } : {}),
        agent: { id: 'agent.helper', name: 'helper', profile: 'conversation', max_steps: 4 },
        agents: [],
        errors: {},
      }),
      text: async () => '',
    };
  };

  try {
    await createAgent({
      name: 'helper',
      profile: 'conversation',
      description: 'delegates work',
      max_steps: 4,
      capabilities: ['tool.echo'],
      skills: ['writing/brief'],
      agents: [],
      review: 'fast',
    });
    await updateAgent('helper', {
      name: 'helper',
      profile: 'conversation',
      description: '',
      max_steps: 5,
      capabilities: ['tool.search'],
      skills: [],
      agents: [],
      review: 'fast',
    });
    await streamTask('hello', 'auto', 'fast', {}, {
      capabilityIds: ['tool.echo'],
      skills: ['writing/brief'],
      agentScope: 'selected',
      agentIds: ['agent.helper'],
    });
    const events = await listRunEvents('run_abc', 7);
    assert.deepEqual(events, [{ event_id: 8, event_type: 'run.finished' }]);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls[0].url, '/api/agents');
  assert.equal(calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    name: 'helper',
    profile: 'conversation',
    description: 'delegates work',
    max_steps: 4,
    capabilities: ['tool.echo'],
    skills: ['writing/brief'],
    agents: [],
    review: 'fast',
  });
  assert.equal(calls[1].url, '/api/agents/helper');
  assert.equal(calls[1].init.method, 'PUT');
  assert.deepEqual(JSON.parse(calls[2].init.body), {
    messages: [{ role: 'user', content: 'hello' }],
    target: 'auto',
    review_level: 'fast',
    capability_ids: ['tool.echo'],
    skills: ['writing/brief'],
    agent_scope: 'selected',
    agent_ids: ['agent.helper'],
  });
  assert.equal(calls[3].url, '/api/runs/run_abc/events?after_event_id=7');
  assert.equal(calls[3].init.method, undefined);
});

test('saved DAG api helpers preserve metadata and layout', async () => {
  const { listSavedDags, saveSavedDag } = await importTypeScriptModule('../src/api.ts', [
    '../src/agentScope.ts',
    '../src/api.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const savedDag = {
    id: 'dag_saved',
    project_id: 'proj_1',
    org_id: 'default',
    owner_user_id: 'default',
    name: 'Saved DAG',
    description: 'persisted',
    spec: {
      id: 'spec_original',
      name: 'Spec Name',
      nodes: [],
      edges: [],
    },
    layout: { positions: { answer: { x: 120, y: 240 } } },
    revision: 3,
    created_at: 10,
    updated_at: 20,
  };
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return {
      ok: true,
      json: async () => (
        init.method === 'POST' || init.method === 'PATCH'
          ? { saved_dag: { ...savedDag, revision: init.method === 'PATCH' ? 4 : 1 } }
          : { saved_dags: [savedDag] }
      ),
      text: async () => '',
    };
  };

  try {
    const listed = await listSavedDags({ projectId: 'proj_1' });
    const created = await saveSavedDag({
      projectId: 'proj_1',
      spec: savedDag.spec,
      layout: savedDag.layout,
    });
    const updated = await saveSavedDag({
      savedDagId: 'dag_saved',
      spec: savedDag.spec,
      layout: savedDag.layout,
      expectedRevision: 3,
    });
    const renamed = await saveSavedDag({
      savedDagId: 'dag_saved',
      name: 'Display Name',
      description: 'Display description',
      spec: savedDag.spec,
      layout: savedDag.layout,
      expectedRevision: 4,
    });

    assert.equal(listed[0].id, 'dag_saved');
    assert.equal(listed[0].name, 'Saved DAG');
    assert.equal(listed[0].description, 'persisted');
    assert.equal(listed[0].spec.id, 'spec_original');
    assert.deepEqual(listed[0].layout, savedDag.layout);
    assert.equal(created.revision, 1);
    assert.equal(updated.revision, 4);
    assert.equal(renamed.revision, 4);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls[0].url, '/api/saved-dags?project_id=proj_1');
  assert.equal(calls[1].url, '/api/saved-dags');
  assert.deepEqual(JSON.parse(calls[1].init.body), {
    project_id: 'proj_1',
    name: 'Spec Name',
    description: '',
    spec: savedDag.spec,
    layout: savedDag.layout,
  });
  assert.equal(calls[2].url, '/api/saved-dags/dag_saved');
  assert.deepEqual(JSON.parse(calls[2].init.body), {
    spec: savedDag.spec,
    layout: savedDag.layout,
    expected_revision: 3,
  });
  assert.equal(calls[3].url, '/api/saved-dags/dag_saved');
  assert.deepEqual(JSON.parse(calls[3].init.body), {
    name: 'Display Name',
    description: 'Display description',
    spec: savedDag.spec,
    layout: savedDag.layout,
    expected_revision: 4,
  });
});

test('orchestration history api helpers use expected endpoints', async () => {
  const {
    deleteSavedDag,
    listConversationRuns,
    listConversations,
    listOrchestrationSessionRuns,
    listProjectConversationRuns,
    listProjectConversations,
    listSavedDagRuns,
    updateConversation,
    updateProjectConversation,
  } = await importTypeScriptModule('../src/api.ts', [
    '../src/agentScope.ts',
    '../src/api.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return {
      ok: true,
      json: async () => ({
        conversation: { id: 'conv_1', title: 'Renamed', kind: 'dynamic_dag' },
        conversations: [{ id: 'conv_1', title: 'Dynamic', kind: 'dynamic_dag' }],
        runs: [{ id: 'run_1', status: 'completed', has_state: true }],
      }),
      text: async () => '',
    };
  };

  try {
    await listConversations({ kind: 'dynamic_dag' });
    await listProjectConversations('proj 1', { kind: 'dynamic_dag' });
    await updateConversation('conv 1', { title: 'Renamed' });
    await updateProjectConversation('proj 1', 'conv 1', { title: 'Project Renamed' });
    await listConversationRuns('conv 1');
    await listProjectConversationRuns('proj 1', 'conv 1');
    await listOrchestrationSessionRuns('orch 1');
    await listSavedDagRuns('dag 1');
    await deleteSavedDag('dag 1');
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls[0].url, '/api/conversations?kind=dynamic_dag');
  assert.equal(calls[1].url, '/api/projects/proj%201/conversations?kind=dynamic_dag');
  assert.equal(calls[2].url, '/api/conversations/conv%201');
  assert.equal(calls[2].init.method, 'PATCH');
  assert.deepEqual(JSON.parse(calls[2].init.body), { title: 'Renamed' });
  assert.equal(calls[3].url, '/api/projects/proj%201/conversations/conv%201');
  assert.equal(calls[3].init.method, 'PATCH');
  assert.deepEqual(JSON.parse(calls[3].init.body), { title: 'Project Renamed' });
  assert.equal(calls[4].url, '/api/conversations/conv%201/runs');
  assert.equal(calls[5].url, '/api/projects/proj%201/conversations/conv%201/runs');
  assert.equal(calls[6].url, '/api/orchestration-sessions/orch%201/runs');
  assert.equal(calls[7].url, '/api/saved-dags/dag%201/runs');
  assert.equal(calls[8].url, '/api/saved-dags/dag%201');
  assert.equal(calls[8].init.method, 'DELETE');
});

test('value binding helpers create labels and rewrite node output references', () => {
  const binding = makeNodeOutputBinding('search', 'value', ['title']);

  assert.deepEqual(binding, {
    $expr: {
      type: 'node_output',
      node_id: 'search',
      field: 'value',
      path: ['title'],
    },
  });
  assert.equal(isValueBinding(binding), true);
  assert.equal(isValueBinding({ $expr: { type: 'missing' } }), false);
  assert.deepEqual(makeGraphInputBinding(['topic']), {
    $expr: {
      type: 'graph_input',
      path: ['topic'],
    },
  });
  assert.deepEqual(makeArtifactBinding('report', 'absolute_path'), {
    $expr: {
      type: 'artifact',
      artifact_id: 'report',
      field: 'absolute_path',
    },
  });
  assert.equal(bindingLabel(makeGraphInputBinding(['topic'])), 'DAG input.topic');
  assert.equal(bindingLabel(binding), 'search.output.title');
  assert.equal(bindingLabel(makeArtifactBinding('report')), 'artifact.report.path');
  assert.deepEqual(collectNodeOutputRefs({ result: binding, nested: [makeNodeOutputBinding('score', 'status')] }), [
    { nodeId: 'search', field: 'value', path: ['title'] },
    { nodeId: 'score', field: 'status', path: [] },
  ]);
  assert.deepEqual(rewriteNodeOutputRefs({ result: binding }, 'search', 'lookup'), {
    result: makeNodeOutputBinding('lookup', 'value', ['title']),
  });
  assert.deepEqual(removeNodeOutputRefs({ result: binding, keep: 'literal' }, 'search'), {
    keep: 'literal',
  });
  assert.equal(wouldCreateCycle([{ source: 'render', target: 'publish', reason: '' }], 'publish', 'render'), true);
  const catalog = buildVariableCatalog(
    {
      dag_id: 'dag',
      task_id: 'dag',
      version: 1,
      status: 'draft',
      nodes: [
        { id: 'search', payload: { type: 'start' } },
        { id: 'render', payload: { type: 'start' } },
        { id: 'publish', payload: { type: 'start' } },
      ],
      edges: [{ source: 'render', target: 'publish', reason: '' }],
    },
    'render',
    { properties: { topic: { type: 'string' } } },
    { report: { id: 'report', paths: ['outputs/report.md'] } },
  );
  assert.deepEqual(catalog.graphInputs.map((item) => item.label), ['DAG input.topic']);
  assert.ok(catalog.nodeOutputs.some((item) => item.label === 'search.output'));
  assert.equal(catalog.nodeOutputs.some((item) => item.label === 'publish.output'), false);
  assert.ok(catalog.artifacts.some((item) => item.label === 'artifact.report.path'));
});

test('variable catalog expands tool and mcp output schema properties', () => {
  const catalog = buildVariableCatalog(
    {
      dag_id: 'dag',
      task_id: 'dag',
      version: 1,
      status: 'draft',
      nodes: [
        {
          id: 'search',
          payload: {
            type: 'capability',
            invocation: { capability_id: 'tool.search', kind: 'tool', arguments: {} },
          },
        },
        {
          id: 'lookup',
          payload: {
            type: 'capability',
            invocation: { capability_id: 'mcp.weather.lookup', kind: 'mcp', arguments: {} },
          },
        },
        {
          id: 'array_result',
          payload: {
            type: 'capability',
            invocation: { capability_id: 'tool.array_result', kind: 'tool', arguments: {} },
          },
        },
        {
          id: 'review',
          payload: {
            type: 'capability',
            invocation: { capability_id: 'agent.review', kind: 'agent', arguments: {} },
          },
        },
        {
          id: 'render',
          payload: {
            type: 'capability',
            invocation: { capability_id: 'tool.render', kind: 'tool', arguments: {} },
          },
        },
      ],
      edges: [],
    },
    'render',
    {},
    {},
    [
      {
        id: 'tool.search',
        kind: 'tool',
        output_schema: {
          type: 'object',
          properties: {
            title: { type: 'string' },
            url: { type: 'string' },
          },
        },
      },
      {
        id: 'mcp.weather.lookup',
        kind: 'mcp',
        output_schema: {
          properties: {
            temperature: { type: 'number' },
          },
        },
      },
      {
        id: 'agent.review',
        kind: 'agent',
        output_schema: {
          properties: {
            verdict: { type: 'string' },
          },
        },
      },
      {
        id: 'tool.array_result',
        kind: 'tool',
        output_schema: {
          type: 'array',
          properties: {
            invalid: { type: 'string' },
          },
        },
      },
    ],
  );

  assert.ok(catalog.nodeOutputs.some((item) => item.label === 'search.output'));
  assert.ok(catalog.nodeOutputs.some((item) => item.label === 'search.content'));
  assert.ok(catalog.nodeOutputs.some((item) => item.label === 'search.status'));
  assert.ok(catalog.nodeOutputs.some((item) => item.label === 'search.steps'));
  assert.ok(catalog.nodeOutputs.some((item) => item.label === 'search.output.title'));
  assert.ok(catalog.nodeOutputs.some((item) => item.label === 'search.output.url'));
  assert.ok(catalog.nodeOutputs.some((item) => item.label === 'lookup.output.temperature'));
  assert.equal(catalog.nodeOutputs.some((item) => item.label === 'review.output.verdict'), false);
  assert.equal(catalog.nodeOutputs.some((item) => item.label === 'array_result.output.invalid'), false);
  assert.deepEqual(
    catalog.nodeOutputs.find((item) => item.label === 'search.output.title')?.binding,
    makeNodeOutputBinding('search', 'value', ['title']),
  );
});

test('updated orchestration and tools workspaces use real backend data with the design shell', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const typesSource = await readFile(new URL('../src/types.ts', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';
  const orchestrationSource = appSource.match(/function OrchestrationWorkspace[\s\S]*?\nfunction RunDagDialog/)?.[0] ?? '';
  const dynamicSource = appSource.match(/function DynamicOrchestrationWorkspace[\s\S]*?\nfunction OrchestrationWorkspace/)?.[0] ?? '';
  const dynamicEventsSource = appSource.match(/function DynamicChatEvents[\s\S]*?\nfunction DynamicOrchestrationWorkspace/)?.[0] ?? '';
  const runDialogSource = appSource.match(/function RunDagDialog[\s\S]*?\nfunction CapabilityDirectory/)?.[0] ?? '';
  const directorySource = appSource.match(/function CapabilityDirectory[\s\S]*?\nfunction chatCapabilityScopeLabel/)?.[0] ?? '';
  const dagReviewSource = appSource.match(/function DagReviewDialog[\s\S]*?\nfunction NodeEditor/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.ok(orchestrationSource, 'OrchestrationWorkspace function should exist');
  assert.ok(dynamicSource, 'DynamicOrchestrationWorkspace function should exist');
  assert.ok(runDialogSource, 'RunDagDialog function should exist');
  assert.ok(directorySource, 'CapabilityDirectory function should exist');

  assert.doesNotMatch(appSource, /const defaultWorkspaceRoot = 'runs';/);
  assert.match(appSource, /ensureOrchestrationContext\(/);
  assert.match(appSource, /runSavedDagStream\(saved\.id/);
  assert.match(apiSource, /export async function runSavedDagStream/);
  assert.match(apiSource, /\/saved-dags\/\$\{encodeURIComponent\(savedDagId\)\}\/run\/stream/);
  assert.doesNotMatch(appSource, /selectedSidebarConversation|workspaceRootLabel|workspace-root-chip/);
  assert.match(appSource, /run\?\.workspace_path \|\| '\.dagent\/runs'/);
  assert.match(appSource, /<WorkspaceSidebar[\s\S]*artifacts=\{editorArtifacts\}[\s\S]*onCreateArtifact=\{createEditorArtifact\}[\s\S]*onUploadFiles=\{\(files\) => void uploadEditorFiles\(files\)\}/);
  assert.match(appSource, /<OrchestrationWorkspace[\s\S]*capabilities=\{capabilities\}[\s\S]*skills=\{skills\}[\s\S]*mcpServers=\{mcpServers\}[\s\S]*spec=\{editorUserDag\}[\s\S]*dag=\{editorDag\}[\s\S]*onSave=\{\(\) => void persistEditorUserDag\(\)\}[\s\S]*onRun=\{\(\) => void runEditorSpec\(\)\}/);
  assert.match(orchestrationSource, /function AgentNodeScopeEditor/);
  assert.match(orchestrationSource, /config=\{selectedUserNode\?\.agent\}/);
  assert.match(orchestrationSource, /node\.id === selectedNode\.id[\s\S]*normalizeUserDagNode\(\{ \.\.\.node, agent \}\)/);
  assert.match(orchestrationSource, /capability\.enabled && capability\.kind !== 'agent' && capability\.kind !== 'skill'/);
  assert.match(typesSource, /export interface UserDagAgentConfig/);
  assert.match(appSource, /const \[orchestrationMode, setOrchestrationMode\] = useState<OrchestrationMode>\('dynamic'\)/);
  assert.match(appSource, /activeWorkspace === 'orchestration' && orchestrationMode === 'dynamic' \? \([\s\S]*<DynamicOrchestrationWorkspace/);
  assert.match(appSource, /activeWorkspace === 'orchestration' && orchestrationMode === 'static' \? \([\s\S]*<OrchestrationWorkspace/);
  assert.match(appSource, /<CapabilityDirectory[\s\S]*capabilities=\{capabilities\}[\s\S]*skills=\{skills\}[\s\S]*mcpServers=\{mcpServers\}[\s\S]*onRefresh=\{refreshConsoleData\}/);
  assert.match(runDialogSource, /function RunTimelineCapabilityDetails/);
  assert.match(runDialogSource, /function RunTimelineTraceDetails/);
  assert.match(runDialogSource, /function RunTimelineCodeBlock/);
  assert.match(appSource, /appendRunTranscriptTraceEvent\(items, event\)/);
  assert.match(runDialogSource, /<span>参数<\/span>/);
  assert.match(runDialogSource, /执行结果/);
  assert.match(runDialogSource, /item\.result \?\? \(item\.event\.type === 'capability\.call\.started'/);
  assert.match(runDialogSource, /className="run-timeline-detail"/);
  assert.match(appSource, /type StaticDagEditorDraft = \{[\s\S]*spec: UserDag;[\s\S]*savedDagId: string \| null;[\s\S]*revision: number \| null;[\s\S]*layoutPositions: Record<string, XYPosition>;[\s\S]*\};/);
  assert.match(appSource, /type SavedDagView = \{[\s\S]*savedDagId: string;[\s\S]*name: string;[\s\S]*description: string;[\s\S]*spec: UserDag;[\s\S]*layoutPositions: Record<string, XYPosition>;[\s\S]*\};/);
  assert.match(appSource, /const \[editorDagDrafts, setEditorDagDraftsState\] = useState<Record<string, StaticDagEditorDraft>>\(\{\}\);/);
  assert.match(appSource, /const editorDagDraftsRef = useRef<Record<string, StaticDagEditorDraft>>\(\{\}\);/);
  assert.match(appSource, /const visibleSavedDags = useMemo\(\(\) => savedDags\.map/);
  assert.match(appSource, /name: spec\.name \|\| saved\.name/);
  assert.match(appSource, /const draftSpec = draft\?\.spec \?\? saved\.spec/);
  assert.match(appSource, /name: draftSpec\.name \|\| saved\.name/);
  assert.match(appSource, /savedDags=\{visibleSavedDags\}/);
  assert.match(appSource, /const loadEditorUserDag = \(saved: SavedDagView\) => \{/);
  assert.match(appSource, /rememberCurrentEditorDraft\(\);[\s\S]*const draft = editorDagDraftsRef\.current\[saved\.savedDagId\];[\s\S]*savedDagId: saved\.savedDagId/);
  assert.match(appSource, /layoutPositionsFromSavedLayout\(saved\.layout\)/);
  assert.match(appSource, /saveSavedDag\(\{[\s\S]*name: spec\.name,[\s\S]*description: spec\.description \?\? '',[\s\S]*savedDagId: editorSavedDagId/);
  assert.match(appSource, /saveSavedDag\(\{[\s\S]*savedDagId: editorSavedDagId,[\s\S]*expectedRevision: editorSavedDagRevision,[\s\S]*layout: savedLayoutWithNodePositions/);
  assert.match(appSource, /const hydrateOrchestrationConversation = useCallback\(async \(conversation: ApiConversation\) => \{/);
  assert.match(appSource, /getOrchestrationSessionByConversation\(conversation\.id\)/);
  assert.match(appSource, /runTranscriptFromTraceEvents\(traceEvents\)/);
  assert.match(appSource, /const ensureEditorDagSavedForRun = async \(spec: UserDag\): Promise<SavedDag \| null> => \{/);
  assert.match(appSource, /savedDagMatchesEditorSpec\(saved, spec\)/);
  assert.match(appSource, /const savedDagsRef = useRef<SavedDag\[\]>\(\[\]\);/);
  assert.match(appSource, /const conversationsRef = useRef<ApiConversation\[\]>\(\[\]\);/);
  assert.match(appSource, /const orchestrationConversationIdsKey = useMemo/);
  const hydrationEffectSource = appSource.match(/useEffect\(\(\) => \{\s*if \(activeWorkspace !== 'orchestration'[\s\S]*?\n  \}, \[[\s\S]*?orchestrationConversationIdsKey,[\s\S]*?\]\);/)?.[0] ?? '';
  assert.ok(hydrationEffectSource, 'orchestration hydration effect should exist');
  assert.match(hydrationEffectSource, /conversationsRef\.current/);
  assert.match(hydrationEffectSource, /const hydrateKey = `\$\{kind\}:\$\{preferred\.id\}`;/);
  assert.doesNotMatch(hydrationEffectSource, /savedRevisionKey|preferred\.updated_at|preferred\.last_run_id|savedDags\.map/);
  assert.doesNotMatch(hydrationEffectSource, /\n\s*conversations,\n|\n\s*savedDags,\n|\n\s*selectedConversation,\n/);
  const ensureContextSource = appSource.match(/const ensureOrchestrationContext = async \([\s\S]*?\n  const uploadEditorFiles/)?.[0] ?? '';
  assert.ok(ensureContextSource, 'ensureOrchestrationContext function should exist');
  assert.match(ensureContextSource, /catch \(exc\) \{[\s\S]*isOrchestrationSessionConflict\(exc\)[\s\S]*getOrchestrationSessionByConversation\(conversation\.id\)/);
  const runEditorSpecSource = appSource.match(/const runEditorSpec = async \(\) => \{[\s\S]*?\n  function dynamicReviewLevel/)?.[0] ?? '';
  assert.match(appSource, /const editorRunInFlightRef = useRef\(false\);/);
  assert.match(runEditorSpecSource, /if \(editorRunning \|\| editorRunInFlightRef\.current\) return;/);
  assert.match(runEditorSpecSource, /editorRunInFlightRef\.current = true;[\s\S]*setEditorRunning\(true\);[\s\S]*ensureEditorDagSavedForRun\(spec\)/);
  assert.match(runEditorSpecSource, /ensureEditorDagSavedForRun\(spec\)/);
  assert.doesNotMatch(runEditorSpecSource, /persistEditorUserDag\(\)/);
  assert.match(appSource, /targetProjectId\?: string \| null/);
  assert.match(appSource, /selectedConversation\?\.project_id === targetProjectId/);
  assert.match(appSource, /refreshConversations\(\)/);
  assert.match(sidebarSource, /orchestrationSubnav/);
  assert.match(sidebarSource, /动态编排/);
  assert.match(sidebarSource, /静态编排/);
  assert.match(sidebarSource, /编排列表/);
  assert.match(sidebarSource, /Artifacts/);
  assert.match(sidebarSource, /className="sidebar-artifact-section"/);
  assert.match(sidebarSource, /<UploadPicker variant="sidebar" onUploadFiles=\{onUploadFiles\} \/>/);
  assert.match(sidebarSource, /onCreateArtifact/);
  assert.match(sidebarSource, /onEditArtifact\(artifact\.id\)/);
  assert.match(sidebarSource, /onDeleteArtifact\(artifact\.id\)/);
  assert.match(sidebarSource, /sidebar-capability-nav/);
  assert.match(sidebarSource, /toolsSub/);

  assert.match(orchestrationSource, /className="design-orchestration-workspace"/);
  assert.match(orchestrationSource, /className="orchestration-canvas"/);
  assert.match(orchestrationSource, /<ReactFlow/);
  assert.match(orchestrationSource, /key=\{spec\.id\}/);
  assert.match(orchestrationSource, /nodeTypes=\{designNodeTypes\}/);
  assert.match(orchestrationSource, /className="orchestration-flow"/);
  assert.match(appSource, /function dagNameInputCh\(value: string\)/);
  assert.match(orchestrationSource, /style=\{\{ width: `\$\{dagNameInputCh\(spec\.name \|\| 'untitled_dag'\)\}ch` \}\}/);
  assert.match(orchestrationSource, /proOptions=\{\{ hideAttribution: true \}\}/);
  assert.match(orchestrationSource, /onInit=\{setFlowInstance\}/);
  assert.match(orchestrationSource, /<ReactFlow[\s\S]*<CanvasViewportControls hasNodes=\{nodes\.length > 0\} \/>[\s\S]*<\/ReactFlow>/);
  assert.match(orchestrationSource, /screenToFlowPosition/);
  assert.match(orchestrationSource, /onPaneClick=\{handlePaneClick\}/);
  assert.match(orchestrationSource, /onAddNode\(contextCapability, contextMenu\.flowPosition\)/);
  assert.doesNotMatch(orchestrationSource, /fitView=\{nodes\.length > 1\}/);
  assert.match(orchestrationSource, /className="node-inspector static-node-inspector"/);
  assert.doesNotMatch(orchestrationSource, /<label>节点 ID<\/label>/);
  assert.doesNotMatch(orchestrationSource, /节点：\$\{contextMenu\.nodeId\}/);
  assert.match(orchestrationSource, /\n\s+运行\n/);
  assert.doesNotMatch(orchestrationSource, /运行编排/);
  assert.match(orchestrationSource, /节点检查器/);
  assert.match(orchestrationSource, /<InspectorArgumentEditor[\s\S]*value=\{selectedInvocation\.arguments \?\? \{\}\}[\s\S]*parameters=\{selectedCapability\?\.parameters\}/);
  assert.match(orchestrationSource, /dag=\{dag\}/);
  assert.match(orchestrationSource, /nodeId=\{selectedNormalized\.id\}/);
  assert.match(orchestrationSource, /onEnsureDependency=\{ensureBindingDependency\}/);
  assert.match(orchestrationSource, /参数/);
  assert.match(orchestrationSource, /键值/);
  assert.match(orchestrationSource, /Raw/);
  assert.match(orchestrationSource, /固定值/);
  assert.match(orchestrationSource, /变量/);
  assert.match(appSource, /function ValueBindingEditor/);
  assert.match(appSource, /function CanvasViewportControls/);
  assert.match(appSource, /const flowInstance = useReactFlow\(\);/);
  assert.match(appSource, /const viewportReady = flowInstance\.viewportInitialized;/);
  assert.match(appSource, /const visibleNodes = flowInstance\.getNodes\(\);/);
  assert.match(appSource, /const bounds = flowInstance\.getNodesBounds\(visibleNodes\);/);
  assert.match(appSource, /flowInstance\.fitBounds\(bounds, \{ padding: 0\.25, duration: 220 \}\)/);
  assert.doesNotMatch(appSource, /flowInstance\.fitView\(\{ padding: 0\.25, duration: 220 \}\)/);
  assert.match(appSource, /flowInstance\.zoomIn\(\{ duration: 160 \}\)/);
  assert.match(appSource, /flowInstance\.zoomOut\(\{ duration: 160 \}\)/);
  assert.match(appSource, /import \{ canvasCenterNodePosition \} from '\.\/canvasPositions';/);
  assert.doesNotMatch(appSource, /return \{ x: 300, y: 220 \};/);
  assert.match(appSource, /className="canvas-viewport-controls nopan nodrag"/);
  assert.match(appSource, /onPointerDown=\{stopCanvasEvent\}/);
  assert.match(appSource, /function buildVariableOptionGroups/);
  assert.match(appSource, /function ensureBindingDependency/);
  assert.match(appSource, /function capabilityOptionGroups/);
  assert.match(orchestrationSource, /<optgroup key=\{group\.kind\} label=\{group\.label\}>/);
  assert.match(appSource, /Parameter binding\./);
  assert.match(orchestrationSource, /KEY/);
  assert.match(orchestrationSource, /VALUE/);
  assert.match(orchestrationSource, /添加参数/);
  assert.match(orchestrationSource, /fields\.map\(\(field, index\) =>/);
  assert.match(orchestrationSource, /key=\{`inspector-argument-\$\{field\.fixed \? key : index\}`\}/);
  assert.doesNotMatch(orchestrationSource, /defaultValue=\{JSON\.stringify\(selectedInvocation\.arguments/);
  assert.match(orchestrationSource, /Artifact 绑定/);
  assert.match(orchestrationSource, /patchArtifactList\('inputs', artifact\.id, event\.target\.checked\)/);
  assert.match(orchestrationSource, /patchArtifactList\('outputs', artifact\.id, event\.target\.checked\)/);
  assert.doesNotMatch(orchestrationSource, /onCreateArtifact|onDeleteArtifact|onUploadFiles|onUploadToArtifact|handleArtifactUpload|上传到此 artifact|设为 path/);
  assert.doesNotMatch(orchestrationSource, /console-grid orchestration-grid|flow-workbench|className="orchestration-edges"|<Controls|orchestration-summary-strip/);
  assert.match(appSource, /function DesignDagNode/);
  assert.match(appSource, /type: 'designDag'/);
  assert.match(appSource, /type: 'designDag',[\s\S]*width:\s*192,[\s\S]*height:\s*64,/);
  assert.match(dagReviewSource, /nodeTypes=\{designNodeTypes\}/);
  assert.match(dagReviewSource, /className="orchestration-flow"/);
  assert.match(appSource, /handles:\s*\[[\s\S]*id:\s*'in'[\s\S]*type:\s*'target'[\s\S]*id:\s*'out'[\s\S]*type:\s*'source'/);
  assert.match(appSource, /<Handle[\s\S]*id="in"[\s\S]*position=\{Position\.Left\}[\s\S]*type="target"/);
  assert.match(appSource, /<Handle[\s\S]*id="out"[\s\S]*position=\{Position\.Right\}[\s\S]*type="source"/);
  assert.match(appSource, /sourceHandle:\s*'out'/);
  assert.match(appSource, /targetHandle:\s*'in'/);
  assert.match(appSource, /function nextHorizontalNodePosition/);
  assert.match(appSource, /onAddNode: \(capability\?: CapabilityDefinition, position\?: XYPosition\) => void;/);
  assert.match(appSource, /graphFromDag\(nextDag, nextPositions\)/);
  assert.match(appSource, /const selectedNode = dag\.nodes\.find\(\(node\) => node\.id === selectedId\) \?\? null;/);
  assert.match(orchestrationSource, /const canvasRef = useRef<HTMLDivElement \| null>\(null\);/);
  assert.match(orchestrationSource, /const firstNodePosition = \(\) => nodes\.length \? undefined : canvasCenterNodePosition\(flowInstance, canvasRef\.current\);/);
  assert.match(orchestrationSource, /<div className="orchestration-canvas" ref=\{canvasRef\}>/);
  assert.match(orchestrationSource, /onClick=\{\(\) => onAddNode\(undefined, firstNodePosition\(\)\)\}[\s\S]*添加第一个节点/);
  assert.match(appSource, /key: 'orchestration', label: '智能体编排'/);

  assert.match(runDialogSource, /运行编排/);
  assert.match(runDialogSource, /运行时间线/);
  assert.match(runDialogSource, /初始输入/);
  assert.match(runDialogSource, /再次运行|开始运行/);
  assert.doesNotMatch(runDialogSource, /Run DAG|Start Run|Run Again|Run Context|Input JSON|Blocking Issues|Review Nodes|No file inputs/);

  assert.match(directorySource, /design-tools-workspace/);
  assert.match(directorySource, /className="tools-detail-panel"/);
  assert.match(directorySource, /skill-editor-toolbar/);
  assert.match(directorySource, /导入 Python 工具|导入技能|保存配置/);
  assert.match(directorySource, /createPythonTool\(/);
  assert.match(directorySource, /uploadPythonTool\(/);
  assert.match(directorySource, /validatePythonTool\(/);
  assert.match(directorySource, /testCapability\(/);
  assert.match(appSource, /installSkill\(/);
  assert.match(directorySource, /createMcpServer\(/);
  assert.doesNotMatch(directorySource, /Capability Workbench|Capability Detail|console-grid directory-grid/);
  assert.doesNotMatch(appSource.match(/function ChatWorkspace[\s\S]*?\nfunction DesignEmptyConversation/)?.[0] ?? '', /dynamicAdjust|onDynamicAdjustChange|动态调整/);
  assert.match(appSource, /function DynamicOrchestrationWorkspace/);
  assert.match(apiSource, /export interface ChatStreamMessage/);
  assert.match(typesSource, /export interface DagValidationIssue/);
  assert.match(typesSource, /export interface DagValidationResult/);
  assert.match(apiSource, /export async function validateDag/);
  assert.match(apiSource, /export async function streamMessagesTask/);
  assert.match(apiSource, /messages,\s*target,\s*review_level: reviewLevel/);
  assert.match(appSource, /streamMessagesTask/);
  assert.match(appSource, /type DynamicChatMessage = ChatStreamMessage & \{ timelineOrder: number \};/);
  assert.match(appSource, /type DynamicTraceLogEvent = TraceLogEvent & \{ timelineOrder: number \};/);
  assert.match(appSource, /const nextDynamicTimelineOrder = useCallback/);
  assert.match(appSource, /const appendDynamicMessage = useCallback/);
  assert.match(appSource, /const \[dynamicMessages, setDynamicMessages\] = useState<DynamicChatMessage\[\]>\(\[\]\);/);
  assert.match(appSource, /const \[dynamicTrace, setDynamicTrace\] = useState<DynamicTraceLogEvent\[\]>\(\[\]\);/);
  assert.match(appSource, /function buildDynamicDagMessages\(history: DynamicChatMessage\[\], prompt: string, dag: Dag\): ChatStreamMessage\[\]/);
  assert.match(appSource, /当前可编辑 DAG 快照/);
  assert.match(appSource, /JSON\.stringify\(dynamicDagForPrompt\(dag\), null, 2\)/);
  assert.match(appSource, /const dynamicRequestMessages = buildDynamicDagMessages\(dynamicMessages, prompt, dynamicDag\);/);
  assert.match(appSource, /ensureOrchestrationContext\(\s*'dynamic_dag'/);
  assert.match(appSource, /streamMessagesTask\([\s\S]*dynamicRequestMessages,[\s\S]*'dag',[\s\S]*dynamicReviewLevel\(\),[\s\S]*dynamicHandlers\(context\.request\),[\s\S]*undefined,[\s\S]*dynamicAdjust,[\s\S]*\{ conversation: context\.request \}/);
  assert.match(appSource, /function dynamicReviewLevel/);
  assert.doesNotMatch(dynamicSource, /<select[\s\S]*reviewLevels|onReviewLevelChange|reviewLevel: ReviewLevel/);
  assert.match(appSource, /className="dynamic-orchestration-chat"/);
  assert.match(dynamicSource, /<div className="dynamic-chat-head">\s*<strong>动态编排<\/strong>\s*<\/div>/);
  assert.doesNotMatch(dynamicSource, /任务目标 \/ SOP/);
  assert.match(appSource, /生成 DAG/);
  assert.match(appSource, /运行/);
  assert.match(appSource, /resumeDagReview\([\s\S]*reviewId,[\s\S]*dag,[\s\S]*dynamicReviewLevel\(\),[\s\S]*true,[\s\S]*dynamicHandlers\(context\.request\),[\s\S]*dynamicRunState,[\s\S]*undefined,[\s\S]*\{ conversation: context\.request \}/);
  assert.doesNotMatch(appSource, /resumeDagReview\(reviewId, null, dynamicReviewLevel\(\), false/);
  assert.match(appSource, /const dynamicDagRef = useRef<Dag>\(emptyDag\);/);
  assert.match(appSource, /function preserveDynamicDagEdges\(nextDag: Dag\): Dag/);
  assert.match(appSource, /syncDynamicDag\(preserveDynamicDagEdges\(nextDag\)\);/);
  assert.match(appSource, /if \(state\?\.dag\) syncDynamicDag\(preserveDynamicDagEdges\(state\.dag\)\);/);
  assert.match(appSource, /onPatchDynamicNode/);
  assert.match(appSource, /onAddDynamicNode/);
  assert.match(appSource, /onDeleteDynamicNode/);
  assert.match(appSource, /nodesDraggable/);
  assert.match(dynamicSource, /defaultViewport=\{\{ x: 0, y: 0, zoom: 1 \}\}/);
  assert.match(dynamicSource, /fitView=\{false\}/);
  assert.match(dynamicSource, /const \[flowInstance, setFlowInstance\] = useState<ReactFlowInstance \| null>\(null\);/);
  assert.match(dynamicSource, /const canvasRef = useRef<HTMLDivElement \| null>\(null\);/);
  assert.match(dynamicSource, /const firstNodePosition = \(\) => nodes\.length \? undefined : canvasCenterNodePosition\(flowInstance, canvasRef\.current\);/);
  assert.match(dynamicSource, /onInit=\{setFlowInstance\}/);
  assert.match(dynamicSource, /<ReactFlow[\s\S]*<CanvasViewportControls hasNodes=\{nodes\.length > 0\} \/>[\s\S]*<\/ReactFlow>/);
  assert.match(dynamicSource, /<div className="orchestration-canvas dynamic-orchestration-canvas" ref=\{canvasRef\}>/);
  assert.match(dynamicSource, /onClick=\{\(\) => onAddNode\(undefined, firstNodePosition\(\)\)\}[\s\S]*添加第一个节点/);
  assert.doesNotMatch(dynamicSource, /fitView=\{!nodes\.length\}/);
  assert.doesNotMatch(dynamicSource, /\{nodes\.length \? \([\s\S]*<ReactFlow/);
  assert.match(dynamicSource, /<ReactFlow[\s\S]*\{!nodes\.length \? \(/);
  assert.doesNotMatch(dynamicSource, /className="dynamic-orchestration-side"/);
  assert.match(dynamicSource, /<DynamicChatEvents[\s\S]*dag=\{dag\}[\s\S]*finalAnswer=\{finalAnswer\}[\s\S]*message=\{message\}[\s\S]*messages=\{messages\}[\s\S]*trace=\{trace\}/);
  assert.doesNotMatch(dynamicSource, /<strong>\{message \|\| '等待任务'\}<\/strong>/);
  assert.match(appSource, /const \[dynamicFinalAnswer, setDynamicFinalAnswer\] = useState\(''\);/);
  assert.match(appSource, /const \[dynamicFinalAnswerOrder, setDynamicFinalAnswerOrder\] = useState\(0\);/);
  assert.match(appSource, /const answer = payload\.result\.output_text \?\? '';/);
  assert.match(appSource, /setOrderedDynamicFinalAnswer\(answer\);/);
  assert.match(appSource, /function executionOrderedNodes\(dag: Dag\): DagNode\[\]/);
  assert.match(appSource, /function dynamicNodeExecutionRows\(dag: Dag, trace: DynamicTraceLogEvent\[\]\)/);
  assert.match(appSource, /function DynamicMarkdown/);
  assert.match(appSource, /<ReactMarkdown remarkPlugins=\{\[remarkGfm\]\}>/);
  assert.match(appSource, /type DynamicTimelineItem =/);
  assert.match(appSource, /function dynamicTimelineItems\([\s\S]*messages: DynamicChatMessage\[\],[\s\S]*statusMessage: string,[\s\S]*statusMessageOrder: number,[\s\S]*rows: DynamicNodeExecutionRow\[\],[\s\S]*finalAnswer: string,[\s\S]*finalAnswerOrder: number,[\s\S]*running: boolean,[\s\S]*\): DynamicTimelineItem\[\]/);
  assert.match(dynamicEventsSource, /dynamic-final-result/);
  assert.match(dynamicEventsSource, /const timelineItems = dynamicTimelineItems\(messages, message, messageOrder, rows, finalText, finalAnswerOrder, running\);/);
  assert.match(appSource, /items\.sort\(\(left, right\) => left\.order - right\.order\)/);
  assert.match(dynamicEventsSource, /timelineItems\.map/);
  assert.doesNotMatch(dynamicEventsSource, /\{messages\.map/);
  assert.match(dynamicEventsSource, /dynamic-conversation-bubble/);
  assert.match(dynamicEventsSource, /dynamic-node-result-list/);
  assert.match(dynamicEventsSource, /dynamic-node-result-list[\s\S]*dynamic-final-result/);
  assert.match(dynamicEventsSource, /item\.type === 'empty'/);
  assert.match(dynamicEventsSource, /<details className=\{`dynamic-chat-bubble assistant dynamic-node-result-card \$\{row\.status\}`\}/);
  assert.match(dynamicEventsSource, /<summary className="dynamic-node-result-summary">/);
  assert.doesNotMatch(dynamicEventsSource, /<details[^>]*open=/);
  assert.match(dynamicEventsSource, /row\.events\.length/);
  assert.doesNotMatch(dynamicEventsSource, /<p>\{clipText\(message|<p>\{clipText\(event\.detail/);
  assert.doesNotMatch(dynamicEventsSource, /dynamic-chat-run-status|dynamic-trace-count|traceCount|运行状态/);
  assert.match(dynamicSource, /className=\{`dynamic-orchestration-body \$\{selectedNormalized \? 'with-inspector' : ''\}`\}/);
  assert.doesNotMatch(appSource.match(/const onAddDynamicNode[\s\S]*?};/)?.[0] ?? '', /setDynamicSelectedId\(id\)/);
  assert.match(appSource, /undefined,[\s\S]*dynamicAdjust,[\s\S]*\{ conversation: context\.request \}/);
  assert.match(apiSource, /dynamicAdjust\?: boolean/);
  assert.match(apiSource, /body\.dynamic_adjust = dynamicAdjust/);
  assert.match(apiSource, /dynamic_adjust\?: boolean/);

  assert.match(css, /\.design-orchestration-workspace/);
  assert.match(css, /\.orchestration-canvas/);
  assert.match(css, /\.orchestration-empty-canvas\s*\{[^}]*left:\s*50%;[^}]*top:\s*50%;[^}]*transform:\s*translate\(-50%, -50%\);/s);
  assert.doesNotMatch(css, /\.orchestration-empty-canvas\s*\{[^}]*left:\s*80px;[^}]*top:\s*80px;/s);
  assert.match(css, /\.canvas-viewport-controls/);
  assert.match(css, /\.canvas-viewport-controls button/);
  assert.doesNotMatch(css, /\.orchestration-name-input\s*\{[^}]*\n\s*width:\s*min\(280px, 28vw\);/s);
  assert.match(css, /\.orchestration-name-input\s*\{[^}]*max-width:\s*min\(280px, 28vw\);/s);
  assert.match(css, /\.node-inspector/);
  assert.match(css, /\.static-node-inspector\s*\{[^}]*width:\s*clamp\(420px,\s*32vw,\s*520px\);/s);
  assert.match(css, /\.sidebar-artifact-section/);
  assert.match(css, /\.artifact-edit-dialog/);
  assert.match(css, /\.inspector-argument-toggle/);
  assert.match(css, /\.inspector-argument-header/);
  assert.match(css, /\.inspector-argument-add/);
  assert.match(css, /\.run-dialog-body\s*\{[^}]*grid-template-columns:\s*260px minmax\(0, 1fr\);/s);
  assert.match(css, /\.run-timeline-section/);
  assert.match(css, /\.run-timeline-code/);
  assert.match(css, /\.run-timeline-list\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;[^}]*overflow-y:\s*auto;/s);
  assert.match(css, /\.run-timeline-row\s*\{[^}]*flex:\s*0 0 auto;/s);
  assert.match(css, /\.run-timeline-list\s*\{[^}]*overflow-y:\s*auto;/s);
  assert.doesNotMatch(css, /\.orchestration-canvas-inner|\.orchestration-edges|\.orchestration-node(?:[\s:{\[]|$)|\.flow-workbench/);
  assert.match(css, /\.design-tools-workspace/);
  assert.match(css, /\.tools-detail-panel/);
  assert.match(css, /\.dynamic-orchestration-body\s*\{[^}]*height:\s*100%;/s);
  assert.match(css, /\.dynamic-chat-head\s*\{[^}]*min-height:\s*61px;[^}]*padding:\s*13px 18px;/s);
  assert.match(css, /\.dynamic-chat-head strong\s*\{[^}]*font-size:\s*14px;/s);
  assert.match(css, /\.dynamic-orchestration-body\.with-inspector\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto;/s);
  assert.match(css, /\.dynamic-orchestration-canvas\s*\{[^}]*height:\s*100%;[^}]*overflow:\s*auto;/s);
  assert.match(css, /\.dynamic-orchestration-canvas \.orchestration-flow\s*\{[^}]*height:\s*100%;/s);
  assert.doesNotMatch(css, /\.dynamic-orchestration-side/);
  assert.doesNotMatch(css, /\.dynamic-chat-run-status|\.dynamic-trace-count|\.dynamic-chat-status-head/);
  assert.match(css, /\.dynamic-node-result-list/);
  assert.match(css, /\.dynamic-node-result-card/);
  assert.match(css, /\.dynamic-node-result-card summary/);
  assert.match(css, /\.dynamic-node-result-card\[open\]/);
  assert.match(css, /\.dynamic-markdown/);
  assert.match(css, /\.dynamic-final-result/);
  assert.match(css, /\.dynamic-node-result-card\.running/);
  assert.match(css, /\.dynamic-event-bubble p\s*\{[^}]*overflow-wrap:\s*anywhere;[^}]*white-space:\s*pre-wrap;[^}]*max-height:/s);
});

test('dynamic orchestration sidebar manages persisted history', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.match(apiSource, /export async function updateConversation/);
  assert.match(apiSource, /export async function updateProjectConversation/);
  assert.match(appSource, /updateConversation,/);
  assert.match(appSource, /updateProjectConversation,/);
  assert.match(appSource, /const \[dynamicConversationQuery, setDynamicConversationQuery\] = useState\(''\);/);
  assert.match(appSource, /const visibleDynamicConversations = useMemo\(\(\) => conversations\.filter/);
  assert.match(appSource, /const clearDynamicWorkspace = useCallback\(\(\) => \{/);
  assert.match(appSource, /const createDynamicOrchestration = async \(\) => \{/);
  assert.match(appSource, /const selectDynamicOrchestration = async \(conversationId: string\) => \{/);
  assert.match(appSource, /const saveDynamicConversationTitle = async \(\) => \{/);
  assert.match(appSource, /const deleteDynamicOrchestration = async \(\) => \{/);
  assert.match(appSource, /dynamicConversations=\{visibleDynamicConversations\}/);
  assert.match(appSource, /dynamicConversationQuery=\{dynamicConversationQuery\}/);
  assert.match(appSource, /onNewDynamicOrchestration=\{\(\) => void createDynamicOrchestration\(\)\}/);
  assert.match(sidebarSource, /activeWorkspace === 'orchestration' && orchestrationMode === 'dynamic'/);
  assert.match(sidebarSource, /dynamicConversations\.length \? dynamicConversations\.map/);
  assert.match(sidebarSource, /onSelectDynamicOrchestration\(conversation\.id\)/);
  assert.match(sidebarSource, /onEditDynamicOrchestration\(conversation\.id, conversation\.title\)/);
  assert.match(sidebarSource, /onDeleteDynamicOrchestration\(conversation\.id\)/);
  assert.match(appSource, /function DynamicConversationRenameDialog/);
  assert.match(appSource, /function DynamicConversationDeleteDialog/);
});

test('static orchestration sidebar supports saved DAG delete and run history', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';
  const orchestrationSource = appSource.match(/function OrchestrationWorkspace[\s\S]*?\nfunction RunDagDialog/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.ok(orchestrationSource, 'OrchestrationWorkspace function should exist');
  assert.match(apiSource, /export async function deleteSavedDag/);
  assert.match(apiSource, /export async function listSavedDagRuns/);
  assert.match(appSource, /deleteSavedDag,/);
  assert.match(appSource, /listSavedDagRuns,/);
  assert.match(appSource, /const \[staticDagDeleteTargetId, setStaticDagDeleteTargetId\] = useState\(''\);/);
  assert.match(appSource, /const \[staticRunHistory, setStaticRunHistory\] = useState<ApiRunSummary\[\]>\(\[\]\);/);
  assert.match(appSource, /const confirmDeleteSavedDag = async \(\) => \{/);
  assert.match(appSource, /await deleteSavedDag\(staticDagDeleteTargetId\)/);
  assert.match(appSource, /listSavedDagRuns\(editorSavedDagId\)/);
  assert.match(appSource, /const selectStaticRunHistory = async \(runId: string\) => \{/);
  assert.match(appSource, /finishedRunResultFromEvents\(events\)/);
  assert.match(sidebarSource, /className=\{item\.savedDagId === selectedDagId \? 'sidebar-saved-dag-row active' : 'sidebar-saved-dag-row'\}/);
  assert.match(sidebarSource, /onDeleteSavedDag\(item\.savedDagId\)/);
  assert.match(appSource, /function SavedDagDeleteDialog/);
  assert.match(appSource, /function RunHistoryPanel/);
  assert.match(orchestrationSource, /runHistory=\{runHistory\}/);
  assert.match(orchestrationSource, /<RunHistoryPanel[\s\S]*title="运行历史"/);
  assert.match(css, /\.sidebar-saved-dag-row/);
  assert.match(css, /\.run-history-panel/);
  assert.match(css, /\.run-history-list/);
});

test('dynamic orchestration workspace shows session run history', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const dynamicSource = appSource.match(/function DynamicOrchestrationWorkspace[\s\S]*?\nfunction OrchestrationWorkspace/)?.[0] ?? '';

  assert.ok(dynamicSource, 'DynamicOrchestrationWorkspace function should exist');
  assert.match(apiSource, /export async function listOrchestrationSessionRuns/);
  assert.match(appSource, /listOrchestrationSessionRuns,/);
  assert.match(appSource, /const \[dynamicOrchestrationSessionId, setDynamicOrchestrationSessionId\] = useState\(''\);/);
  assert.match(appSource, /setDynamicOrchestrationSessionId\(session\.id\)/);
  assert.match(appSource, /listOrchestrationSessionRuns\(dynamicOrchestrationSessionId\)/);
  assert.match(appSource, /const selectDynamicRunHistory = async \(runId: string\) => \{/);
  assert.match(appSource, /setDynamicSelectedRunId\(runId\)/);
  assert.match(appSource, /setDynamicTrace\(mapRunTrace\(nextState\.trace\)\.map/);
  assert.match(appSource, /runHistory=\{\{[\s\S]*runs: dynamicRunHistory,[\s\S]*selectedRunId: dynamicSelectedRunId/);
  assert.match(dynamicSource, /runHistory: RunHistoryPanelData;/);
  assert.match(dynamicSource, /<RunHistoryPanel[\s\S]*title="运行历史"[\s\S]*runHistory=\{runHistory\}/);
});

test('capability management nests resources under the sidebar menu with list creation actions', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';
  const directorySource = appSource.match(/function CapabilityDirectory[\s\S]*?\nfunction AgentManagementWorkspace/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.ok(directorySource, 'CapabilityDirectory should exist');

  assert.match(appSource, /\{ key: 'tools', label: '能力管理', icon: <Blocks size=\{16\} \/> \}/);
  assert.doesNotMatch(appSource, /\{ key: 'tools', label: '工具管理'/);
  assert.match(appSource, /const \[capabilityCreationIntent, setCapabilityCreationIntent\] = useState<ToolDirectoryTab \| null>\(null\);/);
  assert.match(appSource, /creationIntent=\{capabilityCreationIntent\}/);
  assert.match(appSource, /onCreationIntentChange=\{setCapabilityCreationIntent\}/);

  assert.match(sidebarSource, /className="sidebar-capability-nav"/);
  assert.match(sidebarSource, /className="sidebar-capability-chevron"/);
  assert.match(sidebarSource, /className="sidebar-subnav nested"/);
  assert.match(sidebarSource, /onCreateTool/);
  assert.match(sidebarSource, /onImportSkill/);
  assert.match(sidebarSource, /onCreateMcp/);
  assert.match(sidebarSource, /className="sidebar-tool-list-head"/);
  assert.match(appSource, /function SidebarSearchField/);
  assert.match(appSource, /function matchesSearchQuery/);
  assert.match(appSource, /function normalizeSearchQuery/);
  assert.match(sidebarSource, /<SidebarSearchField[\s\S]*value=\{toolsQuery\}[\s\S]*onChange=\{onToolsQueryChange\}/);
  assert.match(sidebarSource, /const sidebarToolTree = buildToolManagementTree\(capabilities, pythonTools, normalizedToolsQuery\);/);
  assert.match(sidebarSource, /collapsedResourceTreeKeys/);
  assert.match(sidebarSource, /toggleResourceTreeKey/);
  assert.match(sidebarSource, /const renderResourceTreeBranch/);
  assert.doesNotMatch(sidebarSource, /const renderToolBranch/);
  assert.match(sidebarSource, /onSelect\?\.\(\);[\s\S]*toggleResourceTreeKey\(treeKey\);/);
  assert.match(sidebarSource, /className="sidebar-skill-row-main"/);
  assert.match(sidebarSource, /className="sidebar-skill-toggle"/);
  assert.match(sidebarSource, /data-open=\{isResourceTreeOpen\(treeKey\)\}/);
  assert.match(sidebarSource, /className=\{`sidebar-skill-file-tree \$\{treeClassName\}`\}/);
  assert.match(sidebarSource, /label: '自定义工具'/);
  assert.match(sidebarSource, /label: 'Python 脚本'/);
  assert.match(sidebarSource, /const sidebarMcpTree = buildMcpManagementTree\(mcpServers, normalizedToolsQuery\);/);
  assert.match(sidebarSource, /const renderMcpTree/);
  assert.match(sidebarSource, /tools\.map/);
  assert.match(sidebarSource, /count: tools\.length/);
  assert.match(sidebarSource, /treeKey: `mcp:\$\{server\.name\}`/);
  assert.match(sidebarSource, /treeClassName: 'sidebar-resource-file-tree'/);
  assert.match(sidebarSource, /renderMcpTree\(\)/);
  assert.match(sidebarSource, /const renderProfileTree/);
  assert.match(sidebarSource, /treeKey: 'agent:profiles:builtin'/);
  assert.match(sidebarSource, /treeKey: 'agent:profiles:custom'/);
  assert.match(sidebarSource, /label: '内置'/);
  assert.match(sidebarSource, /label: '自定义'/);
  assert.match(sidebarSource, /renderProfileRow/);
  assert.match(sidebarSource, /renderAgentPresetRow/);
  assert.match(sidebarSource, /renderProfileTree\(\)/);
  assert.match(sidebarSource, /renderAgentPresetList\(\)/);
  assert.doesNotMatch(sidebarSource, /className="sidebar-tool-tree-group"/);
  assert.doesNotMatch(sidebarSource, /treeKey: 'agent:presets'/);
  assert.doesNotMatch(sidebarSource, /server\.tools\.map/);
  assert.doesNotMatch(sidebarSource, /sidebarMcp\.length \? sidebarMcp\.map/);
  assert.doesNotMatch(sidebarSource, /<div className="sidebar-label inline-label">工具管理<\/div>/);

  assert.match(directorySource, /creationIntent === 'tools'/);
  assert.match(directorySource, /creationIntent === 'skills'/);
  assert.match(directorySource, /creationIntent === 'mcp'/);
  assert.match(directorySource, /onCreationIntentChange\(null\)/);

  assert.match(css, /\.sidebar-capability-nav/);
  assert.match(css, /\.sidebar-capability-chevron/);
  assert.match(css, /\.sidebar-subnav\.nested/);
  assert.doesNotMatch(css, /\.sidebar-subnav\.nested\s*\{[^}]*border-left:/s);
  assert.match(css, /\.sidebar-tool-list-head/);
  assert.match(css, /\.sidebar-resource-tree-row/);
  assert.match(css, /\.sidebar-tool-list \.sidebar-resource-tree-select/);
  assert.match(css, /\.sidebar-resource-file-tree/);
  assert.doesNotMatch(css, /\.sidebar-tool-tree-group/);
});

test('workspace sidebar shares search controls across lower-left resource lists', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');

  assert.match(appSource, /function SidebarSearchField/);
  assert.match(appSource, /function matchesSearchQuery/);
  assert.match(appSource, /function normalizeSearchQuery/);

  assert.match(sidebarSource, /const \[historyQuery, setHistoryQuery\] = useState\(''\);/);
  assert.match(sidebarSource, /const \[dagListQuery, setDagListQuery\] = useState\(''\);/);
  assert.doesNotMatch(sidebarSource, /const \[artifactQuery, setArtifactQuery\] = useState\(''\);/);
  assert.match(sidebarSource, /const \[modelQuery, setModelQuery\] = useState\(''\);/);
  assert.match(sidebarSource, /const \[agentQuery, setAgentQuery\] = useState\(''\);/);

  assert.match(sidebarSource, /const visibleConversations = conversations\.filter\(\(conversation\) => isChatSurfaceConversation\(conversation\) && !conversation\.project_id && matchesSearchQuery/);
  assert.doesNotMatch(sidebarSource, /const visibleHistory = history\.filter/);
  assert.match(sidebarSource, /const visibleSavedDags = savedDags\.filter\(\(dag\) => matchesSearchQuery/);
  assert.doesNotMatch(sidebarSource, /const visibleArtifacts = artifacts\.filter\(\(artifact\) => matchesSearchQuery/);
  assert.match(sidebarSource, /const visibleModels = models\.filter\(\(model\) => matchesSearchQuery/);
  assert.match(sidebarSource, /const visibleProfiles = profiles\.filter\(\(profile\) => matchesSearchQuery/);
  assert.match(sidebarSource, /const visibleAgentPresets = agentPresets\.filter\(\(preset\) => matchesAgentPresetQuery\(preset, normalizedAgentQuery\)\);/);

  assert.match(sidebarSource, /<SidebarSearchField[\s\S]*value=\{historyQuery\}[\s\S]*onChange=\{setHistoryQuery\}/);
  assert.match(sidebarSource, /<SidebarSearchField[\s\S]*value=\{dagListQuery\}[\s\S]*onChange=\{setDagListQuery\}/);
  assert.doesNotMatch(sidebarSource, /<SidebarSearchField[\s\S]*value=\{artifactQuery\}[\s\S]*onChange=\{setArtifactQuery\}/);
  assert.match(sidebarSource, /<SidebarSearchField[\s\S]*value=\{modelQuery\}[\s\S]*onChange=\{setModelQuery\}/);
  assert.match(sidebarSource, /<SidebarSearchField[\s\S]*value=\{agentQuery\}[\s\S]*onChange=\{setAgentQuery\}/);
  assert.match(sidebarSource, /<SidebarSearchField[\s\S]*value=\{toolsQuery\}[\s\S]*onChange=\{onToolsQueryChange\}/);
  assert.doesNotMatch(sidebarSource, /<label className="sidebar-search-field">/);
});

test('skill management shows the selected skill file hierarchy in the left sidebar', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';
  const directorySource = appSource.match(/function CapabilityDirectory[\s\S]*?\nfunction AgentManagementWorkspace/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.ok(directorySource, 'CapabilityDirectory should exist');

  assert.match(sidebarSource, /selectedSkillDetail/);
  assert.match(sidebarSource, /selectedSkillFilePath/);
  assert.match(sidebarSource, /onSelectSkillFile/);
  assert.match(sidebarSource, /onUploadSkillFile/);
  assert.match(sidebarSource, /expandedSkillNames/);
  assert.match(sidebarSource, /expandedSkillFolders/);
  assert.match(sidebarSource, /const isSkillTreeOpen = expandedSkillNames\.has\(name\);/);
  assert.match(sidebarSource, /onClick=\{\(\) => toggleSkillTree\(name\)\}/);
  assert.match(sidebarSource, /className="sidebar-skill-row-main"/);
  assert.match(sidebarSource, /className="sidebar-skill-toggle"/);
  assert.match(sidebarSource, /className="sidebar-skill-folder-toggle"/);
  assert.match(sidebarSource, /className="sidebar-skill-file-tree"/);
  assert.match(sidebarSource, /sidebar-skill-file-row/);
  assert.match(sidebarSource, /className="sidebar-skill-file-group"/);
  assert.match(sidebarSource, /SKILL\.md/);

  assert.doesNotMatch(directorySource, /tools-workspace-skill-tree/);
  assert.doesNotMatch(directorySource, /skill-file-list/);
  assert.doesNotMatch(directorySource, /skill-file-row/);
  assert.match(directorySource, /skill-editor-toolbar/);
  assert.match(directorySource, /selectedSkillFileDetail\?\.file_path \?\? 'SKILL\.md'/);

  assert.match(css, /\.sidebar-skill-file-tree/);
  assert.match(css, /\.sidebar-skill-file-row/);
  assert.match(css, /\.sidebar-skill-file-group/);
  assert.match(css, /\.sidebar-skill-row-main/);
  assert.match(css, /\.sidebar-skill-toggle/);
  assert.match(css, /\.sidebar-skill-folder-toggle/);
  assert.doesNotMatch(css, /\.tools-workspace-skill-tree/);
  assert.doesNotMatch(css, /\.design-tools-workspace\.skills-mode\s*\{[^}]*grid-template-columns:\s*300px minmax\(0, 1fr\);/s);
  assert.match(css, /\.design-tools-workspace\.skills-mode\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);/s);
});

test('capability creation uses modal dialogs and the skill preview fills the detail pane', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const directorySource = appSource.match(/function CapabilityDirectory[\s\S]*?\nfunction AgentManagementWorkspace/)?.[0] ?? '';

  assert.ok(directorySource, 'CapabilityDirectory should exist');
  assert.match(directorySource, /className="capability-create-backdrop"/);
  assert.match(directorySource, /className="capability-create-dialog"/);
  assert.match(directorySource, /creationIntent === 'tools'/);
  assert.match(directorySource, /creationIntent === 'skills'/);
  assert.match(directorySource, /creationIntent === 'mcp'/);
  assert.match(directorySource, /onUploadSkillFile/);
  assert.match(directorySource, /accept="\.md,text\/markdown,text\/plain,\.zip,application\/zip"/);
  assert.match(directorySource, /onUploadSkillFile\(event\.target\.files\?\.\[0\]\)/);
  assert.doesNotMatch(directorySource, /skill-editor-body[\s\S]*<section className="skill-import-panel"/);
  assert.doesNotMatch(directorySource, /tool-detail-surface[\s\S]*<section className="tool-create-drawer"/);
  assert.doesNotMatch(directorySource, /mcp-detail-surface[\s\S]*creatingMcp/);

  assert.match(css, /\.capability-create-backdrop/);
  assert.match(css, /\.capability-create-dialog/);
  assert.doesNotMatch(css, /\.skill-editor-body\s*\{[^}]*grid-template-rows:\s*minmax\(260px, 1fr\) auto;/s);
  assert.match(css, /\.skill-editor-body\s*\{[^}]*grid-template-rows:\s*minmax\(0, 1fr\);/s);
  assert.match(css, /\.skill-editor-body > textarea\s*\{[^}]*height:\s*100%;/s);
});

test('tools management ports the full design columns while keeping backend actions', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const directorySource = appSource.match(/function CapabilityDirectory[\s\S]*?\nfunction AgentManagementWorkspace/)?.[0] ?? '';

  assert.ok(directorySource, 'CapabilityDirectory should end before AgentManagementWorkspace');
  assert.match(directorySource, /const toolTree = buildToolManagementTree\(capabilities, pythonTools, normalizedQuery\);/);
  assert.match(directorySource, /const selectedTool = toolRows\.find/);
  assert.match(directorySource, /tool-info-table/);
  assert.match(directorySource, /tool-schema-block/);
  assert.match(directorySource, /skill-editor-toolbar/);
  assert.match(directorySource, /mcp-config-form/);
  assert.match(directorySource, /className="status-badge mcp-status-badge"/);
  assert.match(appSource, /if \(status === 'connected'\) return '已连接';/);
  assert.doesNotMatch(appSource, /if \(status === 'connected'\) return 'connected';/);
  assert.match(directorySource, /testCapability\(selectedTool\.id, parsed\)/);
  assert.match(appSource, /installSkill\(/);
  assert.match(directorySource, /createMcpServer\(/);
  assert.match(directorySource, /updateMcpServer\(/);
  assert.doesNotMatch(directorySource, /className="tools-directory-main"/);
  assert.doesNotMatch(directorySource, /className="tool-create-panel"/);

  assert.match(css, /\.design-tools-workspace\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);/s);
  assert.match(css, /\.design-tools-workspace\.skills-mode\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);/s);
  assert.match(css, /\.sidebar-skill-file-tree/);
  assert.match(css, /\.tool-info-table/);
  assert.match(css, /\.skill-editor-toolbar/);
  assert.match(css, /\.mcp-config-form/);
  assert.match(css, /\.agent-editor-toolbar \.mcp-status-badge\s*\{[^}]*height:\s*34px;[^}]*min-height:\s*34px;[^}]*border-radius:\s*9px;[^}]*padding:\s*0 14px;[^}]*display:\s*inline-flex;[^}]*align-items:\s*center;[^}]*justify-content:\s*center;[^}]*font-size:\s*13px;[^}]*line-height:\s*1;/s);
});

test('mcp management selects child tools and shows tool details separately from server config', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';
  const directorySource = appSource.match(/function CapabilityDirectory[\s\S]*?\nfunction AgentManagementWorkspace/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.ok(directorySource, 'CapabilityDirectory should exist');

  assert.match(appSource, /const \[selectedToolMcpToolId, setSelectedToolMcpToolId\] = useState\(''\);/);
  assert.match(appSource, /const selectToolMcpResource = useCallback/);
  assert.match(appSource, /selectedToolMcpToolId=\{selectedToolMcpToolId\}/);
  assert.match(appSource, /onSelectToolMcp=\{selectToolMcpResource\}/);
  assert.match(directorySource, /selectedMcpToolId/);
  assert.match(sidebarSource, /selectedToolMcpToolId: string;/);
  assert.match(sidebarSource, /onSelectToolMcp: \(name: string, toolId\?: string \| null\) => void;/);
  assert.match(sidebarSource, /selectedToolMcpToolId === capability\.id/);
  assert.match(sidebarSource, /onClick=\{\(\) => onSelectToolMcp\(server\.name, capability\.id\)\}/);
  assert.match(sidebarSource, /active: selectedToolMcpName === server\.name && !selectedToolMcpToolId/);
  assert.match(sidebarSource, /onSelect: \(\) => onSelectToolMcp\(server\.name, null\)/);
  assert.doesNotMatch(sidebarSource, /onClick=\{\(\) => onSelectToolMcp\(server\.name\)\}/);

  assert.match(directorySource, /const selectedMcpTool = selectedMcp\?\.tools\.find\(\(tool\) => tool\.id === selectedMcpToolId\) \?\? null;/);
  assert.match(directorySource, /selectedMcpTool \? \(/);
  assert.match(directorySource, /MCP 工具/);
  assert.match(directorySource, /selectedMcpTool\.parameters/);
  assert.match(directorySource, /selectedMcpTool\.output_schema/);
  assert.match(directorySource, /selectedMcpTool\.config/);
  assert.match(directorySource, /selectedMcpTool \? <Wrench size=\{15\} \/> : <Database size=\{15\} \/>/);
});

test('system management nests models and OnlyOffice settings', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const typesSource = await readFile(new URL('../src/types.ts', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const appReturnSource = appSource.match(/<main className="workspace">[\s\S]*?<\/main>/)?.[0] ?? '';
  const workspaceItemsSource = appSource.match(/const workspaceItems[\s\S]*?\];/)?.[0] ?? '';
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';
  const systemSource = appSource.match(/function SystemManagementWorkspace[\s\S]*?\nfunction AgentManagementWorkspace/)?.[0] ?? '';
  const modelSource = appSource.match(/function ModelManagementWorkspace[\s\S]*?\nfunction modelInputFromProvider/)?.[0] ?? '';
  const onlyOfficeSource = appSource.match(/function OnlyOfficeSettingsWorkspace[\s\S]*?\nfunction ModelManagementWorkspace/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.ok(systemSource, 'SystemManagementWorkspace should exist');
  assert.ok(modelSource, 'ModelManagementWorkspace should exist');
  assert.ok(onlyOfficeSource, 'OnlyOfficeSettingsWorkspace should exist');
  assert.match(typesSource, /export type WorkspaceKey = 'chat' \| 'orchestration' \| 'tools' \| 'agents' \| 'system';/);
  assert.match(workspaceItemsSource, /\{ key: 'agents', label: '智能体管理', icon: <BotMessageSquare size=\{16\} \/> \}[\s\S]*\{ key: 'system', label: '系统管理'/);
  assert.doesNotMatch(workspaceItemsSource, /\{ key: 'models', label: '模型管理'/);
  assert.match(appReturnSource, /activeWorkspace === 'system' \? \([\s\S]*<SystemManagementWorkspace/);
  assert.match(systemSource, /activeSub === 'models' \? \(/);
  assert.match(systemSource, /<ModelManagementWorkspace/);
  assert.match(systemSource, /<OnlyOfficeSettingsWorkspace/);
  assert.match(appSource, /const \[systemManagementSub, setSystemManagementSub\] = useState<SystemManagementSub>\('models'\);/);
  assert.match(appSource, /const \[models, setModels\] = useState<ModelProvider\[\]>\(\[\]\);/);
  assert.match(appSource, /const \[onlyOfficeSettings, setOnlyOfficeSettings\] = useState<OnlyOfficeSettings>\(defaultOnlyOfficeSettings\);/);
  assert.match(appSource, /const \[activeModelId, setActiveModelId\] = useState\('config'\);/);
  assert.match(appSource, /listModels\(\)/);
  assert.match(appSource, /getOnlyOfficeSettings\(\)/);
  assert.match(appSource, /const \[creatingModel, setCreatingModel\] = useState\(false\);/);
  assert.match(appSource, /<WorkspaceSidebar[\s\S]*systemSub=\{systemManagementSub\}[\s\S]*models=\{models\}[\s\S]*onSystemSubChange=\{setSystemManagementSub\}/);
  assert.match(sidebarSource, /const systemSubnav = \[/);
  assert.match(sidebarSource, /label: '模型管理'/);
  assert.match(sidebarSource, /label: '文档配置'/);
  assert.match(sidebarSource, /onSystemSubChange\(subitem\.key\)/);
  assert.match(sidebarSource, /模型列表/);
  assert.match(sidebarSource, /sidebar-model-list/);
  assert.match(sidebarSource, /onCreateModel/);
  assert.match(sidebarSource, /activeWorkspace === 'system' && systemSub === 'models'/);
  assert.match(sidebarSource, /onSelectModel\(model\.id\)/);
  assert.match(modelSource, /className="design-models-workspace"/);
  assert.match(modelSource, /createModelProvider\(/);
  assert.match(modelSource, /updateModelProvider\(/);
  assert.match(modelSource, /deleteModelProvider\(/);
  assert.match(modelSource, /activateModelProvider\(/);
  assert.match(modelSource, /source === 'config'/);
  assert.match(typesSource, /api_key_configured: boolean;/);
  assert.match(modelSource, /api_key_saved/);
  assert.match(typesSource, /export type ModelApiKeyAction = 'preserve' \| 'replace' \| 'clear';/);
  assert.match(typesSource, /api_key_action: ModelApiKeyAction;/);
  assert.match(modelSource, /const \[apiKeyAction, setApiKeyAction\] = useState<ModelApiKeyAction>/);
  assert.match(modelSource, /api_key_action: creating \? 'replace' : apiKeyAction/);
  assert.match(modelSource, /clearSavedApiKey/);
  assert.match(modelSource, /清除已保存密钥/);
  assert.doesNotMatch(modelSource, /selected\?\.api_key(?!_(?:configured|saved))|selected\.api_key(?!_(?:configured|saved))/);
  assert.doesNotMatch(modelSource, /<label>ID/);
  assert.doesNotMatch(modelSource, /className="model-list-panel"/);
  assert.match(modelSource, /显示名称/);
  assert.match(modelSource, /modelDisplayNameForDraft/);
  assert.match(modelSource, /const \[modelAdvancedOpen, setModelAdvancedOpen\] = useState\(false\);/);
  assert.match(modelSource, /高级配置/);
  assert.match(modelSource, /data-open=\{modelAdvancedOpen\}/);
  assert.match(modelSource, /modelAdvancedOpen \? \(/);
  assert.match(modelSource, /API Key Env/);
  assert.match(modelSource, /Timeout/);
  assert.match(modelSource, /移除 <think> 推理块/);
  assert.match(onlyOfficeSource, /文档配置/);
  assert.match(onlyOfficeSource, /Document Server URL/);
  assert.match(onlyOfficeSource, /Public API Base/);
  assert.match(onlyOfficeSource, /JWT Secret/);
  assert.match(onlyOfficeSource, /允许编辑项目文件/);
  assert.match(onlyOfficeSource, /允许编辑运行产物/);
  assert.match(onlyOfficeSource, /updateOnlyOfficeSettings\(/);
  assert.match(typesSource, /export interface OnlyOfficeSettings/);
  assert.match(typesSource, /jwt_secret\?: string \| null;/);
  assert.match(typesSource, /project_file_edit_enabled: boolean;/);
  assert.match(typesSource, /run_artifact_edit_enabled: boolean;/);

  assert.match(apiSource, /export async function listModels/);
  assert.match(apiSource, /export async function createModelProvider/);
  assert.match(apiSource, /export async function updateModelProvider/);
  assert.match(apiSource, /export async function deleteModelProvider/);
  assert.match(apiSource, /export async function activateModelProvider/);
  assert.match(apiSource, /export async function getOnlyOfficeSettings/);
  assert.match(apiSource, /export async function updateOnlyOfficeSettings/);
  assert.match(apiSource, /jwt_secret: data\.jwt_secret \?\? null/);
  assert.match(apiSource, /project_file_edit_enabled: Boolean\(data\.project_file_edit_enabled\)/);
  assert.match(apiSource, /run_artifact_edit_enabled: Boolean\(data\.run_artifact_edit_enabled\)/);

  assert.match(css, /\.design-models-workspace\s*\{[^}]*display:\s*flex;/s);
  assert.match(css, /\.sidebar-model-list/);
  assert.match(css, /\.model-config-form/);
  assert.match(css, /\.onlyoffice-config-form/);
  assert.match(css, /\.model-secret-state/);
  assert.match(css, /\.model-advanced-toggle/);
});

test('agent management uses real profiles and presets instead of the placeholder workspace', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const appReturnSource = appSource.match(/<main className="workspace">[\s\S]*?<\/main>/)?.[0] ?? '';
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';
  const agentSource = appSource.match(/function AgentManagementWorkspace[\s\S]*?\nfunction DagReviewDialog/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar should exist');
  assert.ok(agentSource, 'AgentManagementWorkspace should exist');
  assert.match(appReturnSource, /activeWorkspace === 'agents' \? \([\s\S]*<AgentManagementWorkspace/);
  assert.doesNotMatch(appReturnSource, /<DesignWorkspacePlaceholder/);
  assert.match(appSource, /const \[agentManagementSub, setAgentManagementSub\] = useState<AgentManagementSub>\('profiles'\);/);
  assert.match(appSource, /<WorkspaceSidebar[\s\S]*agentsSub=\{agentManagementSub\}[\s\S]*profiles=\{profiles\}[\s\S]*selectedProfileId=\{selectedProfileId\}/);
  assert.match(appSource, /<AgentManagementWorkspace[\s\S]*creating=\{creatingProfile\}[\s\S]*profiles=\{profiles\}[\s\S]*selectedId=\{selectedProfileId\}[\s\S]*warnings=\{profileWarnings\}/);
  assert.match(appSource, /setSelectedChatAgentIds\(\(items\) => pruneSelectedAgentIds\(items, agentPresets\)\);/);
  assert.match(sidebarSource, /const agentSubnav = \[/);
  assert.match(sidebarSource, /label: '角色设定'/);
  assert.match(sidebarSource, /label: '智能体预设'/);
  assert.match(sidebarSource, /\{ key: 'profiles' as const, label: '角色设定', icon: <UserCog size=\{16\} \/>, count: profiles\.length \}/);
  assert.match(sidebarSource, /\{ key: 'presets' as const, label: '智能体预设', icon: <Bot size=\{16\} \/>, count: agentPresetCount \}/);
  assert.match(sidebarSource, /onAgentsSubChange\(subitem\.key\)/);
  assert.match(sidebarSource, /const builtinProfiles = visibleProfiles\.filter\(\(profile\) => profile\.source === 'builtin'\);/);
  assert.match(sidebarSource, /const customProfiles = visibleProfiles\.filter\(\(profile\) => profile\.source !== 'builtin'\);/);
  assert.match(sidebarSource, /renderProfileTree\(\)/);
  assert.match(sidebarSource, /builtinProfiles\.map\(\(profile\) => renderProfileRow\(profile\)\)/);
  assert.match(sidebarSource, /customProfiles\.map\(\(profile\) => renderProfileRow\(profile\)\)/);
  assert.match(sidebarSource, /renderAgentPresetList\(\)/);
  assert.match(sidebarSource, /visibleAgentPresets\.map\(\(preset\) => renderAgentPresetRow\(preset\)\)/);
  assert.doesNotMatch(sidebarSource, /treeKey: 'agent:presets'/);
  assert.doesNotMatch(sidebarSource, /agentPresets\.length \? agentPresets\.map/);
  assert.match(agentSource, /className="design-agents-workspace"/);
  assert.doesNotMatch(agentSource, /className="agent-management-tabs"/);
  assert.match(agentSource, /className="agent-prompt-editor"/);
  assert.match(agentSource, /className="agent-metadata-panel"/);
  assert.match(agentSource, /draftContent\.length/);
  assert.doesNotMatch(agentSource, /能力范围/);
  assert.match(agentSource, /复制为本地/);
  assert.match(agentSource, /配置名称/);
  assert.match(agentSource, /智能体预设/);
  assert.match(agentSource, /function AgentPresetManagementPane/);
  assert.match(agentSource, /profileSourceLabel/);
  assert.match(agentSource, /删除配置/);
  assert.doesNotMatch(agentSource, /'agent capability preset'|>Review<|>Skills</);
  assert.doesNotMatch(appSource, /agent presets|No matching agent presets/);
  assert.doesNotMatch(agentSource, /配置文件路径|后端暂未提供/);
  assert.doesNotMatch(agentSource, /Profiles|Agent Profile|Profiles are read-only in this MVP/);
  assert.match(apiSource, /export async function createProfile/);
  assert.match(apiSource, /export async function updateProfile/);
  assert.match(apiSource, /export async function deleteProfile/);
  assert.match(apiSource, /export async function listAgents/);
  assert.match(apiSource, /export async function createAgent/);
  assert.match(apiSource, /export async function updateAgent/);
  assert.match(apiSource, /export async function deleteAgent/);

  assert.match(css, /\.design-agents-workspace\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) 380px;/s);
  assert.match(css, /@media \(max-width: 900px\) \{[\s\S]*\.design-agents-workspace\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);[^}]*grid-template-rows:\s*minmax\(0, 1fr\) auto;/s);
  assert.doesNotMatch(css, /\.agent-management-tabs/);
  assert.match(css, /\.agent-prompt-editor/);
  assert.match(css, /\.agent-name-field/);
  assert.match(css, /\.agent-metadata-panel/);
  assert.match(css, /\.agent-config-list/);
});

test('validation timeline replaces an in-flight validating card with the result', () => {
  const feedback = {
    type: 'validation.feedback',
    passed: false,
    summary: 'The answer missed a requirement.',
    reason: 'Missing artifact details.',
    issues: [{ message: 'Mention the generated file.' }],
  };
  const validating = appendValidatingTimeline([{ type: 'text', content: '初稿。' }]);

  const next = appendValidationTimeline(validating, feedback);

  assert.equal(next.length, 2);
  assert.equal(next.filter((item) => item.type === 'validating').length, 0);
  assert.equal(next.filter((item) => item.type === 'validation').length, 1);
  assert.equal(next[1].event.summary, feedback.summary);
});

test('upsertDagMessageTimeline updates an existing DAG card instead of duplicating after review resume', () => {
  const plannedDag = {
    dag_id: 'dag_review_001',
    task_id: 'task_ui',
    version: 1,
    status: 'review_required',
    nodes: [],
    edges: [],
  };
  const approvedDag = {
    ...plannedDag,
    status: 'approved',
  };
  const messages = [
    {
      role: 'assistant',
      content: '',
      timeline: [{ type: 'dag', dag: plannedDag }],
    },
    {
      role: 'assistant',
      content: '',
      timeline: [{ type: 'text', content: '继续执行。' }],
    },
  ];

  const next = upsertDagMessageTimeline(messages, approvedDag);

  assert.equal(next.length, 2);
  assert.equal(next[0].timeline.filter((item) => item.type === 'dag').length, 1);
  assert.equal(next[0].timeline[0].dag.status, 'approved');
  assert.equal(next[1].timeline.some((item) => item.type === 'dag'), false);
});

test('upsertDagMessageTimeline keeps a rejected DAG card from reverting to running', () => {
  const rejectedDag = {
    dag_id: 'dag_review_001',
    task_id: 'task_ui',
    version: 1,
    status: 'rejected',
    nodes: [],
    edges: [],
  };
  const runningDag = {
    ...rejectedDag,
    status: 'running',
  };
  const reviewDag = {
    ...rejectedDag,
    version: 2,
    status: 'review_required',
  };
  const messages = [
    {
      role: 'assistant',
      content: '',
      timeline: [{ type: 'dag', dag: rejectedDag }],
    },
  ];

  const stillRejected = upsertDagMessageTimeline(messages, runningDag);
  const readyForReview = upsertDagMessageTimeline(stillRejected, reviewDag);

  assert.equal(stillRejected[0].timeline[0].dag.status, 'rejected');
  assert.equal(readyForReview[0].timeline[0].dag.status, 'review_required');
});

test('capability review rejection settles the running tool card', () => {
  const review = {
    review_id: 'review_tool_001',
    kind: 'capability_review',
    message: 'Review capability call.',
    capability_call: {
      invocation_id: 'call_001',
      capability_id: 'tool.shell',
      tool_name: 'tool_shell',
      arguments: { command: 'rm -rf ./tmp' },
    },
  };
  const timeline = [
    {
      type: 'capability',
      event: {
        type: 'capability.call.started',
        invocation_id: 'call_001',
        capability_id: 'tool.shell',
        arguments: { command: 'rm -rf ./tmp' },
      },
    },
  ];

  const next = appendCapabilityReviewDecisionTimeline(timeline, review, false, '不要删除文件');

  assert.equal(next.length, 1);
  assert.equal(next[0].type, 'capability');
  assert.equal(next[0].result.type, 'capability.call.failed');
  assert.equal(next[0].result.invocation_id, 'call_001');
  assert.match(next[0].result.content, /人工审核已拒绝/);
  assert.match(next[0].result.content, /不要删除文件/);
});

test('stream parser preserves capability review tool name', async () => {
  const { streamTask } = await importTypeScriptModule('../src/api.ts', [
    '../src/api.ts',
    '../src/agentScope.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const previousFetch = globalThis.fetch;
  const frame = {
    type: 'review.required',
    data: {
      review_id: 'review_tool_001',
      kind: 'capability_review',
      message: 'Review capability call.',
      capability_call: {
        invocation_id: 'call_001',
        capability_id: 'tool.shell',
        tool_name: 'tool_shell',
        arguments: { command: 'rm -rf ./tmp' },
      },
    },
  };
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(frame)}\n\n`));
      controller.close();
    },
  });
  globalThis.fetch = async () => ({ ok: true, body });
  try {
    let parsedReview;
    await streamTask('run shell', 'tool', 'none', {
      onReview(review) {
        parsedReview = review;
      },
    });

    assert.equal(parsedReview.capability_call.tool_name, 'tool_shell');
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test('stream parser rejects capability review payloads without tool name', async () => {
  const { streamTask } = await importTypeScriptModule('../src/api.ts', [
    '../src/api.ts',
    '../src/agentScope.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const previousFetch = globalThis.fetch;
  const frame = {
    type: 'review.required',
    data: {
      review_id: 'review_tool_001',
      kind: 'capability_review',
      message: 'Review capability call.',
      capability_call: {
        invocation_id: 'call_001',
        capability_id: 'tool.shell',
        arguments: { command: 'rm -rf ./tmp' },
      },
    },
  };
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(frame)}\n\n`));
      controller.close();
    },
  });
  globalThis.fetch = async () => ({ ok: true, body });
  try {
    await assert.rejects(
      streamTask('run shell', 'tool', 'none', {}),
      /Capability review payload missing tool_name/,
    );
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test('dag review resume reuses the DAG assistant turn instead of opening a new chat frame', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const resumeDagSource = appSource.match(/const resumeDag = async[\s\S]*?\n  const confirmDag/)?.[0] ?? '';

  assert.ok(resumeDagSource, 'resumeDag function should exist');
  assert.doesNotMatch(resumeDagSource, /setMessages\(\(items\) => \[[\s\S]*role: 'assistant'/);
});

test('capability review resume keeps streaming in the existing assistant frame', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const resumeCapabilitySource = appSource.match(/const confirmCapabilityReview = async[\s\S]*?\n  const newChat/)?.[0] ?? '';

  assert.ok(resumeCapabilitySource, 'confirmCapabilityReview function should exist');
  assert.doesNotMatch(resumeCapabilitySource, /setMessages\(\(items\) => \[[\s\S]*role: 'assistant'/);
});

test('chat sidebar separates conversations and projects with persisted standalone conversations', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const typesSource = await readFile(new URL('../src/types.ts', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.match(typesSource, /project_id: string \| null;/);
  assert.match(typesSource, /workspace_uri: string;/);
  assert.match(apiSource, /export async function listConversations\(options: \{ kind\?: ApiConversation\['kind'\] \} = \{\}\): Promise<ApiConversation\[\]>/);
  assert.match(apiSource, /export async function listProjectConversations\(\s*projectId: string,\s*options: \{ kind\?: ApiConversation\['kind'\] \} = \{\},\s*\): Promise<ApiConversation\[\]>/);
  assert.match(apiSource, /export async function createConversation\(input: \{ title: string; kind\?: ApiConversation\['kind'\] \}\): Promise<ApiConversation>/);
  assert.match(apiSource, /export async function deleteConversation\(conversationId: string\): Promise<void>/);
  assert.match(apiSource, /export async function deleteProjectConversation\(projectId: string, conversationId: string\): Promise<void>/);
  assert.match(apiSource, /`\$\{API_BASE\}\/conversations`/);
  assert.match(apiSource, /`\$\{API_BASE\}\/conversations\/\$\{encodeURIComponent\(conversationId\)\}`/);
  assert.match(apiSource, /method:\s*'DELETE'/);
  assert.match(appSource, /type ChatWorkspaceSub = 'conversations' \| 'projects';/);
  assert.match(appSource, /const \[chatSub, setChatSub\] = useState<ChatWorkspaceSub>\('conversations'\);/);
  assert.match(appSource, /const \[conversations, setConversations\] = useState<ApiConversation\[\]>\(\[\]\);/);
  assert.match(appSource, /listConversations\(\)/);
  assert.match(appSource, /listProjectConversations\(project\.id\)/);
  assert.match(appSource, /const loadPersistedConversations = useCallback\(async \(projectItems: ApiProject\[\]\) => \{/);
  assert.match(appSource, /deleteConversation,/);
  assert.match(appSource, /deleteProjectConversation,/);
  assert.match(appSource, /const deleteConversationFromSidebar = async \(conversationId: string\)/);
  assert.match(appSource, /const confirmConversationDelete = async \(\)/);
  assert.match(appSource, /await deleteProjectConversation\(conversation\.project_id, conversation\.id\)/);
  assert.match(appSource, /await deleteConversation\(conversation\.id\)/);
  const initialConversationSelectionSource = appSource.match(/setConversations\(conversationItems\);[\s\S]*?setProjectError\(null\);/)?.[0] ?? '';
  assert.ok(initialConversationSelectionSource, 'initial conversation hydration should exist');
  assert.match(initialConversationSelectionSource, /setSelectedConversationId\(\(current\) => \(/);
  assert.match(initialConversationSelectionSource, /\?\s*current\s*:\s*''/);
  assert.doesNotMatch(initialConversationSelectionSource, /conversationItems\.find\(\(conversation\) => conversation\.kind === 'chat' && !conversation\.project_id\)/);
  const selectChatSubSource = appSource.match(/const selectChatSub = \(sub: ChatWorkspaceSub\) => \{[\s\S]*?\n  \};/)?.[0] ?? '';
  assert.ok(selectChatSubSource, 'selectChatSub function should exist');
  assert.match(selectChatSubSource, /setChatSub\(sub\);[\s\S]*if \(sub === 'projects'\)[\s\S]*return;[\s\S]*setSelectedConversationId\(''\);[\s\S]*clearChatSurface\(\);/);
  assert.doesNotMatch(selectChatSubSource, /nextStandalone|conversations\.find\(\(conversation\) => conversation\.kind === 'chat' && !conversation\.project_id\)/);
  const selectWorkspaceSource = appSource.match(/const selectWorkspace = \(workspace: WorkspaceKey\) => \{[\s\S]*?\n  \};/)?.[0] ?? '';
  assert.ok(selectWorkspaceSource, 'selectWorkspace function should exist');
  assert.match(selectWorkspaceSource, /setActiveWorkspace\(workspace\);/);
  assert.match(selectWorkspaceSource, /workspace === 'chat' && chatSub === 'conversations'[\s\S]*setSelectedConversationId\(''\);[\s\S]*clearChatSurface\(\);/);
  assert.match(appSource, /onSelectWorkspace=\{selectWorkspace\}/);
  assert.match(appSource, /<WorkspaceSidebar[\s\S]*chatSub=\{chatSub\}[\s\S]*conversations=\{conversations\}[\s\S]*onChatSubChange=\{selectChatSub\}/);
  assert.doesNotMatch(appSource, /history=\{chatHistory\}/);
  assert.doesNotMatch(appSource, /const chatHistory = useMemo\(\(\) => currentChatHistory/);
  assert.doesNotMatch(appSource, /function currentChatHistory/);
  assert.match(sidebarSource, /chatSub: ChatWorkspaceSub;/);
  assert.match(sidebarSource, /onChatSubChange: \(sub: ChatWorkspaceSub\) => void;/);
  assert.match(sidebarSource, /const chatSubnav = \[/);
  assert.match(sidebarSource, /label: '会话'/);
  assert.match(sidebarSource, /label: '项目'/);
  assert.match(sidebarSource, /\{ key: 'conversations' as const, label: '会话', icon: <MessagesSquare size=\{16\} \/>\, count: standaloneConversationCount \}/);
  assert.match(sidebarSource, /\{ key: 'projects' as const, label: '项目', icon: <FolderKanban size=\{16\} \/>\, count: projects\.length \}/);
  assert.match(sidebarSource, /activeWorkspace === 'chat' && chatSub === 'conversations'/);
  assert.match(sidebarSource, /activeWorkspace === 'chat' && chatSub === 'projects'/);
  assert.match(sidebarSource, /visibleConversations/);
  assert.doesNotMatch(sidebarSource, /history: Array/);
  assert.doesNotMatch(sidebarSource, /visibleHistory/);
  assert.match(appSource, /onDeleteConversation=\{deleteConversationFromSidebar\}/);
  assert.match(sidebarSource, /onDeleteConversation: \(id: string\) => void;/);
  assert.match(sidebarSource, /className=\{conversation\.id === selectedConversationId \? 'sidebar-conversation-row active' : 'sidebar-conversation-row'\}/);
  assert.doesNotMatch(sidebarSource, /className=\{conversation\.id === selectedConversationId \? 'active' : ''\}/);
  const standaloneConversationRowsSource = sidebarSource.match(/visibleConversations\.length \? visibleConversations\.map\(\(conversation\) => \([\s\S]*?\)\) : \(/)?.[0] ?? '';
  assert.ok(standaloneConversationRowsSource, 'standalone conversation rows should exist');
  assert.match(standaloneConversationRowsSource, /<MessageSquare size=\{12\} \/>/);
  assert.match(standaloneConversationRowsSource, /<strong>\{conversation\.title\}<\/strong>/);
  assert.doesNotMatch(standaloneConversationRowsSource, /<em>\{conversation\.status\}<\/em>/);
  assert.match(sidebarSource, /className="sidebar-conversation-delete"/);
  assert.match(sidebarSource, /onDeleteConversation\(conversation\.id\)/);
  assert.match(css, /\.sidebar-history-list\s*\{[^}]*max-height:\s*min\(360px,\s*42vh\);[^}]*overflow-x:\s*hidden;[^}]*overflow-y:\s*auto;/s);
  assert.doesNotMatch(css, /\.sidebar-history-list\s*\{[^}]*border:\s*1px/s);
  assert.match(css, /\.sidebar-history-list button\s*\{[^}]*border:\s*0;[^}]*padding:\s*7px 10px;[^}]*background:\s*transparent;/s);
  assert.match(css, /\.sidebar-conversation-row\s*\{[^}]*border:\s*1px solid #edf0f4;[^}]*border-radius:\s*9px;[^}]*background:\s*#fff;[^}]*overflow:\s*hidden;/s);
  assert.match(css, /\.sidebar-conversation-row:hover,\s*\.sidebar-conversation-row\.active,\s*\.sidebar-project-tree-main:hover,\s*\.sidebar-project-tree-main\.active,\s*\.sidebar-project-conversation-row:hover,\s*\.sidebar-project-conversation-row\.active\s*\{[^}]*border-color:\s*#e7e8ec;[^}]*background:\s*#f0f1f4;/s);
  assert.match(css, /\.sidebar-history-list \.sidebar-conversation-delete\s*\{[^}]*border:\s*0;[^}]*border-radius:\s*0;/s);
  assert.doesNotMatch(css, /\.sidebar-history-list \.sidebar-conversation-delete\s*\{[^}]*border-left:/s);
  assert.match(css, /\.sidebar-history-list \.sidebar-conversation-delete\s*\{[^}]*opacity:\s*0;[^}]*pointer-events:\s*none;[^}]*transition:\s*opacity 120ms ease;/s);
  assert.match(css, /\.sidebar-conversation-row:hover \.sidebar-conversation-delete,\s*\.sidebar-project-conversation-row:hover \.sidebar-conversation-delete\s*\{[^}]*opacity:\s*1;[^}]*pointer-events:\s*auto;/s);
  assert.doesNotMatch(css, /\.(?:sidebar-conversation-row|sidebar-project-conversation-row)(?:\.active|:focus-within) \.sidebar-conversation-delete|\.sidebar-history-list \.sidebar-conversation-delete:focus-visible/s);
});

test('new standalone chat does not create a placeholder conversation title', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const newChatSource = appSource.match(/const newChat = async \(\) => \{[\s\S]*?\n  const clearChatSurface/)?.[0] ?? '';

  assert.ok(newChatSource, 'newChat function should exist');
  assert.match(newChatSource, /if \(streaming\) return;/);
  assert.match(newChatSource, /setSelectedConversationId\(''\);[\s\S]*clearChatSurface\(\);/);
  assert.doesNotMatch(newChatSource, /standaloneConversationCount/);
  assert.doesNotMatch(newChatSource, /createConversation\(/);
  assert.doesNotMatch(newChatSource, /title: `会话 \$\{/);
});

test('persisted chat streams and reviews use conversation context without requiring a project', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const runStreamSource = appSource.match(/const runStream = async[\s\S]*?\n  const stopStream/)?.[0] ?? '';
  const resumeDagSource = appSource.match(/const resumeDag = async[\s\S]*?\n  const confirmDag/)?.[0] ?? '';
  const resumeCapabilitySource = appSource.match(/const confirmCapabilityReview = async[\s\S]*?\n  const newChat/)?.[0] ?? '';

  assert.match(apiSource, /interface ConversationRequestContext \{[\s\S]*projectId\?: string \| null;[\s\S]*conversationId: string;/);
  assert.match(apiSource, /conversation\?: ConversationRequestContext;/);
  assert.match(apiSource, /appendConversationContext\(body, options\.conversation\);/);
  assert.match(apiSource, /function appendConversationContext\(body: Record<string, unknown>, context\?: ConversationRequestContext\): void/);
  assert.match(apiSource, /body\.conversation_id = context\.conversationId;/);
  assert.match(apiSource, /const conversationContext = options\.conversation;/);
  assert.match(apiSource, /const persistedResume = conversationContext !== undefined;/);
  assert.match(apiSource, /projectId\s*\? `\$\{API_BASE\}\/projects\/\$\{encodeURIComponent\(projectId\)\}\/reviews\/\$\{encodeURIComponent\(reviewId\)\}\/resume`[\s\S]*: `\$\{API_BASE\}\/reviews\/\$\{encodeURIComponent\(reviewId\)\}\/resume`/);
  assert.match(apiSource, /persistedResume[\s\S]*approved/);
  assert.match(apiSource, /persistedResume[\s\S]*state/);
  assert.match(runStreamSource, /const conversation = await ensureChatConversation\(prompt, target\);/);
  assert.match(runStreamSource, /const conversationContext = \{ projectId: conversation\.project_id, conversationId: conversation\.id \};/);
  assert.match(runStreamSource, /const streamOptions = \{ signal, uploads: uploadsForRequest, conversation: conversationContext \};/);
  assert.match(runStreamSource, /\}, capabilityScope, null, undefined, streamOptions\);/);
  assert.match(resumeDagSource, /conversation: activeConversationContext/);
  assert.match(resumeCapabilitySource, /conversation: activeConversationContext/);
});

test('persisted conversation selection hydrates the last run snapshot', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const persistedChatSource = await readFile(new URL('../src/persistedChat.ts', import.meta.url), 'utf8');

  assert.match(apiSource, /export interface ApiRunEvent/);
  assert.match(apiSource, /export async function listRunEvents\(runId: string, afterEventId = 0\): Promise<ApiRunEvent\[\]>/);
  assert.match(apiSource, /`\$\{API_BASE\}\/runs\/\$\{encodeURIComponent\(runId\)\}\/events\$\{params\}`/);
  assert.match(persistedChatSource, /export function finishedRunResultFromEvents\(events: ApiRunEvent\[\]\): ApiRunResult \| null/);
  assert.match(persistedChatSource, /export function messagesFromPersistedRunResult\(/);
  assert.match(appSource, /const conversationHydrationRequestRef = useRef\(0\);/);
  assert.match(appSource, /const applyPersistedRunResult = useCallback\(\(result: ApiRunResult, events: ApiRunEvent\[\] = \[\]\) => \{[\s\S]*setRunState\(nextState\);[\s\S]*setTrace\(nextTrace\);[\s\S]*setMessages\(messagesFromPersistedRunResult\(result, nextTrace, events\)\);/);
  assert.match(appSource, /const hydrateConversationSnapshot = useCallback\(async \(conversation: ApiConversation\) => \{[\s\S]*await listRunEvents\(conversation\.last_run_id\);[\s\S]*finishedRunResultFromEvents\(events\);[\s\S]*applyPersistedRunResult\(result, events\);/);
  assert.match(appSource, /const partialMessages = chatMessagesFromPersistedRunEvents\(events, result\);[\s\S]*if \(!result && partialMessages\.length\) \{[\s\S]*setMessages\(partialMessages\);/);
  assert.match(appSource, /if \(!selectedChatSurfaceConversation\?\.last_run_id \|\| streaming \|\| messages\.length \|\| runState\) return;[\s\S]*void hydrateConversationSnapshot\(selectedChatSurfaceConversation\);/);
});

test('persisted conversation hydration restores user and assistant chat turns', async () => {
  const persistedChatSource = await readFile(new URL('../src/persistedChat.ts', import.meta.url), 'utf8');
  const hydrateSource = persistedChatSource.match(/export function messagesFromPersistedRunResult[\s\S]*?\nfunction appendPersistedChatMessage/)?.[0] ?? '';

  assert.ok(hydrateSource, 'messagesFromPersistedRunResult should exist');
  assert.match(persistedChatSource, /function visibleChatContentFromInternalMessage\(message: Record<string, unknown>\): string/);
  assert.match(persistedChatSource, /function inputMessagesFromRunState\(state: ApiRunState \| null\): Record<string, unknown>\[\]/);
  assert.match(persistedChatSource, /state\.internal_messages \?\? \[\]/);
  assert.match(persistedChatSource, /state\.input_message_count/);
  assert.match(hydrateSource, /inputMessagesFromRunState\(state\)/);
  assert.match(hydrateSource, /role !== 'user' && role !== 'assistant'/);
  assert.match(hydrateSource, /appendPersistedChatMessage\(messages, role, content\);/);
  assert.match(persistedChatSource, /role === 'assistant' && last\?\.role === 'assistant'/);
  assert.match(persistedChatSource, /timeline: \[\.\.\.\(last\.timeline \?\? \[\]\), \{ type: 'text', content \}\]/);
  assert.match(persistedChatSource, /function lastAssistantMessageIndex\(messages: ChatMessage\[\]\): number/);
});

test('persisted conversation hydration rebuilds one assistant turn with capability trace', async () => {
  const {
    chatMessagesFromPersistedRunEvents,
    finishedRunResultFromEvents,
  } = await importTypeScriptModule('../src/persistedChat.ts', [
    '../src/persistedChat.ts',
    '../src/chatTimeline.ts',
    '../src/api.ts',
    '../src/agentScope.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const trace = {
    run_id: 'run_1',
    status: 'completed',
    root: {
      id: 'trace_root',
      kind: 'run',
      status: 'completed',
      label: 'run_1',
      ref: { run_id: 'run_1' },
      children: [
        {
          id: 'trace_capability',
          kind: 'capability_call',
          status: 'completed',
          label: 'tool.echo',
          ref: { capability_id: 'tool.echo', invocation_id: 'call_1' },
          capability_execution: {
            invocation: {
              invocation_id: 'call_1',
              capability_id: 'tool.echo',
              kind: 'tool',
              arguments: { text: 'hi' },
            },
            result: {
              invocation_id: 'call_1',
              capability_id: 'tool.echo',
              status: 'completed',
              content: 'echo:hi',
            },
          },
          children: [],
        },
      ],
    },
  };
  const state = {
    run_id: 'run_1',
    kind: 'tool',
    status: 'completed',
    internal_messages: [
      { role: 'user', content: 'echo hi' },
      { role: 'assistant', content: '我会调用工具。' },
      { role: 'assistant', content: '完成：echo:hi' },
    ],
    trace,
  };
  const events = [
    runEvent(1, 'response.content.delta', { delta: '我会调用工具。' }),
    runEvent(2, 'capability.call.started', {
      invocation_id: 'call_1',
      capability_id: 'tool.echo',
      arguments: { text: 'hi' },
    }),
    runEvent(3, 'capability.call.completed', {
      invocation_id: 'call_1',
      capability_id: 'tool.echo',
      content: 'echo:hi',
    }),
    runEvent(4, 'response.content.delta', { delta: '完成：echo:hi' }),
    runEvent(5, 'run.finished', { result: { output_text: '完成：echo:hi', state } }),
  ];

  const result = finishedRunResultFromEvents(events);
  const messages = chatMessagesFromPersistedRunEvents(events, result);
  const assistantMessages = messages.filter((message) => message.role === 'assistant');
  const assistant = assistantMessages[0];

  assert.equal(assistantMessages.length, 1);
  assert.deepEqual(
    assistant.timeline.map((item) => item.type),
    ['text', 'capability', 'text'],
  );
  assert.equal(assistant.timeline[1].event.capability_id, 'tool.echo');
  assert.equal(assistant.timeline[1].result.content, 'echo:hi');
  assert.equal(assistant.traceSnapshot.length, 1);
  assert.equal(assistant.traceSnapshot[0].type, 'capability');
});

test('persisted conversation hydration appends run finished answer after replayed trace', async () => {
  const {
    chatMessagesFromPersistedRunEvents,
    finishedRunResultFromEvents,
  } = await importTypeScriptModule('../src/persistedChat.ts', [
    '../src/persistedChat.ts',
    '../src/chatTimeline.ts',
    '../src/api.ts',
    '../src/agentScope.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const state = {
    run_id: 'run_1',
    kind: 'dynamic_dag',
    status: 'completed',
    internal_messages: [
      { role: 'user', content: 'run dag' },
      { role: 'assistant', content: 'Final answer from run.finished.' },
    ],
  };
  const events = [
    runEvent(1, 'response.content.delta', { delta: '我先说明阶段性情况。' }),
    runEvent(2, 'response.reasoning.delta', { delta: 'planning dag' }),
    runEvent(3, 'capability.call.started', {
      invocation_id: 'call_1',
      capability_id: 'tool.echo',
      arguments: { text: 'ok' },
    }),
    runEvent(4, 'capability.call.completed', {
      invocation_id: 'call_1',
      capability_id: 'tool.echo',
      content: 'echo:ok',
    }),
    runEvent(5, 'run.finished', {
      result: {
        output_text: 'Final answer from run.finished.',
        state,
      },
    }),
  ];

  const result = finishedRunResultFromEvents(events);
  const messages = chatMessagesFromPersistedRunEvents(events, result);
  const assistant = messages.find((message) => message.role === 'assistant');

  assert.ok(assistant, 'assistant message should be restored');
  assert.deepEqual(
    assistant.timeline.map((item) => item.type),
    ['text', 'reasoning', 'capability', 'text'],
  );
  assert.equal(assistant.timeline[0].content, '我先说明阶段性情况。');
  assert.equal(assistant.timeline[3].content, 'Final answer from run.finished.');
  assert.equal(shouldCollapseProcessTimeline(assistant, false), true);
});

test('persisted dynamic DAG hydration hides internal observation prompts', async () => {
  const {
    chatMessagesFromPersistedRunEvents,
    finishedRunResultFromEvents,
  } = await importTypeScriptModule('../src/persistedChat.ts', [
    '../src/persistedChat.ts',
    '../src/chatTimeline.ts',
    '../src/api.ts',
    '../src/agentScope.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const state = {
    run_id: 'task_73fb137e847e45c7b71ee950b4e94107',
    kind: 'dynamic_dag',
    status: 'completed',
    input_message_count: 1,
    user_request: '查找数睿通智库系统白皮书',
    internal_messages: [
      {
        role: 'user',
        content: 'Task id: task_73fb137e847e45c7b71ee950b4e94107\n查找数睿通智库系统白皮书',
      },
      { role: 'assistant', content: '{"nodes":[{"id":"find_white_paper"}]}' },
      {
        role: 'user',
        content: [
          'Task id: task_73fb137e847e45c7b71ee950b4e94107',
          'DAG observation: layer_completed',
          'Task id: task_73fb137e847e45c7b71ee950b4e94107',
          'Completed node outputs:',
          '- find_white_paper: /workspace/数睿通智库系统白皮书.pdf',
          'Node executions:',
          '- node: find_white_paper tool: tool.list_files status: completed',
        ].join('\n'),
      },
      { role: 'assistant', content: 'NO_CHANGE' },
    ],
  };
  const events = [
    runEvent(1, 'run.finished', {
      result: {
        output_text: '已找到白皮书：/workspace/数睿通智库系统白皮书.pdf',
        state,
      },
    }),
  ];

  const result = finishedRunResultFromEvents(events);
  const messages = chatMessagesFromPersistedRunEvents(events, result);

  assert.deepEqual(
    messages.map((message) => [message.role, message.content]),
    [
      ['user', '查找数睿通智库系统白皮书'],
      ['assistant', '已找到白皮书：/workspace/数睿通智库系统白皮书.pdf'],
    ],
  );
  assert.equal(messages.some((message) => message.content.includes('DAG observation')), false);
  assert.equal(messages.some((message) => message.content.includes('NO_CHANGE')), false);
});

test('persisted dynamic DAG hydration rebuilds visible turns from stream events', async () => {
  const {
    chatMessagesFromPersistedRunEvents,
    finishedRunResultFromEvents,
  } = await importTypeScriptModule('../src/persistedChat.ts', [
    '../src/persistedChat.ts',
    '../src/chatTimeline.ts',
    '../src/api.ts',
    '../src/agentScope.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const firstState = {
    run_id: 'task_dynamic',
    kind: 'dynamic_dag',
    status: 'completed',
    input_message_count: 1,
    user_request: 'first request',
    internal_messages: [
      { role: 'user', content: 'Task id: task_dynamic\nfirst request' },
      { role: 'assistant', content: '{"nodes":[{"id":"first"}]}' },
    ],
  };
  const secondState = {
    run_id: 'task_dynamic',
    kind: 'dynamic_dag',
    status: 'completed',
    input_message_count: 5,
    user_request: 'second request',
    internal_messages: [
      { role: 'user', content: 'Task id: task_dynamic\nfirst request' },
      { role: 'assistant', content: '{"nodes":[{"id":"first"}]}' },
      { role: 'user', content: 'Task id: task_dynamic\nDAG observation: layer_completed\nNode executions:\n- first' },
      { role: 'assistant', content: 'NO_CHANGE' },
      { role: 'user', content: 'second request' },
      { role: 'assistant', content: '{"nodes":[{"id":"second"}]}' },
    ],
  };
  const events = [
    runEventInStream(1, 'stream_first', 1, 'run.finished', {
      result: { output_text: 'first answer', state: firstState },
    }),
    runEventInStream(2, 'stream_second', 1, 'response.content.delta', { delta: 'second progress. ' }),
    runEventInStream(3, 'stream_second', 2, 'run.finished', {
      result: { output_text: 'second answer', state: secondState },
    }),
  ];

  const result = finishedRunResultFromEvents(events);
  const messages = chatMessagesFromPersistedRunEvents(events, result);

  assert.deepEqual(
    messages.map((message) => [message.role, message.content]),
    [
      ['user', 'first request'],
      ['assistant', 'first answer'],
      ['user', 'second request'],
      ['assistant', 'second progress. second answer'],
    ],
  );
  assert.equal(messages.some((message) => message.content.includes('DAG observation')), false);
  assert.equal(messages.some((message) => message.content.includes('NO_CHANGE')), false);
  assert.equal(messages.some((message) => message.content.includes('{"nodes"')), false);
});

test('persisted dynamic DAG hydration merges approved review resume into the original assistant turn', async () => {
  const {
    chatMessagesFromPersistedRunEvents,
    finishedRunResultFromEvents,
  } = await importTypeScriptModule('../src/persistedChat.ts', [
    '../src/persistedChat.ts',
    '../src/chatTimeline.ts',
    '../src/api.ts',
    '../src/agentScope.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const reviewDag = {
    dag_id: 'dag_review_restore',
    task_id: 'task_dynamic_review',
    version: 1,
    status: 'review_required',
    nodes: [],
    edges: [],
  };
  const completedDag = {
    ...reviewDag,
    version: 2,
    status: 'completed',
  };
  const reviewState = {
    run_id: 'task_dynamic_review',
    kind: 'dynamic_dag',
    status: 'awaiting_review',
    input_message_count: 1,
    user_request: 'run the reviewed dag',
    dag: reviewDag,
    pending_review: {
      review_id: 'review_dynamic_1',
      kind: 'initial_dag',
      message: 'Review proposed DAG before execution.',
      proposed_dag: reviewDag,
    },
    internal_messages: [
      { role: 'user', content: 'Task id: task_dynamic_review\nrun the reviewed dag' },
      { role: 'assistant', content: '{"nodes":[]}' },
    ],
  };
  const completedState = {
    ...reviewState,
    status: 'completed',
    dag: completedDag,
    pending_review: null,
  };
  const events = [
    runEventInStream(1, 'stream_review', 1, 'dag.updated', { dag: reviewDag }),
    runEventInStream(2, 'stream_review', 2, 'review.required', {
      review_id: 'review_dynamic_1',
      kind: 'initial_dag',
      message: 'Review proposed DAG before execution.',
      proposed_dag: reviewDag,
    }),
    runEventInStream(3, 'stream_review', 3, 'run.finished', {
      result: { output_text: '', state: reviewState },
    }),
    runEventInStream(4, 'stream_resume', 1, 'dag.updated', { dag: { ...reviewDag, status: 'approved' } }),
    runEventInStream(5, 'stream_resume', 2, 'response.content.delta', { delta: 'final answer' }),
    runEventInStream(6, 'stream_resume', 3, 'dag.updated', { dag: completedDag }),
    runEventInStream(7, 'stream_resume', 4, 'run.finished', {
      result: { output_text: 'final answer', state: completedState },
    }),
  ];

  const result = finishedRunResultFromEvents(events);
  const messages = chatMessagesFromPersistedRunEvents(events, result);

  assert.deepEqual(messages.map((message) => message.role), ['user', 'assistant']);
  assert.equal(messages[0].content, 'run the reviewed dag');
  assert.equal(messages[1].content, 'final answer');
  assert.equal(messages[1].dagSnapshot.status, 'completed');
  const dagItems = messages[1].timeline.filter((item) => item.type === 'dag');
  assert.equal(dagItems.length, 1);
  assert.equal(dagItems[0].dag.status, 'completed');
  assert.equal(messages[1].content.includes('Review proposed DAG before execution.'), false);
});

test('persisted conversation hydration settles rejected capability review cards', async () => {
  const {
    chatMessagesFromPersistedRunEvents,
    finishedRunResultFromEvents,
  } = await importTypeScriptModule('../src/persistedChat.ts', [
    '../src/persistedChat.ts',
    '../src/chatTimeline.ts',
    '../src/api.ts',
    '../src/agentScope.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const state = {
    run_id: 'run_1',
    kind: 'tool',
    status: 'completed',
    internal_messages: [
      { role: 'user', content: 'read blocked file' },
      { role: 'assistant', content: 'I will read README instead.' },
    ],
  };
  const events = [
    runEvent(1, 'capability.call.started', {
      invocation_id: 'call_1',
      capability_id: 'tool.read_file',
      arguments: { path: '../blocked/secret.txt' },
    }),
    runEvent(2, 'capability.call.completed', {
      invocation_id: 'call_1',
      capability_id: 'tool.read_file',
      content: "[PENDING_REVIEW] Capability 'tool.read_file' requires human review.",
    }),
    runEvent(3, 'review.required', {
      review_id: 'review_1',
      kind: 'capability_review',
      message: 'Review capability call.',
      capability_call: {
        invocation_id: 'call_1',
        capability_id: 'tool.read_file',
        tool_name: 'tool_read_file',
        arguments: { path: '../blocked/secret.txt' },
      },
    }),
    runEvent(4, 'run.finished', {
      result: {
        output_text: 'I will read README instead.',
        state,
      },
    }),
  ];

  const result = finishedRunResultFromEvents(events);
  const messages = chatMessagesFromPersistedRunEvents(events, result);
  const assistant = messages.find((message) => message.role === 'assistant');
  const capability = assistant.timeline.find((item) => item.type === 'capability');

  assert.ok(capability, 'capability card should be restored');
  assert.equal(capability.result.type, 'capability.call.failed');
  assert.match(capability.result.content, /人工审核已拒绝/);
  assert.equal(shouldCollapseProcessTimeline(assistant, false), true);
});

test('persisted conversation hydration restores interrupted review traces without run finished', async () => {
  const {
    chatMessagesFromPersistedRunEvents,
    finishedRunResultFromEvents,
  } = await importTypeScriptModule('../src/persistedChat.ts', [
    '../src/persistedChat.ts',
    '../src/chatTimeline.ts',
    '../src/api.ts',
    '../src/agentScope.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const events = [
    runEvent(1, 'run.started', { kind: 'tool' }),
    runEvent(2, 'capability.call.started', {
      invocation_id: 'call_1',
      capability_id: 'tool.read_file',
      arguments: { path: '../blocked/secret.txt' },
    }),
    runEvent(3, 'capability.call.completed', {
      invocation_id: 'call_1',
      capability_id: 'tool.read_file',
      content: "[PENDING_REVIEW] Capability 'tool.read_file' requires human review.",
    }),
    runEvent(4, 'review.required', {
      review_id: 'review_1',
      kind: 'capability_review',
      message: 'Review capability call: tool.read_file',
      capability_call: {
        invocation_id: 'call_1',
        capability_id: 'tool.read_file',
        tool_name: 'tool_read_file',
        arguments: { path: '../blocked/secret.txt' },
      },
    }),
  ];

  const result = finishedRunResultFromEvents(events);
  const messages = chatMessagesFromPersistedRunEvents(events, result);
  const assistant = messages.find((message) => message.role === 'assistant');

  assert.equal(result, null);
  assert.ok(assistant, 'interrupted review run should hydrate a visible assistant turn');
  assert.equal(assistant.content, 'Review capability call: tool.read_file');
  assert.deepEqual(
    assistant.timeline.map((item) => item.type),
    ['capability', 'text'],
  );
});

test('persisted conversation replay reuses api stream event parsing', async () => {
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const persistedChatSource = await readFile(new URL('../src/persistedChat.ts', import.meta.url), 'utf8');

  assert.match(apiSource, /export function dispatchStreamEnvelope\(/);
  assert.match(apiSource, /dispatchStreamEnvelope\(event, handlers, seenTraceIds\);/);
  assert.match(persistedChatSource, /import \{[\s\S]*dispatchStreamEnvelope[\s\S]*\} from '\.\/api';/);
  assert.match(persistedChatSource, /dispatchStreamEnvelope\(envelope, \{/);
  assert.doesNotMatch(persistedChatSource, /function capabilityStartedEvent/);
  assert.doesNotMatch(persistedChatSource, /function capabilityFinishedEvent/);
  assert.doesNotMatch(persistedChatSource, /function capabilityContext/);
});

test('chat header labels standalone and project conversations', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const chatWorkspaceSource = appSource.match(/function ChatWorkspace[\s\S]*?\nfunction DesignEmptyConversation/)?.[0] ?? '';

  assert.ok(chatWorkspaceSource, 'ChatWorkspace function should exist');
  assert.match(chatWorkspaceSource, /projectName && conversationTitle[\s\S]*`\$\{projectName\} \/ \$\{conversationTitle\}`/);
  assert.match(chatWorkspaceSource, /conversationTitle[\s\S]*conversationTitle[\s\S]*: 'local session'/);
});

test('chat send creates a standalone conversation when none is selected', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const runStreamSource = appSource.match(/const runStream = async[\s\S]*?\n  const stopStream/)?.[0] ?? '';

  assert.ok(runStreamSource, 'runStream function should exist');
  assert.match(appSource, /const ensureChatConversation = async \(prompt: string, runTarget: ChatTarget\): Promise<ApiConversation \| null> => \{/);
  assert.match(appSource, /await createConversation\(\{[\s\S]*title: conversationTitleFromPrompt\(prompt\)/);
  assert.match(appSource, /setConversations\(\(items\) => \[conversation, \.\.\.items\]\);[\s\S]*setSelectedConversationId\(conversation\.id\);/);
  assert.match(runStreamSource, /const conversation = await ensureChatConversation\(prompt, target\);[\s\S]*if \(!conversation\) return;/);
  assert.match(runStreamSource, /const conversationContext = \{ projectId: conversation\.project_id, conversationId: conversation\.id \};/);
  assert.doesNotMatch(runStreamSource, /请先新建会话/);
});

test('chat DAG target creates a dynamic DAG conversation context before streaming', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const ensureConversationSource = appSource.match(/const ensureChatConversation = async[\s\S]*?\n  const runStream/)?.[0] ?? '';
  const runStreamSource = appSource.match(/const runStream = async[\s\S]*?\n  const stopStream/)?.[0] ?? '';

  assert.ok(ensureConversationSource, 'ensureChatConversation function should exist');
  assert.match(ensureConversationSource, /if \(runTarget === 'dag'\) \{/);
  assert.match(ensureConversationSource, /ensureOrchestrationContext\(\s*'dynamic_dag'/);
  assert.match(ensureConversationSource, /targetProjectId: chatSub === 'projects' \? selectedProjectId : null/);
  assert.match(runStreamSource, /const conversation = await ensureChatConversation\(prompt, target\);/);
});

test('chat history treats dynamic DAG conversations as restorable chat surface sessions', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.match(appSource, /function isChatSurfaceConversation\(conversation: ApiConversation\): boolean \{[\s\S]*conversation\.kind === 'chat' \|\| conversation\.kind === 'dynamic_dag'/);
  assert.match(sidebarSource, /const standaloneConversationCount = conversations\.filter\(\(conversation\) => \([\s\S]*isChatSurfaceConversation\(conversation\) && !conversation\.project_id/);
  assert.match(sidebarSource, /const visibleConversations = conversations\.filter\(\(conversation\) => isChatSurfaceConversation\(conversation\) && !conversation\.project_id && matchesSearchQuery/);
  assert.match(sidebarSource, /if \(!isChatSurfaceConversation\(conversation\)\) return;/);
  assert.match(appSource, /const selectedChatSurfaceConversation = \(\s*selectedConversation && isChatSurfaceConversation\(selectedConversation\)/);
});

test('conversation subnav count excludes project conversations', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.match(sidebarSource, /const standaloneConversationCount = conversations\.filter\(\(conversation\) => \([\s\S]*isChatSurfaceConversation\(conversation\) && !conversation\.project_id[\s\S]*\)\)\.length;/);
  assert.match(sidebarSource, /\{ key: 'conversations' as const, label: '会话', icon: <MessagesSquare size=\{16\} \/>\, count: standaloneConversationCount \}/);
  assert.doesNotMatch(sidebarSource, /\{ key: 'conversations' as const, label: '会话', icon: <MessagesSquare size=\{16\} \/>\, count: conversations\.length \}/);
});

test('project search matches child conversation titles', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.match(sidebarSource, /function projectConversationMatchesSearch\(conversation: ApiConversation, query: string\)/);
  assert.match(sidebarSource, /const visibleProjects = projects\.filter\(\(project\) => \{[\s\S]*projectConversationsByProjectId\.get\(project\.id\) \?\? \[\][\s\S]*projectConversationMatchesSearch\(conversation, normalizedHistoryQuery\)/);
  assert.match(sidebarSource, /const displayedProjectConversations = normalizedHistoryQuery[\s\S]*projectConversations\.filter\(\(conversation\) => projectConversationMatchesSearch\(conversation, normalizedHistoryQuery\)\)[\s\S]*: projectConversations;/);
});

test('project conversation deletion uses the project-scoped route', async () => {
  const { deleteProjectConversation } = await importTypeScriptModule('../src/api.ts', [
    '../src/agentScope.ts',
    '../src/api.ts',
    '../src/dagArtifacts.ts',
    '../src/streamProtocol.ts',
  ]);
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return {
      ok: true,
      body: null,
      json: async () => ({}),
      text: async () => '',
    };
  };

  try {
    await deleteProjectConversation('proj_1', 'conv_1');
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls[0].url, '/api/projects/proj_1/conversations/conv_1');
  assert.equal(calls[0].init.method, 'DELETE');
  const deleteSource = appSource.match(/const confirmConversationDelete = async \(\) => \{[\s\S]*?\n  \};/)?.[0] ?? '';
  const deleteHelperSource = appSource.match(/async function deleteConversationOnce\(conversation: ApiConversation\): Promise<void> \{[\s\S]*?\n\}/)?.[0] ?? '';
  assert.match(deleteSource, /await deleteConversationWithActiveRetry\(conversation,\s*\{ attempts: 8 \}\);/);
  assert.match(deleteHelperSource, /conversation\.project_id[\s\S]*deleteProjectConversation\(conversation\.project_id, conversation\.id\)[\s\S]*deleteConversation\(conversation\.id\)/);
});

test('project management uses custom dialogs and standalone conversation list', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.doesNotMatch(appSource, /window\.prompt/);
  assert.doesNotMatch(appSource, /window\.confirm/);
  assert.match(apiSource, /export async function updateProject/);
  assert.match(apiSource, /export async function deleteProject/);
  assert.match(appSource, /const \[projectCreateOpen, setProjectCreateOpen\] = useState\(false\);/);
  assert.match(appSource, /const \[projectDeleteTargetId, setProjectDeleteTargetId\] = useState\(''\);/);
  assert.match(appSource, /const projectDeleteTarget = useMemo\([\s\S]*projects\.find\(\(project\) => project\.id === projectDeleteTargetId\) \?\? null/);
  assert.match(appSource, /const requestProjectDelete = \(projectId\?: string\) => \{[\s\S]*const targetProjectId = projectId \?\? selectedProject\?\.id;[\s\S]*if \(!targetProjectId\) return;[\s\S]*setProjectDeleteTargetId\(targetProjectId\);[\s\S]*setProjectDeleteOpen\(true\);/);
  assert.match(appSource, /const \[conversationDeleteTargetId, setConversationDeleteTargetId\] = useState\(''\);/);
  assert.match(appSource, /function isConversationActiveError\(value: unknown\): boolean/);
  assert.match(appSource, /async function deleteConversationWithActiveRetry\(\s*conversation: ApiConversation,\s*options: \{ attempts\?: number \} = \{\},\s*\): Promise<void>/);
  assert.match(appSource, /const attempts = Math\.max\(1, options\.attempts \?\? 2\);[\s\S]*if \(!isConversationActiveError\(exc\) \|\| attempt === attempts\) throw exc;[\s\S]*await delay\(350\);/);
  assert.match(appSource, /function ProjectCreateDialog/);
  assert.match(appSource, /function ConversationDeleteDialog/);
  assert.match(appSource, /conversation\.project_id[\s\S]*项目会话只会删除会话记录和运行历史，项目目录会保留。[\s\S]*会话记录和该会话工作目录会同步删除。/);
  assert.match(appSource, /function ProjectEditDialog/);
  assert.match(appSource, /function ProjectDeleteDialog/);
  assert.match(appSource, /projectDeleteOpen && projectDeleteTarget/);
  assert.match(appSource, /const visibleConversations = conversations\.filter\(\(conversation\) => isChatSurfaceConversation\(conversation\) && !conversation\.project_id && matchesSearchQuery/);
});

test('conversation deletion is not blocked by local streaming state', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const deleteRequestSource = appSource.match(/const deleteConversationFromSidebar = async \(conversationId: string\) => \{[\s\S]*?\n  \};/)?.[0] ?? '';
  const deleteConfirmSource = appSource.match(/const confirmConversationDelete = async \(\) => \{[\s\S]*?\n  \};/)?.[0] ?? '';

  assert.ok(deleteRequestSource, 'deleteConversationFromSidebar should exist');
  assert.ok(deleteConfirmSource, 'confirmConversationDelete should exist');
  assert.doesNotMatch(deleteRequestSource, /if \(streaming\) return;/);
  assert.doesNotMatch(deleteConfirmSource, /if \(streaming \|\| !conversationDeleteTarget\) return;/);
  assert.match(deleteConfirmSource, /if \(!conversationDeleteTarget\) return;/);
  assert.match(deleteConfirmSource, /if \(streaming && conversation\.id === selectedConversationId\) \{[\s\S]*stopStream\(\);[\s\S]*\}/);
  assert.match(appSource, /await deleteConversationWithActiveRetry\(conversation,\s*\{ attempts: 8 \}\);/);
});

test('project sidebar is an expandable project and conversation tree', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.match(sidebarSource, /const \[expandedProjectIds, setExpandedProjectIds\] = useState<Set<string>>/);
  assert.match(sidebarSource, /const toggleProjectExpansion = \(projectId: string\)/);
  assert.match(sidebarSource, /onDeleteProject: \(projectId: string\) => void;/);
  assert.match(sidebarSource, /className="sidebar-project-tree-row"/);
  assert.match(sidebarSource, /className=\{conversation\.id === selectedConversationId \? 'sidebar-project-conversation-row active' : 'sidebar-project-conversation-row'\}/);
  const projectConversationRowsSource = sidebarSource.match(/displayedProjectConversations\.length \? displayedProjectConversations\.map\(\(conversation\) => \([\s\S]*?\)\) : \(/)?.[0] ?? '';
  assert.ok(projectConversationRowsSource, 'project conversation rows should exist');
  assert.match(projectConversationRowsSource, /<MessageSquare size=\{15\} \/>/);
  assert.match(projectConversationRowsSource, /<strong>\{conversation\.title\}<\/strong>/);
  assert.doesNotMatch(projectConversationRowsSource, /<em>\{conversation\.status\}<\/em>/);
  assert.match(sidebarSource, /projectConversationsByProjectId/);
  assert.match(sidebarSource, /onSelectProject\(project\.id\);[\s\S]*toggleProjectExpansion\(project\.id\);/);
  assert.match(sidebarSource, /className=\{project\.id === selectedProjectId && !selectedConversationId \? 'sidebar-project-tree-main active' : 'sidebar-project-tree-main'\}/);
  assert.doesNotMatch(sidebarSource, /className=\{project\.id === selectedProjectId && !selectedConversationId \? 'active sidebar-project-select' : 'sidebar-project-select'\}/);
  assert.match(sidebarSource, /className="sidebar-project-delete"[\s\S]*onClick=\{\(\) => onDeleteProject\(project\.id\)\}[\s\S]*title="删除项目"/);
  assert.doesNotMatch(sidebarSource, /className="sidebar-project-toggle"/);
  assert.match(sidebarSource, /onSelectConversation\(conversation\.id\)/);
  assert.match(css, /\.sidebar-history-list\s*\{[^}]*max-height:\s*min\(360px,\s*42vh\);[^}]*overflow-x:\s*hidden;[^}]*overflow-y:\s*auto;/s);
  assert.doesNotMatch(css, /\.sidebar-history-list\s*\{[^}]*border:\s*1px/s);
  assert.match(css, /\.sidebar-project-tree-row\s*\{/);
  assert.match(css, /\.sidebar-project-tree-main,\s*\.sidebar-project-conversation-row\s*\{[^}]*border:\s*1px solid #edf0f4;[^}]*border-radius:\s*9px;[^}]*background:\s*#fff;[^}]*overflow:\s*hidden;/s);
  assert.match(css, /\.sidebar-project-conversation-row > button:first-child svg\s*\{[^}]*flex:\s*0 0 auto;/s);
  assert.match(css, /\.sidebar-project-delete\s*\{[^}]*border:\s*0;[^}]*border-radius:\s*0;/s);
  assert.doesNotMatch(css, /\.sidebar-project-delete\s*\{[^}]*border-left:/s);
  assert.match(css, /\.sidebar-project-delete\s*\{[^}]*opacity:\s*0;[^}]*pointer-events:\s*none;[^}]*transition:\s*opacity 120ms ease;/s);
  assert.match(css, /\.sidebar-project-tree-main:hover \.sidebar-project-delete\s*\{[^}]*opacity:\s*1;[^}]*pointer-events:\s*auto;/s);
  assert.doesNotMatch(css, /\.sidebar-project-tree-main(?:\.active|:focus-within) \.sidebar-project-delete|\.sidebar-project-delete:focus-visible/s);
  assert.match(css, /\.sidebar-project-delete:hover\s*\{[^}]*color:\s*#9f2f2a;/s);
  assert.doesNotMatch(css, /\.sidebar-project-toggle\b/);
});

test('project conversation creation is a row action with a detail shortcut', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const sidebarSource = appSource.match(/function WorkspaceSidebar[\s\S]*?\nfunction DesignWorkspacePlaceholder/)?.[0] ?? '';
  const projectDetailSource = appSource.match(/function ProjectDetailWorkspace[\s\S]*?\nfunction ProjectFileManager/)?.[0] ?? '';

  assert.ok(sidebarSource, 'WorkspaceSidebar function should exist');
  assert.ok(projectDetailSource, 'ProjectDetailWorkspace function should exist');
  assert.match(appSource, /const createProjectConversationFromProject = async \(projectId: string\)/);
  assert.match(appSource, /onNewProjectConversation=\{\(projectId\) => void createProjectConversationFromProject\(projectId\)\}/);
  assert.match(sidebarSource, /onNewProjectConversation: \(projectId: string\) => void;/);
  assert.doesNotMatch(sidebarSource, /className="sidebar-project-create-conversation"/);
  assert.match(sidebarSource, /className="sidebar-project-conversation-tree"[\s\S]*className="sidebar-project-create-conversation-row"[\s\S]*onClick=\{\(\) => onNewProjectConversation\(project\.id\)\}[\s\S]*title="新建会话"[\s\S]*<Plus size=\{12\} \/>[\s\S]*<span>新建会话<\/span>[\s\S]*displayedProjectConversations\.length/);
  assert.doesNotMatch(sidebarSource, /className="sidebar-project-new-chat"/);
  assert.match(projectDetailSource, /onNewConversation: \(\) => void;/);
  assert.match(projectDetailSource, /<button className="secondary-button compact-button" onClick=\{onNewConversation\} type="button">[\s\S]*<Plus size=\{14\} \/>[\s\S]*新建会话/);
  assert.match(css, /\.sidebar-project-create-conversation-row\s*\{[^}]*display:\s*flex;[^}]*align-items:\s*center;[^}]*gap:\s*7px;[^}]*border:\s*1px solid #e2e4ea;/s);
  assert.match(css, /\.sidebar-history-list \.sidebar-project-create-conversation-row\s*\{[^}]*flex-direction:\s*row;[^}]*border-color:\s*#e2e4ea;/s);
  assert.match(css, /\.sidebar-project-create-conversation-row:hover\s*\{[^}]*color:\s*#5b5bd6;/s);
  assert.doesNotMatch(css, /\.sidebar-project-new-chat\s*\{/);
});

test('project detail workspace manages files with tree and preview', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const typesSource = await readFile(new URL('../src/types.ts', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');

  assert.match(typesSource, /export interface ProjectFileItem/);
  assert.match(typesSource, /children\?: ProjectFileItem\[\];/);
  assert.match(typesSource, /export interface RunArtifactFile[\s\S]*version\?: string \| null;/);
  assert.match(typesSource, /export interface ProjectFileItem[\s\S]*version\?: string \| null;/);
  assert.match(typesSource, /export interface ProjectFileItem[\s\S]*onlyoffice_config_url\?: string \| null;/);
  assert.match(typesSource, /export interface ProjectFilesResponse[\s\S]*tree\?: ProjectFileItem\[\];/);
  assert.match(typesSource, /export interface ProjectFilePreview/);
  assert.match(apiSource, /export async function listProjectFiles\(projectId: string, path = '', options: \{ tree\?: boolean \} = \{\}\)/);
  assert.match(apiSource, /if \(options\.tree\) params\.set\('tree', 'true'\);/);
  assert.match(apiSource, /export async function uploadProjectFiles/);
  assert.match(apiSource, /export async function createProjectFolder/);
  assert.match(apiSource, /export async function renameProjectFile/);
  assert.match(apiSource, /export async function deleteProjectFile/);
  assert.match(apiSource, /export async function previewProjectFile/);
  assert.match(apiSource, /export function projectFileDownloadUrl/);
  assert.match(apiSource, /function normalizeProjectFileUrls[\s\S]*children: file\.children\?\.map\(normalizeProjectFileUrls\)/);
  assert.match(appSource, /function ProjectDetailWorkspace/);
  assert.match(appSource, /function ProjectFileManager/);
  assert.match(appSource, /function ProjectFileTreeNode/);
  assert.match(appSource, /function ProjectFilePreviewPane/);
  assert.match(appSource, /function projectFilePreviewArtifactItem/);
  assert.match(appSource, /const projectFilesRequestRef = useRef\(0\);/);
  assert.match(appSource, /const projectFilePreviewRequestRef = useRef\(0\);/);
  const projectSelectionResetSource = appSource.match(/useEffect\(\(\) => \{\n    (?:projectFilesRequestRef\.current \+= 1;\n    )?projectFilePreviewRequestRef\.current \+= 1;[\s\S]*?\n  \}, \[selectedProjectId\]\);/)?.[0] ?? '';
  assert.doesNotMatch(projectSelectionResetSource, /projectFilesRequestRef\.current \+= 1;/);
  assert.match(projectSelectionResetSource, /setProjectFiles\(\[\]\);/);
  assert.match(projectSelectionResetSource, /setProjectFilesError\(null\);/);
  assert.match(appSource, /const requestId = projectFilesRequestRef\.current \+ 1;[\s\S]*projectFilesRequestRef\.current = requestId;[\s\S]*if \(projectFilesRequestRef\.current !== requestId\) return;/);
  assert.match(appSource, /listProjectFiles\(projectId, '', \{ tree: true \}\)/);
  assert.match(appSource, /setProjectFiles\(payload\.tree \?\? payload\.files\)/);
  assert.match(appSource, /const requestId = projectFilePreviewRequestRef\.current \+ 1;[\s\S]*projectFilePreviewRequestRef\.current = requestId;[\s\S]*if \(projectFilePreviewRequestRef\.current !== requestId\) return;/);
  assert.match(appSource, /<ProjectChatWorkspace[\s\S]*project=\{selectedProject\}[\s\S]*files=\{projectFiles\}/);
  assert.match(appSource, /chatSub === 'projects' \? \([\s\S]*<ProjectChatWorkspace[\s\S]*chatProps=\{chatWorkspaceProps\}/);
  assert.match(appSource, /const refreshProjectFilesView = useCallback\(\(\) => \{[\s\S]*void refreshProjectFiles\(\);[\s\S]*\}, \[refreshProjectFiles\]\);/);
  assert.match(appSource, /onRefresh=\{refreshProjectFilesView\}/);
  assert.doesNotMatch(appSource, /onRefresh=\{\(\) => void refreshProjectFiles\(\)\}/);
  const projectPreviewSource = appSource.match(/function ProjectFilePreviewPane[\s\S]*?\nfunction ProjectFileActionDialog/)?.[0] ?? '';
  assert.match(projectPreviewSource, /projectFilePreviewArtifactItem\(selectedFile\)/);
  assert.match(appSource, /version: file\.version \?\? null/);
  assert.match(projectPreviewSource, /<ArtifactPreview/);
  assert.doesNotMatch(projectPreviewSource, /<ArtifactPreview[\s\S]*showHeader=\{false\}/);
  assert.match(appSource, /showHeader = true[\s\S]*title=\{previewFullscreen \? '关闭全屏' : '全屏预览'\}/);
  assert.doesNotMatch(projectPreviewSource, /<iframe|<img/);
  assert.match(css, /\.project-detail-workspace\s*\{/);
  assert.match(css, /\.project-file-tree\s*\{/);
  assert.match(css, /\.project-file-tree-head\s*\{/);
  assert.match(css, /\.project-file-preview\s*\{/);
});

test('project chat mode keeps project files beside the conversation', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const projectChatSource = appSource.match(/function ProjectChatWorkspace[\s\S]*?\nfunction ProjectDetailWorkspace/)?.[0] ?? '';
  const refreshProjectFilesSource = appSource.match(/const refreshProjectFiles = useCallback\(async \(\) => \{[\s\S]*?\n  \}, \[[^\]]+\]\);/)?.[0] ?? '';

  assert.ok(projectChatSource, 'ProjectChatWorkspace function should exist');
  assert.match(appSource, /activeWorkspace === 'chat' \? \([\s\S]*chatSub === 'projects' \? \([\s\S]*<ProjectChatWorkspace/);
  assert.match(projectChatSource, /const \[projectChatWidth, setProjectChatWidth\] = usePanelWidth\('dagent\.project-chat-width', 500, 420, 680\);/);
  assert.match(projectChatSource, /const projectChatProps: Parameters<typeof ChatWorkspace>\[0\] = \{[\s\S]*\.\.\.chatProps,[\s\S]*artifactPanelOpen: false,[\s\S]*artifacts: \[\]/);
  assert.match(projectChatSource, /<div className="project-chat-body" style=\{\{ '--project-chat-width': `\$\{projectChatWidth\}px` \} as React\.CSSProperties\}>[\s\S]*<ProjectFileContextPane[\s\S]*<div className="project-chat-conversation-pane">[\s\S]*<PanelResizeHandle width=\{projectChatWidth\} onResize=\{setProjectChatWidth\} \/>[\s\S]*<ChatWorkspace \{\.\.\.projectChatProps\} \/>/);
  assert.doesNotMatch(projectChatSource, /project-chat-conversation-head|project-pane-statusbar/);
  assert.doesNotMatch(projectChatSource, /project-chat-statusbar/);
  assert.match(projectChatSource, /function ProjectFileContextPane/);
  assert.match(refreshProjectFilesSource, /if \(!selectedProject \|\| activeWorkspace !== 'chat' \|\| chatSub !== 'projects'\)/);
  assert.doesNotMatch(refreshProjectFilesSource, /selectedChatConversation/);
  assert.match(css, /\.project-pane-statusbar\s*\{[\s\S]*min-height: 52px;/);
  assert.match(css, /\.project-pane-statusbar > \.project-file-tree-actions\s*\{[\s\S]*display: flex;/);
  assert.doesNotMatch(css, /\.project-chat-conversation-head\b/);
  assert.doesNotMatch(css, /\.project-chat-statusbar\s*\{/);
  assert.match(css, /\.project-chat-body\s*\{[\s\S]*grid-template-columns: minmax\(0, 1fr\) var\(--project-chat-width, 500px\);/);
  assert.match(css, /\.project-file-context-pane\s*\{/);
  assert.match(css, /\.project-chat-conversation-pane\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;[^}]*overflow:\s*hidden;[^}]*z-index:\s*2;/s);
  assert.match(css, /\.project-chat-conversation-pane > \.panel-resize-handle\s*\{[^}]*display:\s*block;[^}]*left:\s*-6px;[^}]*width:\s*12px;[^}]*z-index:\s*20;/s);
  assert.doesNotMatch(css, /\.project-chat-conversation-pane\s*\{[^}]*grid-template-rows:\s*auto minmax\(0,\s*1fr\);/s);
  assert.match(css, /\.project-chat-conversation-pane \.chat-workspace\s*\{[\s\S]*--chat-content-max-width: calc\(var\(--project-chat-width, 500px\) - 56px\);[\s\S]*--chat-user-avatar-safe-space: 0px;/);
  assert.match(css, /\.panel-resize-shield\s*\{[^}]*position:\s*fixed;[^}]*inset:\s*0;[^}]*z-index:\s*90;[^}]*cursor:\s*col-resize;/s);
  assert.match(appSource, /function PanelResizeHandle[\s\S]*\{dragging \? <div className="panel-resize-shield" aria-hidden="true" \/> : null\}/);
  assert.match(css, /\.user-row\s*\{[^}]*--user-avatar-offset:\s*var\(--chat-user-avatar-safe-space,\s*43px\);/s);
  assert.match(css, /\.project-file-context-pane \.project-file-browser\s*\{[\s\S]*grid-template-columns: 240px minmax\(0, 1fr\);/);
  assert.match(css, /\.project-file-manager\.tree-collapsed \.project-file-browser\s*\{[\s\S]*grid-template-columns: 0 minmax\(0, 1fr\);/);
  assert.match(css, /\.project-file-tree-main\s*\{[\s\S]*width: 100%;[\s\S]*display: grid;[\s\S]*grid-template-columns: 14px 16px minmax\(0, 1fr\);/);
  const projectFileManagerSource = appSource.match(/function ProjectFileManager[\s\S]*?\nfunction ProjectFilePreviewPane/)?.[0] ?? '';
  const projectTreeNodeSource = appSource.match(/function ProjectFileTreeNode[\s\S]*?\nfunction ProjectFilePreviewPane/)?.[0] ?? '';
  const projectPreviewSource = appSource.match(/function ProjectFilePreviewPane[\s\S]*?\nfunction ProjectFileActionDialog/)?.[0] ?? '';
  assert.match(projectFileManagerSource, /const \[treeCollapsed, setTreeCollapsed\] = useState\(false\);/);
  assert.match(projectFileManagerSource, /const \[uploadMenuOpen, setUploadMenuOpen\] = useState\(false\);/);
  assert.match(projectFileManagerSource, /const \[treeMenu, setTreeMenu\] = useState<\{ file: ProjectFileItem \| null; x: number; y: number \} \| null>\(null\);/);
  assert.match(projectFileManagerSource, /className=\{treeCollapsed \? 'project-file-manager tree-collapsed' : 'project-file-manager'\}/);
  assert.match(projectFileManagerSource, /className="icon-button project-upload-trigger"[\s\S]*title="上传"/);
  assert.match(projectFileManagerSource, /className="project-file-upload-menu"[\s\S]*上传文件[\s\S]*上传目录/);
  assert.match(projectFileManagerSource, /className="project-tree-divider-toggle"[\s\S]*onClick=\{\(\) => setTreeCollapsed\(\(value\) => !value\)\}/);
  assert.doesNotMatch(projectFileManagerSource, /project-tree-restore-button/);
  assert.doesNotMatch(projectFileManagerSource, /title="上传文件"[\s\S]*title="上传目录"[\s\S]*onClick=\{\(\) => setTreeCollapsed\(true\)\}/);
  assert.match(css, /\.project-upload-control\s*\{[\s\S]*position: relative;/);
  assert.match(css, /\.project-file-upload-menu\s*\{[\s\S]*position: absolute;/);
  assert.match(css, /\.project-tree-divider-toggle\s*\{[^}]*left: 240px;[^}]*top: 50%;[^}]*box-shadow: none;/s);
  assert.match(css, /\.project-file-manager\.tree-collapsed \.project-tree-divider-toggle\s*\{[\s\S]*left: 0;/);
  assert.match(projectFileManagerSource, /<ProjectFileTreeNode/);
  const openProjectFileTreeMenuSource = projectFileManagerSource.match(/const openProjectFileTreeMenu = \(event: React\.MouseEvent, file: ProjectFileItem\) => \{[\s\S]*?\n  \};/)?.[0] ?? '';
  assert.ok(openProjectFileTreeMenuSource, 'tree context menu handler should exist');
  assert.doesNotMatch(openProjectFileTreeMenuSource, /onFileSelect\(file\)/);
  assert.match(projectFileManagerSource, /const openProjectFileRootMenu = \(event: React\.MouseEvent\) => \{[\s\S]*setTreeMenu\(\{ file: null, x: event\.clientX, y: event\.clientY \}\);[\s\S]*\n  \};/);
  assert.match(projectFileManagerSource, /<div className="project-file-tree-list" onContextMenu=\{openProjectFileRootMenu\}>/);
  assert.match(projectFileManagerSource, /const contextMenuFile = treeMenu\?\.file \?\? null;/);
  assert.match(projectFileManagerSource, /<div[\s\S]*className="project-file-context-menu"[\s\S]*contextMenuFile\?\.name \?\? '项目根目录'[\s\S]*onCreateFolder\(contextMenuFile \?\? undefined\)[\s\S]*新建文件夹[\s\S]*contextMenuFile \? \([\s\S]*onRenameFile\(contextMenuFile\)[\s\S]*onDownloadFile\(contextMenuFile\)[\s\S]*onDeleteFile\(contextMenuFile\)/);
  assert.doesNotMatch(projectFileManagerSource, /className="icon-button" onClick=\{onCreateFolder\} title="新建文件夹"/);
  assert.match(appSource, /const basePath = projectFileDialog\.file[\s\S]*: '';/);
  assert.match(projectTreeNodeSource, /onContextMenu=\{\(event\) => onContextMenu\(event, file\)\}/);
  assert.match(projectTreeNodeSource, /<span>\{file\.name\}<\/span>/);
  assert.doesNotMatch(projectTreeNodeSource, /project-file-actions|onRenameFile|onDownloadFile|onDeleteFile|projectFileMeta\(file\)/);
  assert.match(projectPreviewSource, /<header className="project-file-preview-head project-pane-statusbar">[\s\S]*selectedFile\.name/);
  assert.doesNotMatch(projectPreviewSource, /<ArtifactPreview[\s\S]*showHeader=\{false\}/);
  assert.match(projectPreviewSource, /<ArtifactPreview[\s\S]*selectedArtifact=\{selectedArtifact\}[\s\S]*onCopy=\{copyProjectPreview\}/);
  assert.doesNotMatch(projectFileManagerSource, /project-file-path/);
});

test('chat stop button aborts the active stream request', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const runStreamSource = appSource.match(/const runStream = async[\s\S]*?\n  const stopStream/)?.[0] ?? '';
  const stopStreamSource = appSource.match(/const stopStream = \(\) => \{[\s\S]*?\n  \};/)?.[0] ?? '';
  const resumeDagSource = appSource.match(/const resumeDag = async[\s\S]*?\n  const confirmDag/)?.[0] ?? '';
  const resumeCapabilitySource = appSource.match(/const confirmCapabilityReview = async[\s\S]*?\n  const newChat/)?.[0] ?? '';

  assert.match(apiSource, /interface StreamRequestOptions \{[\s\S]*signal\?: AbortSignal;[\s\S]*\}/);
  assert.match(apiSource, /streamMessagesTask\([\s\S]*options: StreamRequestOptions = \{\}[\s\S]*signal: options\.signal/);
  assert.match(apiSource, /streamTask\([\s\S]*options: StreamRequestOptions = \{\}[\s\S]*signal: options\.signal/);
  assert.match(apiSource, /resumeDagReview\([\s\S]*options: StreamRequestOptions = \{\}[\s\S]*signal: options\.signal/);
  assert.match(apiSource, /resumeCapabilityReview\([\s\S]*options: StreamRequestOptions = \{\}[\s\S]*signal: options\.signal/);
  assert.match(appSource, /const streamAbortRef = useRef<AbortController \| null>\(null\);/);
  assert.match(appSource, /function beginStreamRequest\(\): AbortSignal/);
  assert.match(appSource, /function clearStreamRequest\(signal: AbortSignal\)/);
  assert.match(appSource, /function isAbortError\(value: unknown\)/);
  assert.match(runStreamSource, /const signal = beginStreamRequest\(\);/);
  assert.match(runStreamSource, /const streamOptions = \{ signal, uploads: uploadsForRequest, conversation: conversationContext \};/);
  assert.match(runStreamSource, /streamTask\([\s\S]*streamOptions\);/);
  assert.match(runStreamSource, /if \(isAbortError\(exc\) \|\| signal\.aborted\) return;/);
  assert.match(runStreamSource, /clearStreamRequest\(signal\);/);
  assert.match(stopStreamSource, /streamAbortRef\.current\?\.abort\(\);/);
  assert.match(resumeDagSource, /const signal = beginStreamRequest\(\);/);
  assert.match(resumeDagSource, /resumeDagReview\([\s\S]*signal,[\s\S]*conversation: activeConversationContext/);
  assert.match(resumeDagSource, /const previousDagReview = dagReview;/);
  assert.match(resumeDagSource, /const previousDagReviewFeedback = dagReviewFeedback;/);
  assert.match(resumeDagSource, /restoreDagReviewAfterAbort\(previousDagReview, previousDagReviewFeedback, previousDag, previousMessages\);/);
  assert.match(resumeCapabilitySource, /const signal = beginStreamRequest\(\);/);
  assert.match(resumeCapabilitySource, /resumeCapabilityReview\([\s\S]*signal,[\s\S]*conversation: activeConversationContext/);
  assert.match(resumeCapabilitySource, /const previousCapabilityReview = capabilityReview;/);
  assert.match(resumeCapabilitySource, /const previousCapabilityReviewFeedback = capabilityReviewFeedback;/);
  assert.match(resumeCapabilitySource, /restoreCapabilityReviewAfterAbort\(previousCapabilityReview, previousCapabilityReviewFeedback, previousMessages\);/);
});

test('rejected review actions display as rejected instead of running', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const typesSource = await readFile(new URL('../src/types.ts', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const resumeDagSource = appSource.match(/const resumeDag = async[\s\S]*?\n  const confirmDag/)?.[0] ?? '';
  const resumeCapabilitySource = appSource.match(/const confirmCapabilityReview = async[\s\S]*?\n  const newChat/)?.[0] ?? '';

  assert.match(typesSource, /\| 'rejected'/);
  assert.match(typesSource, /status: 'queued' \| 'running' \| 'awaiting_review' \| 'completed' \| 'failed' \| 'rejected'/);
  assert.match(resumeDagSource, /status: approved \? 'running' : 'rejected'/);
  assert.match(resumeDagSource, /const rejectedDag = \{ \.\.\.dag, status: 'rejected' as const \};/);
  assert.match(resumeDagSource, /attachDagToLastAssistant\(rejectedDag\);/);
  assert.match(resumeCapabilitySource, /status: approved \? 'running' : 'rejected'/);
  assert.match(resumeCapabilitySource, /appendCapabilityReviewDecisionTimeline\(message\.timeline, capabilityReview, approved, feedback\)/);
  assert.match(css, /\.trace-row\.rejected \.trace-icon/);
  assert.match(css, /\.status-badge\[data-status="rejected"\]/);
});

test('awaiting review traces are not displayed as running', async () => {
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const typesSource = await readFile(new URL('../src/types.ts', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');

  assert.match(typesSource, /status: 'queued' \| 'running' \| 'awaiting_review' \| 'completed' \| 'failed' \| 'rejected'/);
  assert.match(apiSource, /if \(status === 'awaiting_review'\) return 'awaiting_review';/);
  assert.match(css, /\.trace-row\.awaiting_review \.trace-icon/);
  assert.match(css, /\.node-log-row\.awaiting_review summary/);
});

test('dag review dialog uses the compact review surface', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');

  assert.match(appSource, /className="dag-review-eyebrow"/);
  assert.match(appSource, /className="review-stat"/);
  assert.match(appSource, /className="review-feedback-shell"/);
  assert.match(appSource, /驳回并反馈/);
  assert.match(appSource, /通过并继续/);
  assert.match(css, /\.dag-review-eyebrow/);
  assert.match(css, /\.review-feedback-shell textarea/);
  assert.match(css, /\.review-stat/);
});

test('buildWorkbenchArtifacts exposes declarative DAG artifacts and real run files for the preview panel', async () => {
  const {
    artifactPreviewText,
    buildWorkbenchArtifacts,
  } = await importTypeScript('../src/workbenchArtifacts.ts');
  const dag = {
    dag_id: 'dag_review_001',
    task_id: 'task_ui_demo',
    version: 1,
    status: 'review_required',
    nodes: [
      {
        id: 'inspect_project',
        payload: {
          type: 'capability',
          invocation: {
            capability_id: 'tool.grep',
            kind: 'tool',
            arguments: { pattern: 'DAG', path: '.' },
          },
        },
        outputs: ['grep_results'],
      },
    ],
    edges: [],
  };
  const dagArtifacts = {
    dag_overview: {
      id: 'dag_overview',
      paths: ['outputs/dag_overview.md'],
      description: 'Generated orchestration summary',
      metadata: { display_name: 'dag_overview.md' },
    },
  };
  const runFiles = [
    {
      id: 'run:outputs/grep_results.txt',
      artifact_id: 'grep_results',
      source: 'run_file',
      path: 'outputs/grep_results.txt',
      name: 'grep_results.txt',
      media_type: 'text/plain',
      preview_kind: 'text',
      previewable: true,
      size: 24,
      status: 'created',
      error: null,
      preview_url: '/runs/run_tool_1/artifacts/preview?path=outputs%2Fgrep_results.txt',
    },
  ];

  const items = buildWorkbenchArtifacts({ dag, dagArtifacts, runFiles, runId: 'run_tool_1' });

  assert.deepEqual(items.map((item) => ({
    id: item.id,
    name: item.name,
    extension: item.extension,
    source: item.source,
    path: item.path,
    preview: artifactPreviewText(item),
  })), [
    {
      id: 'dag:dag_overview',
      name: 'dag_overview.md',
      extension: 'MD',
      source: 'dag',
      path: 'outputs/dag_overview.md',
      preview: 'Generated orchestration summary\n\nPath: outputs/dag_overview.md',
    },
    {
      id: 'run:outputs/grep_results.txt',
      name: 'grep_results.txt',
      extension: 'TXT',
      source: 'run',
      path: 'outputs/grep_results.txt',
      preview: 'Path: outputs/grep_results.txt',
    },
  ]);
});

test('run artifact preview uses backend manifest files and a dedicated preview component', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');

  assert.match(apiSource, /export async function listRunArtifacts\(runId: string\)/);
  assert.match(apiSource, /export async function previewRunArtifact\(runId: string, path: string\)/);
  assert.match(apiSource, /export function runArtifactDownloadUrl\(runId: string, path: string\)/);
  assert.match(appSource, /const \[runArtifactFiles, setRunArtifactFiles\] = useState<RunArtifactFile\[\]>\(\[\]\);/);
  assert.match(appSource, /const runArtifactRequestRef = useRef\(0\);/);
  assert.match(appSource, /function artifactPreviewCacheKey\(item: WorkbenchArtifactItem\)/);
  assert.match(appSource, /item\.version \?\? item\.size \?\? 'unknown'/);
  const artifactAutoOpenSource = appSource.match(/const artifactsPresentRef = useRef\(false\);[\s\S]*?\}, \[chatArtifacts\.length\]\);/)?.[0] ?? '';
  assert.ok(artifactAutoOpenSource, 'artifact auto-open effect should exist');
  assert.match(artifactAutoOpenSource, /setArtifactPanelOpen\(true\);/);
  const artifactSelectionSource = appSource.match(/useEffect\(\(\) => \{\s*if \(!chatArtifacts\.length\) \{[\s\S]*?\}, \[chatArtifacts\]\);/)?.[0] ?? '';
  assert.ok(artifactSelectionSource, 'artifact selection effect should exist');
  assert.match(artifactSelectionSource, /chatArtifacts\.some\(\(item\) => item\.id === current\) \? current : ''/);
  assert.doesNotMatch(artifactSelectionSource, /chatArtifacts\[0\]\.id/);
  assert.match(appSource, /listRunArtifacts\(activeRunId\)/);
  assert.match(appSource, /runArtifactRequestRef\.current !== requestId/);
  assert.match(appSource, /previewRunArtifact\(selectedArtifact\.runId, selectedArtifact\.path\)/);
  assert.match(appSource, /preview\.run_id !== selectedArtifact\.runId \|\| preview\.path !== selectedArtifact\.path/);
  assert.doesNotMatch(appSource, /runArtifacts: runState\?\.trace\?\.artifacts/);
  assert.doesNotMatch(appSource, /artifactPreviewText/);
  assert.match(appSource, /onArtifactRefresh: refreshRunArtifacts/);
  assert.match(appSource, /function ArtifactPreview\(/);
  assert.match(appSource, /showHeader = true/);
  assert.match(appSource, /const downloadUrl = artifactPreviewDownloadUrl\(selectedArtifact\);/);
  assert.match(appSource, /const \[previewFullscreen, setPreviewFullscreen\] = useState\(false\);/);
  assert.match(appSource, /className="artifact-preview-title"/);
  assert.match(appSource, /\{showHeader \? \([\s\S]*className="artifact-preview-head"[\s\S]*\) : null\}/);
  assert.match(appSource, /title=\{previewFullscreen \? '关闭全屏' : '全屏预览'\}/);
  assert.match(appSource, /data-fullscreen=\{previewFullscreen\}/);
  assert.doesNotMatch(appSource, /\{previewFullscreen \? null : body\}/);
  assert.match(appSource, /function ArtifactPreviewBody\(/);
  assert.doesNotMatch(appSource, /<span>\{selectedArtifact\.meta\}<\/span>/);
  assert.match(appSource, /const onlyOfficeConfigUrl = selectedArtifact\.onlyOfficeConfigUrl \?\? null;/);
  assert.match(appSource, /const onlyOfficePreviewCacheRef = useRef<Map<string, ArtifactPreviewRenderHandle>>\(new Map\(\)\);/);
  assert.match(appSource, /const onlyOfficePreviewCacheHostRef = useRef<HTMLDivElement \| null>\(null\);/);
  assert.match(appSource, /const cachedOnlyOfficePreview = onlyOfficePreviewCacheKey \? onlyOfficePreviewCacheRef\.current\.get\(onlyOfficePreviewCacheKey\) \?\? null : null;/);
  assert.match(appSource, /cachedOnlyOfficePreview\.attach\?\.\(container\);/);
  assert.match(appSource, /handle\.cacheable && onlyOfficePreviewCacheKey/);
  assert.match(appSource, /handle\.detach\?\.\(cacheHost\);/);
  assert.match(appSource, /trimOnlyOfficePreviewCache\(onlyOfficePreviewCacheRef\.current, onlyOfficePreviewCacheKey\);/);
  assert.match(appSource, /className="artifact-browser-preview-cache"[\s\S]*ref=\{onlyOfficePreviewCacheHostRef\}[\s\S]*aria-hidden="true"/);
  assert.match(appSource, /onPreviewRefresh=\{onRefresh\}/);
  assert.match(appSource, /href=\{downloadUrl \?\? undefined\}[\s\S]*download=\{selectedArtifact\.name\}[\s\S]*title="下载"/);
  assert.doesNotMatch(appSource, /const downloadUrl = selectedArtifact\.runId && selectedArtifact\.path/);
  assert.match(appSource, /signal:\s*controller\.signal/);
  assert.match(appSource, /onlyOfficeConfigUrl,\s*fileName: selectedArtifact\.name,\s*signal: controller\.signal,\s*onSaved: onPreviewRefresh/s);
  assert.match(appSource, /\[downloadUrl, onPreviewRefresh, onlyOfficeConfigUrl, selectedArtifact\.name, selectedArtifact\.previewKind, selectedArtifact\.version\]/);
  assert.match(appSource, /catch \(exc\) \{[\s\S]*if \(isAbortError\(exc\) \|\| !downloadUrl\) throw exc;[\s\S]*await renderBuiltInBrowserArtifactPreview\(\);/);
  assert.match(appSource, /async function artifactResponseError\(response: Response\): Promise<string> \{[\s\S]*const payload = await response\.clone\(\)\.json\(\);[\s\S]*typeof payload\.detail === 'string'[\s\S]*JSON\.stringify\(payload\.detail\)/);
  assert.match(appSource, /selectedArtifact\.previewKind === 'markdown'/);
  assert.match(appSource, /<ReactMarkdown remarkPlugins=\{\[remarkGfm\]\}>\{preview\.content\}<\/ReactMarkdown>/);
  assert.match(css, /\.artifact-preview-head\s*\{[^}]*min-height:\s*34px;[^}]*padding:\s*6px 10px;/s);
  assert.match(css, /\.artifact-preview-title\s*\{[^}]*margin-right:\s*auto;/s);
  assert.match(css, /\.artifact-preview-fullscreen\s*\{[^}]*position:\s*fixed;[^}]*inset:\s*0;/s);
  assert.match(css, /\.artifact-browser-preview-host:has\(\.artifact-onlyoffice-preview\)\s*\{[^}]*padding:\s*0;[^}]*overflow:\s*hidden;/s);
  assert.match(css, /\.artifact-browser-preview-cache\s*\{[^}]*position:\s*absolute;[^}]*pointer-events:\s*none;/s);
  assert.match(css, /\.artifact-onlyoffice-preview\s*\{[^}]*height:\s*100%;[^}]*min-height:\s*0;[^}]*border:\s*0;[^}]*border-radius:\s*0;/s);
});

test('artifact drawer file list collapses independently from the preview', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const artifactPanelSource = appSource.match(/function ArtifactPanel[\s\S]*?\nfunction ArtifactTree/)?.[0] ?? '';
  const artifactTreeSource = appSource.match(/function ArtifactTree[\s\S]*?\nfunction ArtifactPreview/)?.[0] ?? '';
  const artifactActionsSource = artifactPanelSource.match(/<div className="artifact-drawer-actions">[\s\S]*?<\/div>/)?.[0] ?? '';

  assert.ok(artifactPanelSource, 'ArtifactPanel function should exist');
  assert.ok(artifactTreeSource, 'ArtifactTree function should exist');
  assert.match(artifactPanelSource, /const \[artifactFilesExpanded, setArtifactFilesExpanded\] = useState\(true\);/);
  assert.match(artifactPanelSource, /<div className="artifact-drawer-title">[\s\S]*<strong>产物<\/strong>[\s\S]*<span>\{artifacts\.length\}<\/span>[\s\S]*<\/div>/);
  assert.doesNotMatch(artifactPanelSource, /<button\s+className="artifact-drawer-title"/);
  assert.doesNotMatch(artifactActionsSource, /artifact-drawer-tree-toggle/);
  assert.match(artifactActionsSource, /<button className="icon-button" disabled=\{loading\} onClick=\{onRefresh\}/);
  assert.doesNotMatch(appSource, /className="artifact-file-label"/);
  assert.match(artifactPanelSource, /const artifactTree = useMemo\(\(\) => buildWorkbenchArtifactTree\(artifacts\), \[artifacts\]\);/);
  assert.match(artifactPanelSource, /className="artifact-drawer-body"\s*data-tree-expanded=\{artifactFilesExpanded\}/);
  assert.match(artifactPanelSource, /<div className="artifact-tree-pane" data-expanded=\{artifactFilesExpanded\}>[\s\S]*\{artifactFilesExpanded \? \(\s*<div className="artifact-tree-list">[\s\S]*<ArtifactTree/);
  assert.doesNotMatch(artifactPanelSource, /artifact-tree-pane-head/);
  assert.doesNotMatch(artifactPanelSource, /artifact-tree-pane-title/);
  assert.doesNotMatch(artifactPanelSource, /<span>文件<\/span>/);
  assert.doesNotMatch(artifactPanelSource, /artifact-tree-pane-toggle/);
  assert.match(artifactPanelSource, /aria-label=\{artifactFilesExpanded \? '收起目录树' : '展开目录树'\}[\s\S]*className="artifact-tree-divider-toggle"[\s\S]*title=\{artifactFilesExpanded \? '收起目录树' : '展开目录树'\}/);
  assert.match(artifactPanelSource, /\{artifactFilesExpanded \? <ChevronLeft size=\{14\} \/> : <ChevronRight size=\{14\} \/>\}/);
  assert.doesNotMatch(artifactPanelSource, /artifact-tree-rail-toggle/);
  assert.doesNotMatch(artifactPanelSource, /artifact-drawer-tree-toggle/);
  assert.match(artifactPanelSource, /<\/div>\s*\n\s*<button[\s\S]*className="artifact-tree-divider-toggle"[\s\S]*<\/button>\s*\n\s*<ArtifactPreview/);
  assert.match(artifactTreeSource, /className="artifact-tree-folder"/);
  assert.match(artifactTreeSource, /className=\{node\.item\.id === selectedArtifactId \? 'active artifact-tree-file' : 'artifact-tree-file'\}/);
  assert.doesNotMatch(artifactTreeSource, /<span className="artifact-extension">\{node\.item\.extension\}<\/span>/);
  assert.doesNotMatch(artifactTreeSource, /<em>\{node\.item\.meta\}<\/em>/);
  assert.doesNotMatch(appSource, /artifact-file-download/);
  assert.doesNotMatch(appSource, /download=\{artifactFileName\}/);
  assert.doesNotMatch(appSource, /<strong>\{artifact\.name\}<\/strong>\s*<em>\{artifact\.meta\}<\/em>/);
  assert.match(appSource, /function artifactListFileName\(artifact: WorkbenchArtifactItem\): string/);
  assert.match(css, /\.artifact-drawer-title\s*\{[^}]*flex:\s*1 1 auto;/s);
  assert.match(css, /\.artifact-drawer-actions\s*\{[^}]*margin-left:\s*auto;[^}]*display:\s*flex;/s);
  assert.match(css, /\.artifact-drawer-body\s*\{[^}]*display:\s*grid;/s);
  assert.match(css, /\.artifact-drawer-body\[data-tree-expanded="true"\]\s*\{[^}]*grid-template-columns:\s*minmax\(136px,\s*0\.3fr\)\s+minmax\(0,\s*1fr\);/s);
  assert.match(css, /\.artifact-drawer-body\[data-tree-expanded="false"\]\s*\{[^}]*grid-template-columns:\s*0\s+minmax\(0,\s*1fr\);/s);
  assert.match(css, /\.artifact-tree-pane\s*\{[^}]*overflow:\s*hidden;/s);
  assert.doesNotMatch(css, /\.artifact-tree-pane-head\b/);
  assert.doesNotMatch(css, /\.artifact-tree-pane-title\b/);
  assert.doesNotMatch(css, /\.artifact-tree-pane-toggle\b/);
  assert.match(css, /\.artifact-tree-divider-toggle\s*\{[^}]*position:\s*absolute;[^}]*left:\s*max\(136px,\s*23\.077%\);[^}]*width:\s*22px;[^}]*height:\s*44px;[^}]*box-shadow:\s*none;/s);
  assert.match(css, /\.artifact-drawer-body\[data-tree-expanded="false"\]\s+\.artifact-tree-divider-toggle\s*\{[^}]*left:\s*0;[^}]*border-left:\s*0;[^}]*border-radius:\s*0 999px 999px 0;/s);
  assert.match(css, /\.artifact-tree-list\s*\{[^}]*overflow-x:\s*hidden;[^}]*overflow-y:\s*auto;/s);
  assert.doesNotMatch(css, /\.artifact-tree-rail-toggle\b/);
  assert.match(css, /--artifact-tree-indent:\s*calc\(var\(--artifact-tree-depth,\s*0\)\s*\*\s*12px\);/);
  assert.doesNotMatch(css, /--artifact-tree-indent:\s*calc\(var\(--artifact-tree-depth,\s*0\)\s*\*\s*16px\);/);
  assert.match(css, /\.artifact-tree-file\s*\{[^}]*min-height:\s*30px;/s);
  assert.match(css, /\.artifact-tree-file\s*\{[^}]*width:\s*100%;[^}]*min-width:\s*0;/s);
  assert.match(css, /\.artifact-tree-file\s*\{[^}]*color:\s*#4c535f;[^}]*font-size:\s*12px;[^}]*font-weight:\s*500;/s);
  assert.match(css, /\.artifact-tree-file-name\s*\{[^}]*overflow:\s*hidden;[^}]*color:\s*inherit;[^}]*font-family:\s*inherit;[^}]*font-size:\s*inherit;[^}]*font-weight:\s*inherit;[^}]*text-overflow:\s*ellipsis;/s);
  assert.doesNotMatch(css, /\.artifact-tree-file-name\s*\{[^}]*SFMono/s);
});

test('workbench artifacts preserve uploaded folder paths as a directory tree', async () => {
  const {
    artifactFolderIdsForPath,
    buildWorkbenchArtifactTree,
    buildWorkbenchArtifacts,
  } = await importTypeScript('../src/workbenchArtifacts.ts');

  assert.equal(typeof artifactFolderIdsForPath, 'function');
  assert.equal(typeof buildWorkbenchArtifactTree, 'function');

  const items = buildWorkbenchArtifacts({
    runId: 'run_upload_1',
    runFiles: [
      {
        id: 'run:uploads/project/src/index.ts',
        artifact_id: null,
        source: 'run_file',
        path: 'uploads/project/src/index.ts',
        name: 'index.ts',
        media_type: 'text/x-typescript',
        preview_kind: 'code',
        previewable: true,
        size: 18,
        version: '18:100',
        status: 'created',
        error: null,
      },
      {
        id: 'run:uploads/project/README.md',
        artifact_id: null,
        source: 'run_file',
        path: 'uploads/project/README.md',
        name: 'README.md',
        media_type: 'text/markdown',
        preview_kind: 'markdown',
        previewable: true,
        size: 12,
        status: 'created',
        error: null,
      },
      {
        id: 'run:outputs/report.pdf',
        artifact_id: null,
        source: 'run_file',
        path: 'outputs/report.pdf',
        name: 'report.pdf',
        media_type: 'application/pdf',
        preview_kind: 'pdf',
        previewable: true,
        size: 90,
        status: 'created',
        error: null,
      },
    ],
  });
  const tree = buildWorkbenchArtifactTree(items);

  assert.equal(items.find((item) => item.path === 'uploads/project/src/index.ts')?.version, '18:100');
  assert.deepEqual(artifactFolderIdsForPath('uploads/project/src/index.ts'), [
    'folder:uploads',
    'folder:uploads/project',
    'folder:uploads/project/src',
  ]);
  assert.deepEqual(tree.map((node) => ({
    kind: node.kind,
    name: node.name,
    fileCount: node.kind === 'folder' ? node.fileCount : 1,
    children: node.kind === 'folder' ? node.children.map((child) => ({
      kind: child.kind,
      name: child.name,
      fileCount: child.kind === 'folder' ? child.fileCount : 1,
    })) : [],
  })), [
    {
      kind: 'folder',
      name: 'outputs',
      fileCount: 1,
      children: [{ kind: 'file', name: 'report.pdf', fileCount: 1 }],
    },
    {
      kind: 'folder',
      name: 'uploads',
      fileCount: 2,
      children: [{ kind: 'folder', name: 'project', fileCount: 2 }],
    },
  ]);
});

test('artifact fullscreen preview reuses one preview instance instead of remounting browser previews', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const previewSource = appSource.match(/function ArtifactPreview\([\s\S]*?\nfunction ArtifactPreviewBody/)?.[0] ?? '';

  assert.ok(previewSource, 'ArtifactPreview function should exist');
  assert.match(previewSource, /data-fullscreen=\{previewFullscreen\}/);
  assert.match(previewSource, /className="artifact-preview-fullscreen-shell"/);
  assert.match(previewSource, /<ArtifactPreviewBody[\s\S]*selectedArtifact=\{selectedArtifact\}/);
  assert.doesNotMatch(previewSource, /\{previewFullscreen \? null : body\}/);
  assert.doesNotMatch(previewSource, /artifact-preview-fullscreen-body/);
  assert.equal((previewSource.match(/<ArtifactPreviewBody/g) ?? []).length, 1);
});

test('artifactPreview module centralizes preview routing for future provider swaps', async () => {
  const previewSource = await readFile(new URL('../src/artifactPreview.ts', import.meta.url), 'utf8');

  assert.equal(artifactPreviewMode('markdown'), 'text');
  assert.equal(artifactPreviewMode('code'), 'text');
  assert.equal(artifactPreviewMode('text'), 'text');
  assert.equal(artifactPreviewMode('pdf'), 'browser');
  assert.equal(artifactPreviewMode('docx'), 'browser');
  assert.equal(artifactPreviewMode('xlsx'), 'browser');
  assert.equal(artifactPreviewMode('pptx'), 'browser');
  assert.equal(artifactPreviewMode(null), 'unsupported');
  assert.match(previewSource, /pptx-react-viewer/);
  assert.match(previewSource, /function renderOnlyOfficePreview/);
  assert.match(previewSource, /DocsAPI\.DocEditor/);
  assert.match(previewSource, /loadOnlyOfficeScript/);
  assert.match(previewSource, /const onlyOfficeLoadedScriptUrls = new Set<string>\(\);/);
  assert.match(previewSource, /cacheable\?: boolean;/);
  assert.match(previewSource, /attach\?: \(container: HTMLElement\) => void;/);
  assert.match(previewSource, /detach\?: \(container: HTMLElement\) => void;/);
  assert.match(previewSource, /cacheable: isOnlyOfficeViewMode\(payload\.config\)/);
  assert.match(previewSource, /function isOnlyOfficeViewMode/);
  assert.match(previewSource, /onSaved\?: \(\) => void;/);
  assert.doesNotMatch(previewSource, /onDocumentStateChange/);
  assert.match(previewSource, /events: \{[\s\S]*onRequestClose/);
  assert.match(previewSource, /try \{[\s\S]*new window\.DocsAPI\.DocEditor\(editorId, payload\.config\)[\s\S]*\} catch \(exc\) \{[\s\S]*root\.remove\(\);/);
  assert.match(previewSource, /if \(request\.kind === 'xlsx'\) return renderXlsxPreview\(container, request\);/);
  assert.match(previewSource, /return renderPptxPreview\(container, request\);/);

  assert.equal(shouldFetchTextArtifactPreview({
    previewKind: 'markdown',
    previewable: true,
    previewUrl: '/runs/run_1/artifacts/preview?path=notes%2Foutput.md',
  }), true);
  assert.equal(shouldFetchTextArtifactPreview({
    previewKind: 'pdf',
    previewable: true,
    downloadUrl: '/runs/run_1/artifacts/download?path=exports%2Freport.pdf',
  }), false);
  assert.equal(artifactPreviewDownloadUrl({
    previewKind: 'xlsx',
    previewable: true,
    downloadUrl: '/runs/run_1/artifacts/download?path=exports%2Fdata.xlsx',
  }), '/runs/run_1/artifacts/download?path=exports%2Fdata.xlsx');
  assert.equal(artifactPreviewDownloadUrl({
    previewKind: 'docx',
    previewable: true,
  }), null);
  assert.equal(artifactPreviewDownloadUrl({
    previewKind: 'pptx',
    previewable: true,
    downloadUrl: '/runs/run_1/artifacts/download?path=exports%2Fdeck.pptx',
  }), '/runs/run_1/artifacts/download?path=exports%2Fdeck.pptx');
});

test('docx artifact preview keeps generated pages compact and unframed', async () => {
  const previewSource = await readFile(new URL('../src/artifactPreview.ts', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');

  assert.match(previewSource, /className:\s*'artifact-docx-document'/);
  assert.match(previewSource, /signal\?:\s*AbortSignal;/);
  assert.match(previewSource, /function throwIfAborted\(signal: AbortSignal \| undefined\)/);
  assert.match(previewSource, /request\.signal\?\.addEventListener\('abort', \(\) => renderTask\.cancel\?\.\(\), \{ once: true \}\);/);
  assert.doesNotMatch(previewSource, /setAttribute\('role', 'tablist'\)/);
  assert.match(previewSource, /const widestColumnCount = rows\.reduce\(\(count, row\) => Math\.max\(count, row\.length\), 0\);/);
  assert.match(previewSource, /previewNotice\(`仅预览前 \$\{maxColumns\} 列，共 \$\{widestColumnCount\} 列。`\)/);
  assert.match(previewSource, /ignoreHeight:\s*true/);
  assert.match(css, /\.artifact-docx-preview\s+\.artifact-docx-document-wrapper\s*\{[^}]*background:\s*transparent;[^}]*padding:\s*0;/s);
  assert.match(css, /\.artifact-docx-preview\s+section\.artifact-docx-document\s*\{[^}]*margin:\s*0 auto 16px;/s);
  assert.match(css, /\.artifact-docx-preview\s+\.artifact-docx-document-wrapper\s*>\s*section\.artifact-docx-document\s*\{[^}]*margin:\s*0 auto 16px !important;[^}]*box-shadow:\s*none !important;/s);
  assert.doesNotMatch(css, /\.artifact-docx-preview\s+section\.artifact-docx-document\s*\{[^}]*box-shadow:/s);
});

test('buildWorkbenchArtifacts maps run file manifests into previewable drawer items', async () => {
  const {
    artifactPreviewText,
    buildWorkbenchArtifacts,
  } = await importTypeScript('../src/workbenchArtifacts.ts');

  const items = buildWorkbenchArtifacts({
    runId: 'run_tool_1',
    runFiles: [
      {
        id: 'run:exports/brief.docx',
        artifact_id: null,
        source: 'run_file',
        path: 'exports/brief.docx',
        name: 'brief.docx',
        media_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        preview_kind: 'docx',
        previewable: true,
        size: 48,
        status: 'created',
        error: null,
        download_url: '/runs/run_tool_1/artifacts/download?path=exports%2Fbrief.docx',
        onlyoffice_config_url: '/runs/run_tool_1/artifacts/onlyoffice/config?path=exports%2Fbrief.docx',
      },
      {
        id: 'run:notes/output.md',
        artifact_id: null,
        source: 'run_file',
        path: 'notes/output.md',
        name: 'output.md',
        media_type: 'text/markdown',
        preview_kind: 'markdown',
        previewable: true,
        size: 13,
        status: 'created',
        error: null,
        preview_url: '/runs/run_tool_1/artifacts/preview?path=notes%2Foutput.md',
      },
      {
        id: 'run:scripts/tool.py',
        artifact_id: null,
        source: 'run_file',
        path: 'scripts/tool.py',
        name: 'tool.py',
        media_type: 'text/x-python',
        preview_kind: 'code',
        previewable: true,
        size: 15,
        status: 'created',
        error: null,
        preview_url: '/runs/run_tool_1/artifacts/preview?path=scripts%2Ftool.py',
      },
    ],
  });

  assert.deepEqual(items.map((item) => ({
    id: item.id,
    name: item.name,
    extension: item.extension,
    meta: item.meta,
    path: item.path,
    previewKind: item.previewKind,
    previewable: item.previewable,
    runId: item.runId,
    downloadUrl: item.downloadUrl,
    onlyOfficeConfigUrl: item.onlyOfficeConfigUrl,
    preview: artifactPreviewText(item),
  })), [
    {
      id: 'run:exports/brief.docx',
      name: 'brief.docx',
      extension: 'DOCX',
      meta: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document · 48 B',
      path: 'exports/brief.docx',
      previewKind: 'docx',
      previewable: true,
      runId: 'run_tool_1',
      downloadUrl: '/runs/run_tool_1/artifacts/download?path=exports%2Fbrief.docx',
      onlyOfficeConfigUrl: '/runs/run_tool_1/artifacts/onlyoffice/config?path=exports%2Fbrief.docx',
      preview: 'Path: exports/brief.docx',
    },
    {
      id: 'run:notes/output.md',
      name: 'output.md',
      extension: 'MD',
      meta: 'text/markdown · 13 B',
      path: 'notes/output.md',
      previewKind: 'markdown',
      previewable: true,
      runId: 'run_tool_1',
      downloadUrl: null,
      onlyOfficeConfigUrl: null,
      preview: 'Path: notes/output.md',
    },
    {
      id: 'run:scripts/tool.py',
      name: 'tool.py',
      extension: 'PY',
      meta: 'text/x-python · 15 B',
      path: 'scripts/tool.py',
      previewKind: 'code',
      previewable: true,
      runId: 'run_tool_1',
      downloadUrl: null,
      onlyOfficeConfigUrl: null,
      preview: 'Path: scripts/tool.py',
    },
  ]);
});

test('buildWorkbenchArtifacts keeps unsupported run files visible but not previewable', async () => {
  const { buildWorkbenchArtifacts } = await importTypeScript('../src/workbenchArtifacts.ts');

  const items = buildWorkbenchArtifacts({
    runId: 'run_tool_1',
    runFiles: [
      {
        id: 'run:exports/archive.bin',
        artifact_id: null,
        source: 'run_file',
        path: 'exports/archive.bin',
        name: 'archive.bin',
        media_type: 'application/octet-stream',
        preview_kind: null,
        previewable: false,
        size: 15,
        status: 'created',
        error: null,
        preview_url: null,
        download_url: '/runs/run_tool_1/artifacts/download?path=exports%2Farchive.bin',
      },
    ],
  });

  assert.deepEqual(items.map((item) => ({
    id: item.id,
    extension: item.extension,
    meta: item.meta,
    previewKind: item.previewKind,
    previewable: item.previewable,
    previewUrl: item.previewUrl,
    downloadUrl: item.downloadUrl,
    onlyOfficeConfigUrl: item.onlyOfficeConfigUrl,
  })), [
    {
      id: 'run:exports/archive.bin',
      extension: 'BIN',
      meta: 'application/octet-stream · 15 B',
      previewKind: undefined,
      previewable: false,
      previewUrl: null,
      downloadUrl: '/runs/run_tool_1/artifacts/download?path=exports%2Farchive.bin',
      onlyOfficeConfigUrl: null,
    },
  ]);
});

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

test('visibleCapabilitiesForPicker keeps enabled capabilities and drops disabled ones', () => {
  const capabilities = [
    { id: 'tool.read_file', kind: 'tool', enabled: true },
    { id: 'tool.write_file', kind: 'tool', enabled: false },
    { id: 'agent.conversation', kind: 'agent', enabled: true },
  ];

  assert.deepEqual(
    visibleCapabilitiesForPicker(capabilities).map((capability) => capability.id),
    ['tool.read_file', 'agent.conversation'],
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
      {
        id: 'read',
        target: 'tool.echo',
        inputs: { path: artifactPathExpr('source') },
        artifact_inputs: ['source'],
        artifact_outputs: ['report'],
      },
      {
        id: 'review',
        target: 'tool.echo',
        inputs: { path: artifactPathExpr('report') },
        artifact_inputs: ['report'],
      },
    ],
    edges: [],
  };

  assert.deepEqual(removeArtifactBinding(spec, 'report'), {
    ...spec,
    artifacts: {
      source: { id: 'source', paths: ['uploads/source.md'] },
    },
    nodes: [
      { ...spec.nodes[0], artifact_outputs: [] },
      { ...spec.nodes[1], inputs: {}, artifact_inputs: [], artifact_outputs: [] },
    ],
  });
});

test('updateArtifactBinding renames artifacts and rewrites node references', () => {
  const spec = {
    id: 'example',
    name: 'Example',
    artifacts: {
      source: { id: 'source', paths: ['uploads/source.md'] },
      report: { id: 'report', paths: ['outputs/report.md'] },
    },
    nodes: [
      {
        id: 'write',
        target: 'tool.write_file',
        inputs: {
          path: artifactPathExpr('report'),
          message: {
            $expr: {
              type: 'format',
              template: 'write {file}',
              values: { file: artifactPathExpr('report') },
            },
          },
        },
        artifact_inputs: ['source'],
        artifact_outputs: ['report'],
        boundary: {
          allowed_paths: [artifactPathExpr('report')],
        },
      },
    ],
    edges: [],
  };

  const next = updateArtifactBinding(spec, 'report', {
    id: 'final_report',
    paths: ['outputs/final.md'],
    description: 'Final report',
    required: true,
  });

  assert.deepEqual(Object.keys(next.artifacts), ['source', 'final_report']);
  assert.deepEqual(next.artifacts.final_report, {
    id: 'final_report',
    paths: ['outputs/final.md'],
    description: 'Final report',
    required: true,
    metadata: {},
  });
  assert.deepEqual(next.nodes[0].artifact_inputs, ['source']);
  assert.deepEqual(next.nodes[0].artifact_outputs, ['final_report']);
  assert.deepEqual(next.nodes[0].inputs.path, artifactPathExpr('final_report'));
  assert.deepEqual(next.nodes[0].inputs.message.$expr.values.file, artifactPathExpr('final_report'));
  assert.deepEqual(next.nodes[0].boundary.allowed_paths, [artifactPathExpr('final_report')]);
});

test('artifactPathExpr builds structured executor artifact value expressions', () => {
  assert.deepEqual(artifactPathExpr('report'), {
    $expr: {
      type: 'artifact',
      artifact_id: 'report',
      field: 'path',
    },
  });
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
  assert.equal(uploadFormFilename(file), 'dataset/data.csv');
});

test('createUploadedFileArtifacts preserves browser folder relative paths', () => {
  const result = createUploadedFileArtifacts(
    [{ name: 'summary.md', webkitRelativePath: 'research/day1/summary.md' }],
    { artifacts: {}, uploadRoot: 'uploads' },
  );

  assert.equal(result.uploads[0].artifact.paths[0], 'uploads/research/day1/summary.md');
  assert.equal(result.uploads[0].artifact.metadata.relative_path, 'research/day1/summary.md');
});

test('upload helpers reject unsafe relative paths instead of rewriting them', () => {
  assert.throws(
    () => uploadFormFilename({ name: 'secret.txt', webkitRelativePath: '../secret.txt' }),
    /cannot contain '\.\.'/,
  );
  assert.throws(
    () => uploadFormFilename({ name: 'secret.txt', webkitRelativePath: '/tmp/secret.txt' }),
    /must be relative/,
  );
  assert.throws(
    () => uploadFormFilename({ name: 'secret.txt', webkitRelativePath: 'C:\\tmp\\secret.txt' }),
    /must be relative/,
  );
  assert.throws(
    () => createUploadedFileArtifacts(
      [{ name: 'secret.txt', webkitRelativePath: 'docs/../secret.txt' }],
      { artifacts: {}, uploadRoot: 'uploads' },
    ),
    /cannot contain '\.\.'/,
  );
});

test('buildRunDialogSummary surfaces files, outputs, risk, and blocking issues', () => {
  const summary = buildRunDialogSummary({
    id: 'summary_dag',
    name: 'Summary DAG',
    artifacts: {
      upload_source: {
        id: 'upload_source',
        paths: ['uploads/source/source.md'],
        description: 'source.md',
        metadata: {
          source: 'upload',
          kind: 'file',
          hidden: true,
          display_name: 'source.md',
        },
      },
      report: {
        id: 'report',
        paths: ['outputs/report.md'],
        description: 'Report',
      },
    },
    nodes: [
      {
        id: 'agent_1',
        target: 'agent.capability',
        inputs: {},
        artifact_inputs: ['upload_source'],
        artifact_outputs: ['report', 'missing_output'],
      },
      {
        id: 'broken',
        target: '',
        inputs: {},
        artifact_inputs: ['missing_input'],
      },
    ],
    edges: [{ source: 'agent_1', target: 'broken', reason: 'test' }],
  });

  assert.equal(summary.nodeCount, 2);
  assert.equal(summary.edgeCount, 1);
  assert.deepEqual(summary.inputArtifacts, [
    {
      id: 'upload_source',
      label: 'source.md',
      path: 'uploads/source/source.md',
      kind: 'file',
    },
  ]);
  assert.deepEqual(summary.outputArtifacts, [
    {
      id: 'report',
      label: 'report',
      path: 'outputs/report.md',
      kind: 'artifact',
    },
  ]);
  assert.deepEqual(summary.riskyNodes, [
    {
      id: 'agent_1',
      capabilityId: 'agent.capability',
      risk: 'medium',
    },
  ]);
  assert.equal(summary.canRun, false);
  assert.deepEqual(summary.issues.map((issue) => issue.message), [
    "Node 'agent_1' references unknown output artifact 'missing_output'.",
    "Node 'broken' is missing a target.",
    "Node 'broken' references unknown input artifact 'missing_input'.",
  ]);
});

test('appendRunTranscriptToken streams consecutive text into one message', () => {
  const timeline = appendRunTranscriptToken([], 'Hello ');
  const next = appendRunTranscriptToken(timeline, 'world');

  assert.deepEqual(next, [
    {
      type: 'text',
      content: 'Hello world',
    },
  ]);
});

test('appendReasoningTimeline streams reasoning into one open block', () => {
  const timeline = appendReasoningTimeline([], 'I should ');
  const next = appendReasoningTimeline(timeline, 'check the docs.');

  assert.deepEqual(timeline, [
    { type: 'reasoning', content: 'I should ', closed: false },
  ]);
  assert.deepEqual(next, [
    { type: 'reasoning', content: 'I should check the docs.', closed: false },
  ]);
});

test('closeReasoningTimeline closes reasoning before answer text', () => {
  const timeline = appendReasoningTimeline([], 'I should check the docs.');
  const closed = closeReasoningTimeline(timeline);
  const next = appendTextTimeline(closed, 'The answer is ready.');

  assert.deepEqual(next, [
    { type: 'reasoning', content: 'I should check the docs.', closed: true },
    { type: 'text', content: 'The answer is ready.' },
  ]);
});

test('completed assistant answers collapse reasoning and capability process trace', () => {
  assert.equal(typeof shouldCollapseProcessTimeline, 'function');
  assert.equal(typeof processTimelineSummary, 'function');
  assert.equal(typeof isProcessTimelineItem, 'function');

  const timeline = [
    { type: 'reasoning', content: 'I should inspect the file.', closed: true },
    {
      type: 'capability',
      event: {
        type: 'capability.call.started',
        invocation_id: 'invoke_1',
        capability_id: 'tool.read_file',
        arguments: { path: 'README.md' },
      },
      result: {
        type: 'capability.call.completed',
        invocation_id: 'invoke_1',
        capability_id: 'tool.read_file',
        content: 'contents',
      },
    },
    { type: 'validation', event: { type: 'validation.passed', passed: true, summary: 'ok', issues: [] } },
    { type: 'text', content: 'Final answer.' },
  ];
  const message = { role: 'assistant', content: 'Final answer.', timeline };

  assert.equal(shouldCollapseProcessTimeline(message, false), true);
  assert.equal(isProcessTimelineItem(timeline[0]), true);
  assert.equal(isProcessTimelineItem(timeline[3]), false);
  assert.deepEqual(processTimelineSummary(timeline), {
    reasoningCount: 1,
    capabilityCount: 1,
    validationCount: 1,
    failedCount: 0,
    runningCount: 0,
  });
});

test('process timeline summary treats review rejection separately from anomalies', () => {
  const rejected = {
    type: 'capability',
    event: {
      type: 'capability.call.started',
      invocation_id: 'invoke_rejected',
      capability_id: 'tool.write_file',
      arguments: { path: 'notes.md' },
    },
    result: {
      type: 'capability.call.failed',
      invocation_id: 'invoke_rejected',
      capability_id: 'tool.write_file',
      content: '人工审核已拒绝。\n\n反馈：不要写入文件',
    },
  };
  const failed = {
    type: 'capability',
    event: {
      type: 'capability.call.started',
      invocation_id: 'invoke_failed',
      capability_id: 'tool.read_file',
      arguments: { path: 'missing.md' },
    },
    result: {
      type: 'capability.call.failed',
      invocation_id: 'invoke_failed',
      capability_id: 'tool.read_file',
      content: 'FileNotFoundError: missing.md',
    },
  };

  assert.equal(processTimelineSummary([rejected]).failedCount, 0);
  assert.equal(processTimelineSummary([failed]).failedCount, 1);
});

test('running, answerless, and review assistant turns keep process trace expanded', () => {
  const runningCapability = {
    type: 'capability',
    event: {
      type: 'capability.call.started',
      invocation_id: 'invoke_1',
      capability_id: 'tool.read_file',
      arguments: { path: 'README.md' },
    },
  };
  const answerlessMessage = {
    role: 'assistant',
    content: '',
    timeline: [
      { type: 'reasoning', content: 'I should inspect the file.', closed: false },
      runningCapability,
    ],
  };
  const reviewMessage = {
    role: 'assistant',
    content: '请审核 DAG。',
    dagSnapshot: { dag_id: 'dag_1', status: 'awaiting_review' },
    timeline: [
      { type: 'dag', dag: { dag_id: 'dag_1', status: 'awaiting_review' } },
      { type: 'reasoning', content: 'Need review.', closed: true },
      { type: 'text', content: '请审核 DAG。' },
    ],
  };

  assert.equal(shouldCollapseProcessTimeline(answerlessMessage, true), false);
  assert.equal(shouldCollapseProcessTimeline(answerlessMessage, false), false);
  assert.equal(processTimelineSummary(answerlessMessage.timeline).runningCount, 2);
  assert.equal(shouldCollapseProcessTimeline(reviewMessage, false), false);
  assert.equal(shouldCollapseProcessTimeline({
    role: 'assistant',
    content: 'Final answer.',
    timeline: [runningCapability, { type: 'text', content: 'Final answer.' }],
  }, false), true);
});

test('completed assistant answers collapse everything before the final text without reordering', () => {
  assert.equal(typeof collapsedProcessTimelineParts, 'function');
  const firstCall = {
    type: 'capability',
    event: {
      type: 'capability.call.started',
      invocation_id: 'invoke_1',
      capability_id: 'tool.read_file',
      arguments: { path: 'README.md' },
    },
    result: {
      type: 'capability.call.completed',
      invocation_id: 'invoke_1',
      capability_id: 'tool.read_file',
      content: 'contents',
    },
  };
  const secondCall = {
    type: 'capability',
    event: {
      type: 'capability.call.started',
      invocation_id: 'invoke_2',
      capability_id: 'tool.search',
      arguments: { query: 'dagent trace' },
    },
    result: {
      type: 'capability.call.completed',
      invocation_id: 'invoke_2',
      capability_id: 'tool.search',
      content: 'result',
    },
  };
  const timeline = [
    { type: 'reasoning', content: 'I should inspect the file.', closed: true },
    firstCall,
    { type: 'text', content: '我先说明一下阶段性结论。' },
    { type: 'reasoning', content: 'Now refine the final answer.', closed: true },
    secondCall,
    { type: 'text', content: '这是最终回答。' },
  ];

  const parts = collapsedProcessTimelineParts(timeline);

  assert.deepEqual(parts.processItems, timeline.slice(0, 5));
  assert.deepEqual(parts.finalAnswerItem, timeline[5]);
  assert.deepEqual(parts.trailingItems, []);
});

test('message timeline renders completed process trace behind a collapsible summary', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const summarySource = appSource.match(/function ProcessSummaryCard[\s\S]*?\nfunction processSummaryText/)?.[0] ?? '';

  assert.match(appSource, /shouldCollapseProcessTimeline/);
  assert.match(appSource, /collapsedProcessTimelineParts/);
  assert.match(appSource, /function ProcessSummaryCard/);
  assert.match(appSource, /const collapsedProcess = collapseProcess \? collapsedProcessTimelineParts\(timeline\) : null;/);
  assert.match(appSource, /completedProcessTimeline/);
  assert.match(appSource, /completed-process-timeline/);
  assert.match(appSource, /renderCollapsedMessageTimeline/);
  assert.match(appSource, /<ProcessSummaryCard[\s\S]*items=\{collapsedProcess\.processItems\}/);
  assert.match(summarySource, /<Wrench size=\{14\} \/>/);
  assert.doesNotMatch(summarySource, /AlertTriangle/);
  assert.doesNotMatch(summarySource, /process-summary-warning/);
});

test('expanded and live process traces share the subtle timeline treatment', async () => {
  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const messageTimelineSource = appSource.match(/function MessageTimeline[\s\S]*?\nfunction renderCollapsedMessageTimeline/)?.[0] ?? '';

  assert.match(appSource, /isProcessTimelineItem/);
  assert.match(messageTimelineSource, /const liveProcessTimeline = !collapseProcess && timeline\.some\(isProcessTimelineItem\);/);
  assert.match(messageTimelineSource, /const completedProcessTimeline = Boolean\(collapsedProcess\);/);
  assert.match(messageTimelineSource, /className=\{`message-timeline\$\{liveProcessTimeline \? ' live-process-timeline' : ''\}\$\{completedProcessTimeline \? ' completed-process-timeline' : ''\}`\}/);

  assert.match(css, /\.assistant-turn-frame :is\(\.process-summary-body, \.message-timeline\.live-process-timeline, \.message-timeline\.completed-process-timeline\)\s*\{[^}]*gap:\s*4px;[^}]*padding:\s*4px 6px 6px;/s);
  assert.doesNotMatch(css, /\.assistant-turn-frame \.process-summary-body::before/);
  assert.doesNotMatch(css, /\.assistant-turn-frame \.message-timeline\.live-process-timeline::before/);
  assert.doesNotMatch(css, /\.assistant-turn-frame \.message-timeline\.completed-process-timeline::before/);
  assert.match(css, /\.assistant-turn-frame :is\(\.process-summary-body, \.message-timeline\.live-process-timeline, \.message-timeline\.completed-process-timeline\) > :is\(\.think-block, \.timeline-card, \.capability-event-card, \.dag-summary-card, \.markdown-body\)\s*\{[^}]*border-left:\s*3px solid #d8dee8;/s);
  assert.match(css, /\.assistant-turn-frame :is\(\.process-summary-body, \.message-timeline\.live-process-timeline, \.message-timeline\.completed-process-timeline\) > \.think-block\s*\{[^}]*border-left-color:\s*#94a3b8;/s);
  assert.match(css, /\.assistant-turn-frame :is\(\.process-summary-body, \.message-timeline\.live-process-timeline, \.message-timeline\.completed-process-timeline\) > \.capability-event-card\s*\{[^}]*border-left-color:\s*#2dd4bf;/s);
  assert.match(css, /\.assistant-turn-frame :is\(\.process-summary-body, \.message-timeline\.live-process-timeline, \.message-timeline\.completed-process-timeline\) > \.validation-card\s*\{[^}]*border-left-color:\s*#818cf8;/s);
  assert.match(css, /\.assistant-turn-frame :is\(\.process-summary-body, \.message-timeline\.live-process-timeline, \.message-timeline\.completed-process-timeline\) > \.dag-summary-card\s*\{[^}]*border-left-color:\s*#60a5fa;/s);
  assert.match(css, /\.assistant-turn-frame :is\(\.process-summary-body, \.message-timeline\.live-process-timeline, \.message-timeline\.completed-process-timeline\) > \.markdown-body\s*\{[^}]*border-left-color:\s*#cbd5e1;/s);
});

test('appendRunTranscriptCapability pairs capability results with prior calls', () => {
  const call = {
    type: 'capability.call.started',
    invocation_id: 'invoke_1',
    capability_id: 'tool.read_file',
    arguments: { path: 'inputs/source.md' },
  };
  const result = {
    type: 'capability.call.completed',
    invocation_id: 'invoke_1',
    capability_id: 'tool.read_file',
    content: 'file contents',
  };

  const timeline = appendRunTranscriptCapability([], call);
  const next = appendRunTranscriptCapability(timeline, result);

  assert.equal(next.length, 1);
  assert.deepEqual(next[0], {
    type: 'capability',
    event: call,
    result,
  });
});

test('appendRunTranscriptTraceEvent records static tool results from trace snapshots', () => {
  const call = {
    type: 'capability.call.started',
    invocation_id: 'invoke_1',
    capability_id: 'tool.read_file',
    arguments: { path: 'inputs/source.md' },
  };
  const traceEvent = {
    id: 'trace_read:completed',
    event_id: 'trace_read',
    type: 'capability',
    label: 'tool.read_file',
    detail: 'hello',
    status: 'completed',
    timestamp: '10:00:00',
    node_id: 'read',
    payload: {
      invocation_id: 'invoke_1',
      capability_id: 'tool.read_file',
      input: { path: 'inputs/source.md' },
      output: 'hello',
      result: {
        invocation_id: 'invoke_1',
        capability_id: 'tool.read_file',
        kind: 'tool',
        status: 'completed',
        content: 'hello',
      },
    },
  };

  const started = appendRunTranscriptCapability([], call);
  const next = appendRunTranscriptTraceEvent(started, traceEvent);

  assert.equal(next.length, 1);
  assert.deepEqual(next[0], {
    type: 'trace',
    event: traceEvent,
  });
});

test('appendRunTranscriptTraceEvent records static DAG node lifecycle events', () => {
  const traceEvent = {
    id: 'trace_node:completed',
    event_id: 'trace_node',
    type: 'node',
    label: 'summarize',
    detail: 'completed',
    status: 'completed',
    timestamp: '10:00:00',
    node_id: 'summarize',
    payload: {
      node_id: 'summarize',
      input: { topic: 'pricing' },
      output: 'done',
    },
  };

  const next = appendRunTranscriptTraceEvent([], traceEvent);

  assert.deepEqual(next, [
    {
      type: 'trace',
      event: traceEvent,
    },
  ]);
});

test('runTranscriptFromTraceEvents rebuilds static run timelines from persisted trace events', () => {
  const traceEvents = [
    {
      id: 'node_a:completed',
      event_id: 'node_a',
      type: 'node',
      label: 'collect',
      detail: 'collected',
      status: 'completed',
      timestamp: '10:00:00',
      node_id: 'collect',
      payload: { output: 'collected' },
    },
    {
      id: 'node_b:completed',
      event_id: 'node_b',
      type: 'node',
      label: 'summarize',
      detail: 'summarized',
      status: 'completed',
      timestamp: '10:00:01',
      node_id: 'summarize',
      payload: { output: 'summarized' },
    },
  ];

  const timeline = runTranscriptFromTraceEvents(traceEvents);

  assert.equal(timeline.length, 2);
  assert.deepEqual(timeline.map((item) => item.type), ['trace', 'trace']);
  assert.deepEqual(timeline.map((item) => item.event.node_id), ['collect', 'summarize']);
});

test('responseDeltaPayload preserves native response identity fields', () => {
  assert.deepEqual(responseDeltaPayload({
    delta: 'hello',
    response_id: 'resp_1',
    model_step: '2',
    run_id: 'run_1',
    dag_id: 'dag_1',
    node_id: 'answer',
    parent_capability_id: 'tool.echo',
  }), {
    delta: 'hello',
    response_id: 'resp_1',
    model_step: 2,
    run_id: 'run_1',
    dag_id: 'dag_1',
    node_id: 'answer',
    parent_capability_id: 'tool.echo',
  });
  assert.throws(() => responseDeltaPayload({ delta: 'hello' }), /Missing response_id/);
});

test('runStartedPayload validates the resolved run kind', () => {
  assert.deepEqual(runStartedPayload({ kind: 'dynamic_dag' }), { kind: 'dynamic_dag' });
  assert.throws(() => runStartedPayload({ kind: 'legacy' }), /Unsupported run kind/);
});
