#!/bin/sh
# Copies the bot to legion, runs the self-check and restarts the service. The .env and state.json on legion stay as they are.
set -e
cd "$(dirname "$0")"
HOST=legion@10.0.0.3
scp bot.py test_bot.py sim.py scan.py history.py gate_test.py requirements.txt devbot.service "$HOST:devbot/"
ssh "$HOST" 'cd devbot && .venv/bin/pip install -q -r requirements.txt && .venv/bin/python test_bot.py \
  && mkdir -p ~/.config/systemd/user && cp devbot.service ~/.config/systemd/user/ \
  && systemctl --user daemon-reload && systemctl --user enable devbot && systemctl --user restart devbot'
