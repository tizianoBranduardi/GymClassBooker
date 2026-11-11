# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.
`CLAUDE.md`, `.github/copilot-instructions.md` and `.cursor/rules/` all point
here, so this is the single source of truth.

## What this is

GymClassBooker: a serverless Telegram bot (AWS Lambda, Python 3.12) that books
gym classes at the exact second the booking window opens. Bedrock Converse
turns plain language into tool calls; EventBridge Scheduler wakes a booker
Lambda just before the window opens. State lives in DynamoDB, per-user gym
credentials in Secrets Manager. See `README.md` for the architecture and
`docs/OPERATIONS.md` for running it.

## Layout

```
lambda/bot/          Lambda code + template.yaml (SAM). Flat modules, no package.
  handler.py         Telegram webhook (Function URL): secret check, routing, async self-invoke
  commands.py        Slash commands          admin.py / enrollment.py   admin + approval flow
  assistant.py       Bedrock Converse + tool use, behind a guardrail
  booker_handler.py  Booker Lambda entry point (invoked by Scheduler)
  booking_core.py    Window maths, wait_until_open, book_with_retry (pure, injectable sleep)
  gym_client.py      Gym booking API client (base URL / app id from env)
  *_store.py, scheduler_client.py, telegram_api.py   AWS / Telegram adapters
scripts/manage.py    Operator CLI: doctor | deploy | status | webhook | destroy
tests/bot, tests/scripts   pytest suites (fully offline)
docs/OPERATIONS.md   Runbook for operators
```

## Commands

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest                                              # all tests
python -m pytest --cov=lambda/bot --cov=scripts --cov-report=term-missing
sam validate --lint -t lambda/bot/template.yaml               # after touching the template
```

`pytest.ini` puts `lambda/bot` and `scripts` on `pythonpath`, so tests import
modules by bare name (`import handler`). Never run a real deploy to "check"
something: `manage.py deploy` creates billable AWS resources and needs the
operator's credentials. Tests and `sam validate` are the verification.

## How we work

- **Test first.** Write the failing test, then the minimum code, then refactor.
  Coverage must stay at or above 80% (currently 84%).
- **Everything is offline in tests.** Mock AWS, Telegram and the gym API. A test
  must never need credentials, network or real sleeps.
- **Dependency injection, not patching internals.** Handlers take a `Deps`
  dataclass (`handler.Deps`, `booker_handler.Deps`). Time-dependent code takes
  an injectable `sleep_fn`/clock. New collaborators go on `Deps`.
- **Keep the contract test green.** `tests/bot/test_deps_contract.py` binds every
  call handlers make on `Deps` to the real functions' signatures, because
  `mock.Mock` accepts wrong arguments. If you add or change a function used
  via `Deps`, update the contract test too.
- **Small modules.** None over 300 lines (`manage.py` is the one that needs
  watching). Split by responsibility rather than growing a file.
- **Immutability.** Return new objects rather than mutating arguments; prefer
  frozen dataclasses for new value types.
- **Type annotations** on all function signatures; PEP 8; use `logging`, not `print`
  (the operator CLI's user-facing output is the exception).
- **Errors:** handle explicitly, log context server-side, never swallow, and keep
  user-facing Telegram messages friendly. Never include secrets in either.

## Hard rules (security and privacy)

- **No secrets, ids or vendor values in the repo.** The gym API key, base URL and
  app id are deploy-time inputs (`GYM_API_KEY`, `GYM_API_BASE_URL`, `GYM_APP_ID`).
  Do not add defaults, fixtures or comments containing real values. Real
  config goes in the git-ignored `.env.deploy`.
- **No gym brand names** in code, docs, tests or commit messages. The project is
  deliberately vendor-neutral. The one accepted exception is the HTTP header
  `X-Va-Region`, which the upstream API requires.
- **Webhook secret check stays constant-time** (`hmac.compare_digest`) and runs
  before any other work. Missing or wrong header means 403.
- **Never log or echo passwords.** `/login` messages are deleted from the chat;
  credentials are validated, then stored only in Secrets Manager.
- **Least-privilege IAM.** New permissions in `template.yaml` must be scoped to
  specific resources or prefixes (`gym-booker/user/*`, schedules `gym-*`).
  `bedrock:InvokeModel` on `*` is a documented, known exception.
- **Webhook acknowledges first.** Telegram re-delivers non-200 responses, so do
  slow work (assistant calls) in the async self-invoke, not inline.
- Keep `.env*` (except `.env.example`), `.aws-sam/`, `samconfig.*` and `*.har`
  out of git.

## Domain facts worth knowing

- The booker is scheduled **3 minutes before** the window opens, then polls every
  0.5 s, books at the instant, and retries up to 90 s on `TOO_EARLY_TO_BOOK`.
  Lambda timeout is 360 s. If the window is already open, the booker is invoked
  immediately.
- Schedules are one-time, in `Europe/Rome`, `ActionAfterCompletion=DELETE`.
  Lambda itself runs in UTC, so always convert through `booking_core`.
- **One stack per AWS account and region**: table, topic and secret names are
  fixed (`bookingscript-users`, `bookingscript-bookings`, `gym-booker/...`).
- The assistant must ask for missing details and get an explicit confirmation
  before scheduling a booking. The model picks tools; the code executes them.
- `template.yaml` has no defaults for `AdminUserId`, `GymApiKey`,
  `GymApiBaseUrl`, `GymAppId`. Keep it that way.

## Adding things

- **New bot command:** add a failing test in `tests/bot/`, implement in
  `commands.py` (or `admin.py` for admin-only), document it in
  `docs/OPERATIONS.md`.
- **New assistant tool:** define it and its handler in `assistant.py`, add the
  guardrail/confirmation behavior to tests, update the README tool count.
- **New env var or template parameter:** add it to `template.yaml`, `.env.example`
  and `scripts/manage.py` (`FIELDS`), cover it in `tests/scripts/test_manage.py`,
  and mention it in the README configuration reference.
- **New dependency:** Lambda runtime needs go in `lambda/bot/requirements.txt`
  (boto3 is already in the runtime, so don't add it); dev-only in
  `requirements-dev.txt`. Pin versions.

## Commits and PRs

- One line, lowercase, no trailing period: `feat: ...`, `fix: ...`, `docs: ...`,
  `test: ...`, `chore: ...`, `refactor: ...`. Add `!` for breaking changes
  (`feat!: ...`). No ticket id, body or footers.
- Do not add AI attribution or `Co-Authored-By` trailers to commits.
- Run the tests, and `sam validate --lint` if the template changed, before
  committing. Do not push unless asked.
- Docs change with the code: README, `docs/OPERATIONS.md`, `.env.example`.
