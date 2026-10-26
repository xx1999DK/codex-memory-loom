# Changelog

## 0.1.0 — 2026-10-27

First packaged release of **Memory Loom · 项目织忆**.

- Project-local Markdown memory with task continuity, navigation and sourced direction summaries.
- Explicit project identity and boundaries; reuse existing task folders without copying old records.
- `init`, `new-task`, `search`, `reindex` and read-only `check` commands.
- Bounded keyword search, with task body matching enabled only on request.
- Safer initialization, write locking and index generation that preserves hand-written knowledge links.
- Reproducible ZIP releases, SHA-256 checksums, and an installer with guarded updates and backups.
- Chinese and English guides, a visual identity, and automated tests.

### Compatibility

The prototype was called `project-memory`. Its skill invocation is now `$memory-loom`.
Existing `.project-memory/` data, schema version `1`, project IDs, task IDs and managed Markdown markers remain unchanged. Do not reinitialize or move project data to adopt the new name. Update customized workflow files by reviewing differences; upgrading the skill does not rewrite existing project instructions.
