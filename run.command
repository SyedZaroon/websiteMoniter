#!/bin/bash
# Double-click this file in Finder to run the website checker.
cd "$(dirname "$0")" || exit 1

if [ ! -d ".venv" ]; then
  echo "First run: setting up (this takes about a minute)..."
  python3 -m venv .venv || { echo "Could not create a virtual environment. Is Python 3 installed?"; read -n 1 -s -r -p "Press any key to close..."; exit 1; }
  source .venv/bin/activate
  pip install --upgrade pip >/dev/null
  pip install -r requirements.txt || { echo "Install failed."; read -n 1 -s -r -p "Press any key to close..."; exit 1; }
else
  source .venv/bin/activate
fi

python main.py --open
echo
read -n 1 -s -r -p "Done. Press any key to close this window..."
