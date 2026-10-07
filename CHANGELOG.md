# Changelog

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
