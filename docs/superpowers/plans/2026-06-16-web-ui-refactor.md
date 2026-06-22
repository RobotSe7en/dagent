# Web UI Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the web UI to match the supplied chat workspace design without preserving the old topbar layout or adding compatibility shims.

**Architecture:** Keep the existing API calls, stream handlers, DAG data model, review resume flow, and capability timeline model. Replace the visible shell with the design draft: left workspace sidebar, central chat execution stream, right artifact preview panel, and restyled DAG review modal. Extract only the data helpers needed for the new UI; do not introduce parallel legacy render paths.

**Tech Stack:** React 19, TypeScript, Vite, lucide-react, ReactMarkdown, @xyflow/react, existing node:test-based web tests.

---

### Task 1: Artifact Panel Data Contract

**Files:**
- Create: `web/src/workbenchArtifacts.ts`
- Modify: `web/scripts/schemaArguments.test.mjs`

- [ ] Add a failing node:test case that imports `buildWorkbenchArtifacts` and expects visible artifact entries from DAG artifact contracts and runtime trace artifacts.
- [ ] Run `npm --prefix web test` and confirm the new test fails because `web/src/workbenchArtifacts.ts` is missing.
- [ ] Implement `WorkbenchArtifactItem`, `buildWorkbenchArtifacts`, and `artifactPreviewText` with no fallback fake data.
- [ ] Run `npm --prefix web test` and confirm the artifact tests pass.

### Task 2: Strict Design Shell

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

- [ ] Replace the old `.topbar` app chrome with a left sidebar and workspace body matching `Dagent.dc.html`.
- [ ] Render all four workspaces through the sidebar only; remove the top navigation render path.
- [ ] Add sidebar collapse state, chat history rows derived from the current message list, and the `.dagent/runs` workspace root chip.
- [ ] Keep the existing workspace handlers and public API calls unchanged.

### Task 3: Chat Execution Workspace

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

- [ ] Rebuild chat layout with central message stream and bottom composer matching the design draft.
- [ ] Render user turns as right-aligned accent bubbles.
- [ ] Render assistant turns as a unified execution frame with reasoning, DAG, capability, answer, and validation timeline sections.
- [ ] Rewire existing `runStream`, `stopStream`, `newChat`, target mode, review level, validation toggle, and capability scope controls into the new composer.

### Task 4: Artifact Preview Panel

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`
- Use: `web/src/workbenchArtifacts.ts`

- [ ] Add the right artifact panel and collapsed rail exactly following the design draft.
- [ ] Drive file count, selected item, labels, metadata, and preview text from `buildWorkbenchArtifacts`.
- [ ] Show a real empty state when there are no artifacts.
- [ ] Do not add sample artifacts or compatibility placeholders.

### Task 5: DAG Review Modal Restyle

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

- [ ] Restyle `DagReviewDialog` header, actions, flow region, inspector, and feedback footer to match the design draft.
- [ ] Keep the existing confirm, reject, add node, delete node, node patch, edge patch, and selection behavior.
- [ ] Preserve React Flow as the interactive DAG surface, but remove old modal visual chrome.

### Task 6: Verification

**Commands:**
- `npm --prefix web install`
- `npm --prefix web test`
- `npm --prefix web run build`
- `git diff --check`

- [ ] Install worktree-local dependencies.
- [ ] Run the web tests.
- [ ] Run the production build.
- [ ] Run whitespace diff checks.
- [ ] Inspect the final diff for leftover old topbar/chat visual paths, fake artifacts, compatibility aliases, and historical shims.

### Task 7: Direct HTML Design Port

**Files:**
- Modify: `web/scripts/schemaArguments.test.mjs`
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

- [x] Replace the previous approximation tests with checks for the design draft structure: all four sidebar entries from `Dagent.dc.html`, a chat-only implementation path for the main workspace, real backend stream wiring, the artifact drawer, and the non-chat placeholder.
- [x] Run `npm --prefix web test` and confirm the design-port test fails before implementation.
- [x] Translate the `Dagent.dc.html` sections into React/TSX in `App.tsx`: sidebar, real-data chat stream, composer, artifact drawer, and non-chat placeholder.
- [x] Translate the inline design CSS values into class-based CSS in `styles.css`: exact shell columns, sidebar dimensions, chat max widths, artifact widths, row paddings, font sizes, colors, and validation strip.
- [x] Run `npm --prefix web test`, `npm --prefix web run build`, and browser screenshots against `http://127.0.0.1:5173/`.
