@AGENTS.md

## Claude Code specifics

- Prefer the dedicated tools (Read, Edit, Grep) over shell equivalents; the AWS
  MCP server is available for read-only AWS lookups, but never create or change
  AWS resources unless the user asks.
- Run `python -m pytest` after every change and report real output. Do not claim
  a deploy works: it has only been verified through mocked tests and
  `sam validate --lint`.
- For a new feature, write the failing test first, show it failing, then implement.
- Commit only when asked, following the format in AGENTS.md (single line, no
  attribution trailer), and never push without being asked.
- Personal overrides go in `CLAUDE.local.md` or `.claude/settings.local.json`
  (both git-ignored).
