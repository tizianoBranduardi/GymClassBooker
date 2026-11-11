# Copilot instructions

Follow [AGENTS.md](../AGENTS.md) at the repository root: layout, commands,
test-first workflow, dependency-injection conventions and the hard security
rules (no secrets or vendor values in the repo, constant-time webhook secret
check, least-privilege IAM).

Quick reminders:

- Python 3.12, type-annotated signatures, `logging` not `print`.
- Tests are offline (mocked AWS, Telegram, gym API); run `python -m pytest`.
- Commit messages are one lowercase line: `feat: ...`, `fix: ...`, `docs: ...`.
