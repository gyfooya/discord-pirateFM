#!/usr/bin/env bash
# chmod +x start.sh
# sudo pacman -S ffmpeg
set -e

# USER Variable
MYUSER="noname"
BASE_DIR="/home/$MYUSER/discord-pirateFM"

cd "$BASE_DIR"
source "$BASE_DIR/venv/bin/activate"
python "$BASE_DIR/discord-bot.py"
