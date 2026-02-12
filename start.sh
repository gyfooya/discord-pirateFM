#!/usr/bin/env bash
set -e

# USER Variable
MYUSER="noname"
BASE_DIR="/home/$MYUSER/discord-pirateFM"

cd "$BASE_DIR"
source "$BASE_DIR/venv/bin/activate"
python "$BASE_DIR/discord-bot.py"
