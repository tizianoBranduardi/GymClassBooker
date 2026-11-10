# Operations guide

Everything you need to run the stack on your own, without the original author.

## Day-to-day (all inside Telegram)

You are the **admin** (the Telegram user id given at deploy time).

| Who | Command | What it does |
|---|---|---|
| Anyone | message the bot | Becomes `pending`; you get an approval request |
| Admin | `/approve <id>` / `/deny <id>` | Decide on a pending user |
| Admin | `/pending`, `/users` | Requests waiting for a decision / everyone and their status |
| Admin | `/remove <id>` | Delete a user: cancels their schedules, deletes their stored gym credentials and record |
| Approved user | `/login` | Guided: link the gym account (message is deleted right away) |
| Approved user | `/status` | Show the linked account |
| Approved user | `/stop` | Abort a guided `/login` in progress |
| Approved user | `/search <date> <time>` | Classes from that time, +3 h, for 4 days |
| Approved user | `/pick <n>` | Schedule booking of option *n* |
| Approved user | `/bookings`, `/cancel <id>` | List / cancel scheduled bookings |
| Approved user | plain text | Natural language ("book spinning tomorrow at 7") |

The booker reports every outcome to the admin and to the booking user.

## Operator commands

Run from the repository root with the virtualenv active
(`pip install -r requirements-dev.txt`).

```bash
python scripts/manage.py doctor     # tools, AWS credentials, Bedrock model access
python scripts/manage.py deploy     # create/update everything, register the webhook
python scripts/manage.py status     # stack status + Telegram webhook health
python scripts/manage.py webhook    # rotate the webhook secret and re-register
python scripts/manage.py destroy    # delete the stack, schedules and all secrets
```

Use `--profile`, `--region` and `--stack` (or `AWS_PROFILE`, `AWS_REGION`,
`STACK_NAME`) to pick the account, region and stack name. **One stack per
account and region**: table, topic and secret names are fixed.

## Common tasks

**Update the code** — pull, run the tests, then `python scripts/manage.py deploy`
again. Re-running is safe: unchanged resources are left alone.

**Change a setting** (model, club, alarm email) — edit `.env.deploy` and deploy again.
Every parameter is re-applied on each deploy, so keep the file complete.

**Rotate the Telegram bot token** — revoke it in @BotFather (`/revoke`), put the
new token in `.env.deploy` and deploy again; the secret and webhook are refreshed.

**Rotate the webhook secret** — `python scripts/manage.py webhook`.

**Hand the admin role to someone else** — set `ADMIN_USER_ID` to their Telegram id,
deploy again.

**Remove one user** — the admin sends `/remove <telegram_id>` to the bot.

**Tear everything down** — `python scripts/manage.py destroy`. It removes the
stack, pending schedules, every user's stored credentials, the webhook secret and
the bot-token secret, and unregisters the Telegram webhook.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Bot never answers | `manage.py status`: check `last error` and `pending updates`; re-run `manage.py webhook` |
| `AccessDenied ... explicit deny in a service control policy` | The account only allows some regions: deploy in the allowed region |
| `cannot call model ...: AccessDeniedException` | Enable that model for your region in the Bedrock console, or set `BEDROCK_MODEL_ID` |
| Assistant says it can only help with booking | Working as intended: the guardrail blocks off-topic requests |
| "Login failed" after a user changed their gym password | The user runs `/login` again |
| Booking missed | CloudWatch alarm `<stack>-booker-errors` and the booker log group show why; the API's reason (e.g. `CLASS_FULL`) is in the Telegram report |
| Schedules did not fire | EventBridge Scheduler (default group), names start with `gym-`; check the time zone is Europe/Rome |

Logs: CloudWatch Logs groups `/aws/lambda/<stack>-BotFunction-*` and
`/aws/lambda/<stack>-BookerFunction-*`.

## Required permissions

The deploying identity needs rights on CloudFormation, Lambda, IAM (roles),
DynamoDB, Secrets Manager, EventBridge Scheduler, Bedrock (guardrails and invoke),
SNS, CloudWatch and S3 (SAM artifact bucket). A personal sandbox account with
administrator access is the simplest option.

## Security notes

- The Function URL is public by design (Telegram cannot sign requests); every
  request must carry the secret header or it is rejected.
- Users' gym credentials live in Secrets Manager, one secret per user. Anyone with
  `secretsmanager:GetSecretValue` on `gym-booker/user/*` can read them.
- The gym API key is stored as a Lambda environment variable.
- Never commit `.env.deploy`; it is git-ignored.
