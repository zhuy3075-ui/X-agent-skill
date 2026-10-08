# Changelog

## v1.1.0 — 2026-10-08

- Add four MCP tools to save, read, filter and reindex authored Markdown research notes (35 tools total).
- Link posts, daily summaries, materials, comment needs, resources, publication combinations and decision reports.
- Preserve stable IDs, revision history, source evidence and user edits through explicit hash-based updates.
- Bundle seven Chinese templates and a single updated skill; initialize an empty local library without collecting X data.
- Keep PostgreSQL source data unchanged and exclude personal notes from public Git history.

## v1.0.1 — 2026-10-07

- Remove development reports, launch copy, internal agent instructions and old development guides from the distribution.
- Document browser auth_token retrieval, local installation, account registration and troubleshooting.
- Add a complete same-host server guide with database roles, private paths, systemd, HTTPS, upgrade and backup steps.
- Align server templates and simplify the skill to current supported operational rules.
- Preserve the complete runtime and existing user credentials and preferences.

## v1.0.0 — 2026-10-07

First public release of the PostgreSQL-backed X research MCP service and Chinese skills.

- 31 MCP tools over stdio and authenticated Streamable HTTP.
- Parameterized posts/comments collection, bounded pagination continuation and account rotation.
- Canonical author archives, current records, content versions, metric snapshots and change history.
- Explicitly requested hourly monitoring with bounded fair rotation and on-demand agent reports.
- Conservative visibility transitions; transient errors never confirm deletion.
- Bundled required Scweet runtime, one unified skill, automatic installer, component launcher and read-only health check.

Known limits: private GraphQL may change; visible comments are not all platform comments;
historical growth cannot be reconstructed before observations; suspected deletion is unconfirmed.
No shared 30-minute authentication cache or automatic periodic reports are shipped.
