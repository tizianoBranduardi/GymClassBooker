# GymClassBooker

**A serverless Telegram bot that books gym classes at the exact second the booking window opens, so you don't have to be awake, online or fast.**

## The problem

Popular classes fill up within minutes of the booking window opening, and the
window opens at a precise time that depends on the class: 48 hours before the
start for regular members, longer for priority members. Doing it by hand means
being at your phone at the right second. This project started as a Python
script on a Raspberry Pi triggered by cron, which worked but needed SSH access
and a machine at home. GymClassBooker is the same idea rebuilt as a managed,
shareable, serverless service.

## The solution

1. You message a Telegram bot, with plain language ("book spinning tomorrow at 7")
   or commands (`/search 2026-11-12 18:00`).
2. The bot searches the classes, you pick one, and it asks you to confirm.
3. A one-time schedule is created for just before the booking window opens.
4. At that moment a Lambda function logs in, waits for the exact instant, books
   the class (retrying briefly if the server's clock is slightly behind), and
   reports the outcome on Telegram.

Several people can use one deployment: an admin approves each user, and every
user links their own account.

> Personal project, not affiliated with or endorsed by any gym or fitness
> company. It talks to a booking API using your own account credentials and
> ships no vendor API values or branding; check that your gym's terms allow
> automated booking before using it.

## Architecture

```
You (Telegram) ─► Lambda Function URL ─► bot Lambda ─► assistant (Bedrock, tool use)
                                              ├─ DynamoDB (users, bookings, conversation)
                                              ├─ EventBridge Scheduler (one-time, window − 3 min)
                                              ▼
                                         booker Lambda ─► gym booking API
                                              └─► Telegram sendMessage (result)
```

| Piece | Role |
|---|---|
| **bot Lambda** (Function URL) | Telegram webhook: auth, enrollment, commands, hands free text to the assistant |
| **assistant** | Amazon Bedrock Converse with four tools (search, schedule, list, cancel) behind a guardrail |
| **EventBridge Scheduler** | One-time schedule per booking, fires 3 minutes before the window, deletes itself |
| **booker Lambda** | Logs in, waits for the exact instant, books, retries, reports |
| **DynamoDB** | Users, bookings, short conversation history (on-demand billing) |
| **Secrets Manager** | Bot token, webhook secret, one secret per user's gym credentials |
| **CloudWatch + SNS** | Alarm when the booker crashes (optional email) |

## Technical choices

| Choice | Why |
|---|---|
| **Serverless (Lambda, Scheduler, DynamoDB)** | The workload is a few seconds of work at unpredictable moments. Nothing runs, or bills, between bookings, and there is no server or cron host to keep alive. |
| **Python 3.12 on arm64 (Graviton)** | The original script was Python, so the proven booking logic carried over, and `boto3` is built into the runtime. arm64 is cheaper and fast enough here. |
| **Lambda Function URL, no API Gateway** | One webhook endpoint is all that's needed, so there is one less service to configure. Telegram can't sign AWS requests, so the endpoint is public and every request must carry a secret header, checked in code with a constant-time comparison. |
| **Scheduler fires 3 min early, then the booker waits** | Cold starts, login and scheduler delivery delay are absorbed before the window opens. The booker polls every 500 ms and fires the request at the instant, then retries for up to 90 s on `TOO_EARLY_TO_BOOK`. |
| **EventBridge Scheduler, not cron or `sleep`** | A schedule per booking, in the Europe/Rome time zone (Lambda runs in UTC), with retries and automatic deletion after it runs. If the window is already open, the booker is invoked immediately instead. |
| **Webhook acknowledges first, work runs async** | Telegram re-delivers any update that isn't answered with 200, which would duplicate work. The webhook returns at once and the assistant job runs in an asynchronous self-invoke. |
| **Amazon Bedrock Converse with tool use** | The model only picks a tool and its arguments; the code executes it. It must ask for missing details and get an explicit confirmation before booking. The model id is a deploy parameter, so you can pick any tool-capable model available in your region. |
| **Bedrock Guardrail (denied topics)** | Keeps the assistant on topic: general knowledge, code, math, creative writing and requests for secrets or the system prompt are blocked. |
| **Secrets Manager per user, not DynamoDB** | Credentials live apart from application data, with IAM scoped to a secret prefix. The trade-off: Secrets Manager bills per secret. |
| **Infrastructure as code (AWS SAM)** | One template defines everything, including the guardrail. `sam validate --lint` runs in development. |
| **Operator CLI (`scripts/manage.py`)** | `doctor`, `deploy`, `status`, `webhook`, `destroy`: a new person can deploy and remove the stack without knowing AWS internals. |

## Engineering approach

- **Test-first (TDD).** Red, green, refactor: tests are written before the
  behavior they describe. The suite has 153 tests with coverage above 80%.
- **Fully offline tests.** AWS, Telegram and the gym API are mocked, so the
  tests need no credentials or network and run in under a second.
- **Dependency injection.** Handlers receive their collaborators in a small
  `Deps` dataclass, and time-dependent code takes an injectable `sleep_fn`, so
  the wait-and-retry logic is tested with a fake clock instead of real sleeps.
- **Contract tests.** Mocks can't catch a wrong function signature, which once
  caused a `TypeError` in production. A contract test binds every call the
  handlers make to the real functions' signatures.
- **Small, focused modules.** Commands, admin, enrollment, assistant, booking
  core, stores and clients each live in their own file, none over 300 lines.
- **Typed, validated boundaries.** The operator CLI validates every input,
  never echoes secret values in errors, and is itself unit-tested.

## Security model

- Webhook requests need the `X-Telegram-Bot-Api-Secret-Token` header, compared in
  constant time; anything else gets a 403 before any work happens.
- **Closed enrollment:** anyone can message the bot, but only the admin can
  approve them. Unapproved users can't run any booking command.
- `/login` messages containing a password are deleted from the chat right away;
  credentials are validated, then stored only in Secrets Manager.
- Least-privilege IAM: secrets scoped by prefix, schedules scoped to `gym-*`,
  `iam:PassRole` limited to the scheduler, scheduler role trust restricted to the
  account. Known broader grant: `bedrock:InvokeModel` is allowed on all
  resources, because cross-region inference profiles resolve to models in
  several regions.
- No secrets in the repository; deploy-time values go in a git-ignored
  `.env.deploy` and `NoEcho` template parameters.

## Limits and trade-offs

- The booking API is the gym's private app API; it can change or block clients
  at any time, and its terms may not allow automation. You bring your own API values.
- Class times and the schedule time zone are fixed to Europe/Rome.
- The gym API key is a Lambda environment variable, not a secret.
- The guardrail uses its draft version so edits apply on deploy.
- One stack per AWS account and region: table, topic and secret names are fixed.

## Quick start (deploy it yourself)

You need: an AWS account, the [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html),
Python 3.12+, a Telegram account, and **your own** gym booking API values
(`GYM_API_KEY`, `GYM_API_BASE_URL`, `GYM_APP_ID`); this repository does not ship them.

1. **Create a Telegram bot:** talk to [@BotFather](https://t.me/BotFather), send `/newbot`, keep the token.
2. **Enable a Bedrock model** in your AWS region (Bedrock console, model access),
   or choose one you already can use (`BEDROCK_MODEL_ID`).
3. **Configure AWS credentials** (`aws configure`, or `export AWS_PROFILE=... AWS_REGION=...`).
4. **Install and check:**

   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements-dev.txt
   cp .env.example .env.deploy        # fill in the values (git-ignored)
   python scripts/manage.py doctor
   ```

5. **Deploy:**

   ```bash
   python scripts/manage.py deploy
   ```

   It stores the bot token in Secrets Manager, builds and deploys the stack
   (including the Bedrock guardrail), and registers the Telegram webhook. If you
   did not set `ADMIN_USER_ID`, it asks you to message your bot once and detects
   your Telegram id.
6. **Use it:** open your bot in Telegram, send `/start`, then `/login`.

Day-to-day use, updates, rotating secrets, troubleshooting and teardown are in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## Configuration reference

Nothing account- or vendor-specific is hard-coded. See
[`.env.example`](.env.example) and the parameters in
[`lambda/bot/template.yaml`](lambda/bot/template.yaml). Never commit real values.

## Development

```bash
python -m pytest --cov=lambda/bot --cov=scripts --cov-report=term-missing
sam validate --lint -t lambda/bot/template.yaml
```

> **Region note:** if your AWS account restricts services to a single region,
> deploy in that region; an `AccessDenied ... explicit deny in a service control
> policy` error means wrong region, not a broken account.
