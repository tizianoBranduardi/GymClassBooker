# GymClassBooker

The class you want fills up minutes after booking opens, and you are never at
your phone at that exact moment. GymClassBooker is.

Message a Telegram bot what you want ("book spinning tomorrow at 7"), pick a
class, and walk away. At the exact second the booking window opens, a Lambda
function books it for you, then tells you whether you got in.

- **Chat, don't click:** plain-language requests or simple commands, in Telegram.
- **Exact timing:** a one-time schedule wakes the booker just before the window
  opens, and it waits for the precise instant instead of guessing.
- **Shareable:** an admin approves who can use the bot; each user links their own
  account, and credentials are stored in AWS Secrets Manager.
- **Nothing to babysit:** fully serverless, pay-per-use, no server to keep
  running; one command deploys it and one command removes it.

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

- **Webhook:** public Function URL guarded by the `X-Telegram-Bot-Api-Secret-Token`
  header (secret `gym-booker/webhook-secret`).
- **Enrollment:** anyone can message the bot and becomes `pending`; only the admin
  (`AdminUserId`) is asked to `/approve <id>` or `/deny <id>`; `/users` lists everyone.
- **Linking an account:** approved users run `/login`; the message is deleted
  immediately, the credentials are validated against the gym API and stored in
  Secrets Manager (`gym-booker/user/<telegram_user_id>`); the association lives in
  DynamoDB (`bookingscript-users`).
- **Booking:** `/search <date> <time>`, `/pick <n>`, `/bookings`, `/cancel <id>`.
  A one-time schedule (Europe/Rome, self-deleting) fires 3 minutes before the
  window opens; the booker waits for the exact instant (500 ms polling), then
  books with a retry loop for `TOO_EARLY_TO_BOOK` (90 s budget).
- **Natural language:** free text from a linked user goes to Amazon Bedrock
  (Converse API) with four tools: search, schedule, list, cancel. The model
  must ask for missing details and get an explicit confirmation before booking.
- **Scope guardrail:** a Bedrock Guardrail is attached to every model call and
  blocks off-topic requests and requests for secrets or the system prompt.
- **Reports:** the booker messages the admin on every outcome; a CloudWatch alarm
  on booker errors publishes to an SNS topic (optional `AlarmEmail`).

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
