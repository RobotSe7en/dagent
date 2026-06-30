# Project Management Files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add polished project and conversation management UI, project-scoped file management, and project conversation file preview parity.

**Architecture:** Keep persistence and file operations in `api/`; SDK remains unchanged. Project and conversation state stays in SQLite, while project file operations are bounded to `Project.workspace_uri` through the API. The Web UI keeps “会话” for standalone conversations and “项目” for project trees/details, reusing the existing workbench preview idiom for files and artifacts.

**Tech Stack:** FastAPI, SQLite store, local workspace files, React/TypeScript, existing source tests in `web/scripts/schemaArguments.test.mjs`, pytest.

---

### Task 1: Backend Project Management And File API

**Files:**
- Modify: `api/app.py`
- Modify: `api/storage/base.py`
- Modify: `api/storage/sqlite.py`
- Test: `tests/test_api_persistence.py`

- [ ] Add failing pytest coverage for project update/delete and safe project file operations.
- [ ] Implement `Store.update_project` and `Store.delete_project`.
- [ ] Add `PATCH /projects/{project_id}` and `DELETE /projects/{project_id}`.
- [ ] Add bounded project file helpers and endpoints for list, upload, folder creation, rename, delete, download, and preview.
- [ ] Run `uv run --extra dev pytest tests/test_api_persistence.py tests/test_api.py`.

### Task 2: Frontend API Contracts

**Files:**
- Modify: `web/src/api.ts`
- Modify: `web/src/types.ts`
- Test: `web/scripts/schemaArguments.test.mjs`

- [ ] Add source tests for project update/delete helpers and project file helpers.
- [ ] Add `ProjectFileItem`, `ProjectFileListResponse`, and `ProjectFilePreview` types.
- [ ] Implement API helpers for project update/delete and project files.
- [ ] Run `npm --prefix web test -- --test-name-pattern "project management|project files"`.

### Task 3: Sidebar And Modal UI

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`
- Test: `web/scripts/schemaArguments.test.mjs`

- [ ] Add source tests that native `window.prompt`/`window.confirm` are no longer used for project creation or conversation deletion.
- [ ] Add source tests that the “会话” submenu filters `project_id === null`.
- [ ] Add source tests that the “项目” submenu renders project rows with expandable conversation children.
- [ ] Implement shared modal surfaces for new project, delete conversation, edit project, and delete project.
- [ ] Run `npm --prefix web test`.

### Task 4: Project Detail And File Preview

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`
- Test: `web/scripts/schemaArguments.test.mjs`

- [ ] Add source tests for `ProjectDetailWorkspace`, file tree, upload/folder/rename/delete/download actions, and preview panel.
- [ ] Render project detail when a project row is selected.
- [ ] Implement project file tree and preview using the same right-side preview pattern as conversation artifacts.
- [ ] Keep project conversation chat using the existing artifact/file preview drawer.
- [ ] Run `npm --prefix web run build`.

### Task 5: Verification And Restart

**Files:**
- No additional source files.

- [ ] Run `uv run --extra dev pytest`.
- [ ] Run `npm --prefix web test`.
- [ ] Run `npm --prefix web run build`.
- [ ] Run `git diff --check`.
- [ ] Confirm `.dagent/runs` was not recreated by tests.
- [ ] Restart API and Web dev servers on `0.0.0.0`.
