# Fork Features

This fork lives at
[`Huygon764/claude-code-telegram`](https://github.com/Huygon764/claude-code-telegram)
and tracks upstream
[`RichardAtCT/claude-code-telegram`](https://github.com/RichardAtCT/claude-code-telegram).

Use this doc when bootstrapping the bot on a fresh machine -- it covers the
fork-only commands, env vars, and the 9Router runbook end to end.

Quick map:

| Feature | Commands | Required env |
| --- | --- | --- |
| [Cursor Agent backend](#cursor-agent-backend) | `/cursor`, `/cursor_usage`, `/backend cursor` | `cursor-agent` on `PATH` |
| [9Router backend](#9router-backend) | `/9router`, `/backend 9router` | `NINE_ROUTER_AUTH_TOKEN` |
| [Plan usage](#plan-usage) | `/usage` | (uses Claude OAuth token) |
| [External session adoption](#external-session-adoption) | `/resume <id>` | — |
| [Diff review](#diff-review) | `/review`, `/review deep` | — |
| [Build info](#build-info) | `/version` | — |
| [Self-restart and deploy](#self-restart-and-deploy) | `/restart`, `/deploy` | supervisor (see below) |

---

## Cursor Agent backend

Run requests through [Cursor Agent CLI](https://cursor.sh) instead of the
Anthropic SDK. Useful when you want to use a Cursor subscription quota or a
different model selection.

**Setup**

1. Install Cursor and the `cursor-agent` CLI so it's resolvable on `PATH`
   (verify with `which cursor-agent`).
2. Log in once: `cursor-agent login`.
3. (Optional) override the model in `.env`: `CURSOR_MODEL=<model-name>`.
   Leave blank to use the CLI default.

No bot env is required — the integration calls `cursor-agent` directly.

**Usage**

- `/cursor <prompt>` — one-shot send to Cursor regardless of default backend.
- `/backend cursor` — make Cursor the default for plain text messages.
- `/cursor_usage` — show Cursor subscription quota.

Sessions for Cursor are stored in a separate `cursor_sessions` SQLite table so
auto-resume never picks up a Claude UUID by mistake (the fix in commit
`1e82636`).

## 9Router backend

Route Claude SDK calls through a local [9Router](https://github.com/decolua/9router)
proxy. Lets you fan out across upstream providers (OpenCode, OpenRouter,
direct Anthropic, ...) under a single combo route, without touching shell env.

This is the full bootstrap for a fresh machine.

### A. Build and run the container

```bash
git clone https://github.com/decolua/9router ~/repos/9router
cd ~/repos/9router
cp .env.example .env
```

Edit `~/repos/9router/.env` and set the required fields (others can stay at
defaults):

```
JWT_SECRET=<openssl rand -hex 32>
INITIAL_PASSWORD=<choose a password for first dashboard login>
DATA_DIR=/var/lib/9router
PORT=20128
```

Build + start:

```bash
bash start.sh
docker ps                                # expect a "9router" entry
curl http://localhost:20128/api/health   # expect {"ok": true, ...}
```

(Optional) auto-start the container whenever Docker is up:

```bash
docker update --restart unless-stopped 9router
```

### B. Create a Combo in the dashboard

Open `http://localhost:20128`, log in with `INITIAL_PASSWORD`.

1. Go to **Combos** in the sidebar. Click **Create Combo** (top right).
2. Fill the modal:
   - **Combo Name**: `Free-models` (this is what the bot sends as
     `ANTHROPIC_MODEL`; pick a different name only if you update
     `NINE_ROUTER_MODEL` to match).
   - **Models**: add OpenCode free models with **Add Model**. The stack
     used for this fork:
     ```
     oc/deepseek-v4-flash-free
     oc/qwen3.6-plus-free
     oc/minimax-m2.5-free
     oc/mimo-v2.5-free
     oc/nemotron-3-super-free
     ```
     The `oc/` prefix selects the bundled OpenCode provider -- no separate
     provider setup is needed for these.
   - **Save**.
3. On the combo row, enable **Round Robin** so requests rotate across the
   five upstreams (otherwise it always tries the first one and falls back).
4. Go to **API Keys** -> **Create**. Copy the token; paste it into
   `NINE_ROUTER_AUTH_TOKEN` in the next step.

> Adding other providers (Anthropic direct, OpenRouter, paid upstreams) is
> done under **Providers** in the sidebar; each needs its own provider API
> key. You can then mix those model IDs into the same combo or create a
> separate combo and switch `NINE_ROUTER_MODEL` per use case.

### C. Wire the bot

Add to the bot's `.env`:

```
NINE_ROUTER_AUTH_TOKEN=<api key from dashboard step B.3>
NINE_ROUTER_BASE_URL=http://localhost:20128
NINE_ROUTER_MODEL=Free-models
NINE_ROUTER_REPO_PATH=/Users/<you>/repos/9router   # absolute path
NINE_ROUTER_CONTAINER_NAME=9router
```

Restart the bot. Logs should print `9Router backend enabled`.

### D. Daily usage

- `/9router status` -- container state + `/api/health`.
- `/9router start` -- `docker start 9router`, or run `start.sh` if the
  container is missing (requires `NINE_ROUTER_REPO_PATH`).
- `/9router stop` -- `docker stop 9router`.
- `/backend 9router` -- make 9Router the default for text messages.

Per-request env swap lives in `src/claude/routing.py`
(`anthropic_routing_env`). Sessions are isolated in a dedicated
`nine_router_sessions` table.

**Troubleshooting**

- "Start script failed" — run `bash start.sh` manually in the 9Router repo to
  see the full error; usually a missing `.env` *inside* the 9Router repo.
- Health red after start — `docker logs 9router`; container may still be
  initializing for ~30s on first boot.

## Plan usage

`/usage` reports current Claude plan usage (limits, used, remaining for the
active billing window). Uses the Claude OAuth token already present from
`claude` CLI login — no extra setup.

## External session adoption

`/resume <session_id>` adopts an existing Claude session ID (e.g. one started
in your terminal) so future Telegram messages continue that conversation.
The ID is saved as the current `claude_session_id` for the user; no history
copy.

## Diff review

`/review` runs Claude over the current git diff and posts a Telegram-friendly
review (security, code quality, perf). `/review deep` dispatches parallel
sub-agents (security / code / perf) and aggregates results.

No env config — works wherever the bot's current directory has a git
repository.

## Build info

`/version` shows:

- the commit the running process was launched from (captured at boot via
  `src/utils/gitinfo.py`);
- the commit on disk at `HEAD`;
- whether the two match (i.e. whether you need to `/restart` to pick up new
  code).

## Self-restart and deploy

`/restart` and `/deploy` SIGTERM the bot process. A **supervisor must bring
it back up** — these commands only work when the bot is managed by something
that restarts on exit:

- Linux: `systemd` (see [SYSTEMD_SETUP.md](../SYSTEMD_SETUP.md)).
- macOS: `launchd` or `tmux` + a `while true; do make run; done` loop.
- Docker: `--restart unless-stopped`.

After SIGTERM, `restart_receipt.json` records who triggered the restart and
where to notify. On boot, the bot reads that receipt and DM's the user that
the new version is up (`src/utils/restart_receipt.py`).

`/deploy` does `git pull` then SIGTERMs. If the working tree is dirty or the
pull fails, it aborts and reports the error instead of restarting.

---

## New-machine bootstrap checklist

Prerequisites: Python 3.11+, Poetry, Docker (only if you want 9Router),
a Telegram bot token from [@BotFather](https://t.me/botfather).

1. **Clone the fork and install deps.**
   ```bash
   git clone https://github.com/Huygon764/claude-code-telegram ~/repos/claude-code-telegram
   cd ~/repos/claude-code-telegram
   make dev
   ```
2. **Configure the minimum env.**
   ```bash
   cp .env.example .env
   ```
   Fill the required upstream vars in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=<from @BotFather>
   TELEGRAM_BOT_USERNAME=<bot username>
   APPROVED_DIRECTORY=/Users/<you>/projects
   ALLOWED_USERS=<your Telegram user id>
   ```
3. **Authenticate Claude Code** on this machine (`claude` CLI login) so the
   SDK has an OAuth token. `/usage` also reads this token.
4. **(Optional) Cursor backend** -- install `cursor-agent` on `PATH`, run
   `cursor-agent login`. No bot env required.
5. **(Optional) 9Router backend** -- follow the
   [9Router](#9router-backend) section above (A: container, B: dashboard,
   C: bot env).
6. **(Optional) self-restart supervisor** -- so `/restart` and `/deploy`
   actually come back up. Linux: see [SYSTEMD_SETUP.md](../SYSTEMD_SETUP.md).
7. **Run** with `make run-debug` and verify in Telegram: `/start`,
   `/version`, `/backend`.
