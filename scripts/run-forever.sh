#!/usr/bin/env bash
# Supervisor: keep the Telegram bot running.
#
# Relaunches the bot whenever it exits — e.g. after /restart or /deploy
# (which SIGTERM the bot process). No systemd/pm2 needed.
#
# Stop the supervisor itself: Ctrl+C (foreground) or kill this script's
# PID. A plain bot exit/SIGTERM only triggers a restart, not a stop.
set -u

cd "$(dirname "$0")/.." || exit 1

stop=0
trap 'stop=1' INT TERM

while [ "$stop" -eq 0 ]; do
    poetry run claude-telegram-bot || true
    if [ "$stop" -eq 0 ]; then
        echo "[run-forever] bot exited; restarting in 2s..."
        sleep 2
    fi
done

echo "[run-forever] supervisor stopped."
