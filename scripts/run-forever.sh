#!/usr/bin/env bash
# Supervisor: keep the Telegram bot running.
#
# Relaunches the bot whenever it exits — for /restart, /deploy, or any
# unexpected crash. No systemd/pm2 needed.
#
# Crash-loop guard: if the bot exits in under STABLE_SECONDS for
# MAX_FAST_CRASHES runs in a row, the supervisor gives up. This stops
# a broken build from burning CPU restarting itself forever.
#
# Stop the supervisor itself: Ctrl+C (foreground) or kill this script's
# PID. A plain bot exit/SIGTERM only triggers a restart, not a stop.
set -u

cd "$(dirname "$0")/.." || exit 1

STABLE_SECONDS=${RUN_FOREVER_STABLE_SECONDS:-30}
MAX_FAST_CRASHES=${RUN_FOREVER_MAX_FAST_CRASHES:-5}
BACKOFF_SECONDS=${RUN_FOREVER_BACKOFF_SECONDS:-2}

stop=0
trap 'stop=1' INT TERM

fast_crashes=0
while [ "$stop" -eq 0 ]; do
    start_ts=$SECONDS
    poetry run claude-telegram-bot || true
    elapsed=$((SECONDS - start_ts))

    if [ "$stop" -ne 0 ]; then
        break
    fi

    if [ "$elapsed" -ge "$STABLE_SECONDS" ]; then
        fast_crashes=0
    else
        fast_crashes=$((fast_crashes + 1))
        if [ "$fast_crashes" -ge "$MAX_FAST_CRASHES" ]; then
            echo "[run-forever] bot exited $fast_crashes times in <${STABLE_SECONDS}s each; giving up. Fix the crash and re-run." >&2
            exit 1
        fi
    fi

    echo "[run-forever] bot exited after ${elapsed}s (fast_crashes=$fast_crashes); restarting in ${BACKOFF_SECONDS}s..."
    sleep "$BACKOFF_SECONDS"
done

echo "[run-forever] supervisor stopped."
