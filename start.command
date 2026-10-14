#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ -x .venv/bin/python3 ]]; then
  .venv/bin/python3 gui.py
else
  python3 gui.py
fi
